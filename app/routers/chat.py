"""③ 대화형 도서 추천: POST /recommendations/chat

명세: docs/wiki/ai/1-model-api/spec.md ③ POST /recommendations/chat
텍스트 턴과 이미지 턴을 여기서 함께 다룬다 — 같은 핸들러가 후보검색·카드생성
파이프라인을 공유하므로 나누지 않는다(개발 워크플로 위키 §9 참고).

처리를 명세 4단계 그대로 함수로 나눴다. 1·3단계는 LLM 체인이고, 2단계는 ①의
하이브리드 검색을 쓴다(아래 각 함수 docstring 참고). 바깥 흐름(이 4단계가 이 순서로
불리는 것)은 안 바뀐다.
"""

import json
import logging
import re
from typing import Any

import asyncpg
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from langchain_core.prompts import ChatPromptTemplate
from pydantic import ValidationError

from app.chat.schemas import MAX_RECENT_TURNS, ChatRequest, Spec, Turn
from app.core import body, categories, db, history, popularity, responses
from app.core.pgvector import to_vector_literal
from app.feed import personalized, scoring
from app.gateway import embedding
from app.gateway.llm import (
    LLMUnavailableError,
    get_chat_model,
    invoke_chain,
    message_text,
    parse_json_response,
)
from app.search import service
from app.search.schemas import MAX_QUERY_CHARS, SearchRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

CANDIDATE_LIMIT = 10
# ①에 없는 exclude, 소개글 없는 책을 검색 결과에서 사후 필터링하므로, 걸러지고도
# CANDIDATE_LIMIT이 남을 만큼 넉넉히 받는다. SearchRequest.size 상한(50) 안쪽.
SEARCH_SIZE = 30

# 명세 에러표의 413은 (V2) 이미지 10MB 상한이라 V1(텍스트 턴)엔 숫자가 없다(#99).
# message(200자)·recent_turns(20턴)는 작지만, exclude_book_ids처럼 길이 상한이
# 없는 배열도 있어 상한 없이 통째로 메모리에 올리면 거절 자체가 공격 수단이 될
# 수 있다. 정상 요청이면 절대 안 닿을 만큼 넉넉하게 잡는다.
MAX_BODY_BYTES = 64 * 1024

# 출판사 비교용 잡음 — "(주)"·"주식회사"·공백. 카탈로그 표기가 "(주)현암사",
# "현암주니어 :현암사"처럼 들쭉날쭉해, 이걸 지우고 포함 관계로 봐야 같은
# 출판사가 표기 차이로 조용히 빠지지 않는다(리뷰 지적).
_PUBLISHER_NOISE = re.compile(r"\(주\)|주식회사|\s+")


def _normalize_publisher(name: str) -> str:
    return _PUBLISHER_NOISE.sub("", name)


# ③ 전용 채점 비중(#222) — ④(app/feed/scoring.py)와 다른 식을 쓴다. 명세 각주: "이번
# 요청 조건(spec.semantic) 유사도와 취향 유사도를 함께 쓴다. ④의 계산식과 다르다."
# 지금까지는 ④와 똑같은 식(취향 0.6·카테고리 0.25·인기 0.15)을 그대로 갖다 썼는데,
# 그러면 사용자가 지금 딱 찾는 조건에 맞는 책이 나와도 "평소 취향과 다르다"는
# 이유로 점수가 낮게 뜬다. 질의 유사도를 메인으로 두고 취향은 동점 보정 정도로
# 낮춘다. 숫자는 ④의 기존 비중처럼 임시값이다 — 시드 사용자로 보고 조정한다.
QUERY_SIMILARITY_WEIGHT = 0.5
TASTE_SIMILARITY_WEIGHT = 0.2
CHAT_CATEGORY_WEIGHT = 0.2
CHAT_POPULARITY_WEIGHT = 0.1


CARD_LIMIT = 3


def parse_request(payload: Any) -> ChatRequest | tuple[int, str]:
    """계약에 맞으면 요청 객체, 아니면 (상태 코드, message) 를 돌려준다.

    spec 필드 자체가 잘못됐으면 422 spec_schema_violation — 명세가 "호출자가
    초기 spec으로 되돌려 1회 재시도"하라고 못박은 전용 에러다. 그 외
    형식 오류(예: message 길이 초과, image_ref와 동시 존재)는 400.
    """
    if not isinstance(payload, dict):
        return (400, "invalid_request")
    try:
        return ChatRequest.model_validate(payload)
    except ValidationError as e:
        # 모델 전체 검증(예: message/image_ref 동시 존재)은 loc이 빈 튜플이라
        # err["loc"][0]을 바로 쓰면 IndexError가 난다 — 실제로 재현된 버그.
        if any(err["loc"] and err["loc"][0] == "spec" for err in e.errors()):
            return (422, "spec_schema_violation")
        return (400, "invalid_request")


# {spec_json}·{message}가 채워지는 자리다. JSON 예시의 중괄호는 {{ }}로 겹쳐 쓴다
# (CARD_PROMPT와 같은 이유 — 실제로 모델에 가는 글자는 겹치기 전과 같다).
#
# 지금까지의 조건 전체를 매번 그대로 옮겨 적게 시켰더니, 안 건드려야 할 값까지
# 예시를 베껴 써서 망가뜨리는 사고가 반복됐다(둘 다 실제 Ollama/qwen2.5:7b로
# 재현): 1) 예시의 false를 그대로 베껴 in_stock_only=true가 매번 false로 바뀜
# 2) intent 자리의 설명 문구("exact 또는 semantic")를 글자 그대로 복사해서
# Spec 검증 실패로 이어짐. 그래서 지금까지의 조건을 다시 옮겨 적게 하는 대신
# 이번 메시지로 "바뀌는 값만" 답하게 하고, 언급 안 된 값은 서버가 지금 조건에서
# 그대로 들고 와 겹쳐 쓴다(_merge_spec_patch) — 모델이 값을 옮겨 적다 틀릴
# 여지 자체를 없앤다. exclude도 같은 이유로 매번 전체 목록을 다시 쓰게 하면
# 모델이 옛 항목을 빠뜨렸을 때 이미 제외했던 책이 되살아난다 — 새로 빼고 싶은
# 것만 받아 서버가 기존 목록에 더한다.
#
# "너무 무겁지 않은 걸로" 같은 분위기·톤 조정 표현은 제목·저자·가격·장르·
# 제외 어디에도 안 맞아 지시문이 없으면 그냥 버려진다(실제 Ollama/qwen2.5:7b로
# 재현, 이슈 #132). exact·filters와 달리 semantic은 문장 하나짜리라 하위 키
# patch가 안 되고, 반영하려면 지금 문장을 바탕으로 전체를 다시 써야 한다 —
# 그래서 "언제 semantic을 통째로 다시 쓰는지"를 별도로 못박아둔다.
#
# "2만원 이하 아니어도돼"처럼 조건을 없애 달라는 말도 지시문이 없으면 그냥
# 버려진다(이슈 #239) — patch에 그 키를 안 넣는 것과 "없애라"를 모델이
# 구분 못 해서, _merge_spec_patch가 "patch에 없으면 기존 값 유지"로 처리해
# 이전 조건이 안 풀리고 눌러앉는다. 그래서 없애는 의도는 그 키를 JSON null로
# 명시하라고 따로 가르친다 — null이 명시되면 _merge_spec_patch가 정상적으로
# 지운다.
#
# 재고 조건(in_stock_only)은 지시문과 예시에 넣지 않는다(#319). AI가 재고를 받지
# 않게 되어(#307) 챗봇은 이 조건을 쓰지 않는다. 예시에 있으면 모델이 사용자가
# 말하지 않은 재고 조건을 지어낸다.
SPEC_PROMPT = ChatPromptTemplate.from_template(
    "너는 책 추천 챗봇의 조건(spec) 갱신기다. 이번 메시지를 보고 조건 중 "
    "실제로 바뀌는 값만 JSON으로 답하라 — 언급되지 않은 값은 답에 아예 "
    "넣지 마라. 지금 조건을 옮겨 적을 필요 없다, 안 넣은 값은 그대로 "
    "유지된다.\n\n"
    "최근 대화(참고용 — 이번 메시지가 가리키는 대상을 파악할 때만 참고하고, "
    "여기 나온 값을 그대로 옮겨 적지 마라):\n{recent_turns}\n\n"
    "지금 조건(spec):\n{spec_json}\n\n"
    '이번 메시지: "{message}"\n\n'
    "intent를 이번에 새로 정할 때만 아래 둘 중 하나를 그 낱말 그대로 써라"
    "(설명 문구를 베끼면 안 된다).\n"
    "- exact: 제목이나 저자를 콕 집어 말함\n"
    "- semantic: 분위기나 상황을 말함\n\n"
    "exact·filters는 바뀌는 하위 키만 넣어라 — 예를 들어 가격 상한만 새로 "
    '말했으면 {{"filters": {{"price_max": 20000}}}}처럼 그 키 하나만 담고, '
    "다른 하위 키는 넣지 마라.\n"
    '이번 메시지가 어떤 조건을 없애 달라는 뜻이면(예: "가격 상관없어", '
    '"2만원 이하 아니어도돼", "장르 상관없이 다 보여줘") 그 하위 키 값을 '
    '"JSON null"로 명시해서 넣어라 — 예를 들어 가격 상한을 없애 달라면 '
    '{{"filters": {{"price_max": null}}}}처럼 그 키를 null로 채워라. 그냥 '
    "안 넣으면 이전 값이 그대로 남으니, 없애려면 반드시 null을 명시해야 한다.\n"
    "exclude는 이번에 새로 빼고 싶은 책 id만 넣어라 — 기존 목록은 서버가 "
    "그대로 유지하니 다시 적을 필요 없다.\n\n"
    "이번 메시지가 제목·저자·가격·장르·제외처럼 구체적인 조건이 아니라 "
    '"너무 무겁지 않게", "더 재밌는 걸로", "덜 슬프게"처럼 분위기나 톤을 '
    "조정하는 말이면, 지금 semantic 문장을 바탕으로 그 톤을 반영한 "
    "완전한 새 문장을 semantic에 통째로 다시 써라 — 다른 필드처럼 바뀐 "
    "조각만 넣는 게 아니라 문장 전체를 새로 써야 한다.\n"
    '한 메시지에 구체적인 조건과 분위기·톤이 같이 오면(예: "가벼운 걸로, '
    '1만원 이하로") 둘 다 반영해라 — 조건은 해당 필드에, 톤은 semantic에 '
    "각각 넣고 한쪽만 고르지 마라. 아래 마지막 예시가 이 경우다.\n\n"
    "이번 메시지로 바뀌는 게 없으면 빈 객체 {{}}로 답하라. 숫자는 따옴표 "
    "없이 써라. 아래는 형식 예시일 뿐 실제 값이 아니다 — 그대로 베끼지 "
    "말고 실제로 바뀌는 값만 채워라:\n"
    '{{"semantic": "비 오는 날 읽을 잔잔한 책"}}\n\n'
    "톤 조정 예시(형식일 뿐 실제 값 아님) — 지금 semantic이 "
    '"비 오는 날 읽을 책"이고 메시지가 "너무 무겁지 않은 걸로"면:\n'
    '{{"semantic": "비 오는 날 읽을 무겁지 않은 책"}}\n\n'
    "조건+톤 혼합 예시(형식일 뿐 실제 값 아님) — 지금 semantic이 "
    '"비 오는 날 읽을 책"이고 메시지가 "가벼운 걸로, 1만원 이하로"면 '
    "둘 다 넣어라:\n"
    '{{"semantic": "비 오는 날 읽을 가벼운 책", '
    '"filters": {{"price_max": 10000}}}}\n\n'
    "조건 제거 예시(형식일 뿐 실제 값 아님) — 지금 filters.price_max가 "
    '20000이고 메시지가 "2만원 이하 아니어도돼"면, 키를 빼지 말고 null로 '
    "명시해라:\n"
    '{{"filters": {{"price_max": null}}}}\n\n'
    "제목 지정 예시(형식일 뿐 실제 값 아님) — 메시지가 "
    '"토지 찾아줘"면:\n'
    '{{"intent": "exact", "exact": {{"title": "토지"}}}}\n'
    "저자 지정 예시(형식일 뿐 실제 값 아님) — 메시지가 "
    '"박경리 책 보여줘"면:\n'
    '{{"intent": "exact", "exact": {{"author": "박경리"}}}}\n\n'
    "중요: 메시지가 책 제목·작품명·저자 이름뿐이거나 "
    '"~ 찾아줘/보여줘"처럼 특정 책이나 저자를 가리키면 분위기를 지어내지 '
    "말고 intent를 exact로 해라. 제목이면 exact.title에, 저자면 exact.author에 "
    "그 이름을 그대로 넣어라. 메시지에 없는 분위기·톤(가벼운, 잔잔한 등)을 "
    "semantic에 절대 덧붙이지 마라.\n"
    "제목만 말한 예시(형식일 뿐 실제 값 아님) — 메시지가 "
    '"데미안"이면:\n'
    '{{"intent": "exact", "exact": {{"title": "데미안"}}}}'
)


def _format_recent_turns(turns: list[Turn]) -> str:
    """최근 대화를 "user: …" 줄로 바꾼다. 없으면 빈 대화임을 알리는 문구.

    명세: 최대 20턴, 초과분은 최근 20턴만 사용 — 뒤에서 MAX_RECENT_TURNS개만 자른다.
    """
    if not turns:
        return "(최근 대화 없음)"
    return "\n".join(f"{t.role}: {t.text}" for t in turns[-MAX_RECENT_TURNS:])


def _clear_stale_exact(merged_exact: dict, sub_patch: dict) -> None:
    """새 제목을 찾을 때 이전 턴의 저자·출판사가 남으면 엉뚱한 책이 되거나 0건이 된다(#203).

    title이 이번 patch에 새로 오면, 같이 안 온 author·publisher는 이전 턴 값을
    지운다 — 다른 책을 찾는 것이니 이전 저자·출판사는 이제 안 맞다. 같은 patch에
    author·publisher도 같이 왔으면(예: "이 저자의 이 책") 그 값은 그대로 쓴다.
    """
    if "title" not in sub_patch:
        return
    if "author" not in sub_patch:
        merged_exact["author"] = None
    if "publisher" not in sub_patch:
        merged_exact["publisher"] = None


def _clear_inverted_range(
    merged_filters: dict, sub_patch: dict, low: str, high: str
) -> None:
    """가격·연도 구간의 한쪽만 새로 오면, 반대쪽에 남아 있던 이전 값과 뒤집힐 수 있다(#203).

    뒤집힌 채로 두면 Spec 검증(SearchFilters._ranges_are_ordered)이 실패해
    update_spec이 이걸 LLM 장애와 똑같이 spec_degraded로 처리한다 — LLM은
    멀쩡히 답했는데 병합 로직 때문에 대화가 막힌다. 이번에 안 건드린 반대쪽이
    새 값과 뒤집히면, 그 반대쪽은 더 이상 유효하지 않은 이전 조건이므로 지운다.
    """
    low_given, high_given = low in sub_patch, high in sub_patch
    if low_given and not high_given:
        old_high = merged_filters.get(high)
        new_low = merged_filters.get(low)
        if old_high is not None and new_low is not None and new_low > old_high:
            merged_filters[high] = None
    elif high_given and not low_given:
        old_low = merged_filters.get(low)
        new_high = merged_filters.get(high)
        if old_low is not None and new_high is not None and old_low > new_high:
            merged_filters[low] = None


def _merge_spec_patch(current: Spec, patch: dict) -> dict:
    """이번 턴에 LLM이 바꾼 값만 담긴 patch를 지금 spec 위에 겹쳐 완전한 spec dict를 만든다.

    patch에 없는 키(하위 키 포함)는 지금 값을 그대로 두고, 있는 키만
    덮어쓴다. exclude는 겹쳐 쓰지 않고 더한다 — 모델이 옛 항목을 안 실어도
    사라지지 않는다. 단순 겹쳐쓰기만으로는 안 맞는 두 경우(#203)는 겹쳐쓴
    뒤 따로 손본다 — 새 제목에 이전 저자·출판사가 남는 경우, 구간 한쪽만
    새로 와 반대쪽과 뒤집히는 경우.
    """
    merged = current.model_dump()
    for key in ("exact", "filters"):
        sub_patch = patch.get(key)
        if isinstance(sub_patch, dict):
            merged[key] = {**merged[key], **sub_patch}

    exact_patch = patch.get("exact")
    if isinstance(exact_patch, dict):
        _clear_stale_exact(merged["exact"], exact_patch)

    filters_patch = patch.get("filters")
    if isinstance(filters_patch, dict):
        _clear_inverted_range(
            merged["filters"], filters_patch, "price_min", "price_max"
        )
        _clear_inverted_range(
            merged["filters"], filters_patch, "pub_year_from", "pub_year_to"
        )

    new_exclude = patch.get("exclude")
    if isinstance(new_exclude, list):
        merged["exclude"] = merged["exclude"] + [
            book_id for book_id in new_exclude if book_id not in merged["exclude"]
        ]

    for key in ("intent", "semantic", "anchor_book"):
        if key in patch:
            merged[key] = patch[key]

    # LLM이 제목·저자만 채우고 intent를 빠뜨리면 semantic으로 남아 소개글 없는 책이 버려진다(#281).
    # intent를 직접 준 경우는 LLM 판단을 따른다.
    if (
        "intent" not in patch
        and isinstance(exact_patch, dict)
        and (exact_patch.get("title") or exact_patch.get("author"))
    ):
        merged["intent"] = "exact"

    return merged


async def update_spec(
    message: str, spec: Spec, recent_turns: list[Turn]
) -> tuple[Spec, bool]:
    """1단계 — 메시지로 spec을 갱신한다. 명세대로 LLM이 한다.

    LLM은 지금 조건 전체가 아니라 바뀌는 값만 담은 patch를 돌려주고,
    _merge_spec_patch가 지금 spec 위에 겹쳐 완전한 spec을 만든다(SPEC_PROMPT
    주석 참고).

    recent_turns는 spec에 안 담기는 애매한 참조("아까 그 책 말고")를 이번
    메시지가 뭘 가리키는지 판단하는 데만 쓴다. 서버는 대화를 저장하지 않으므로
    (명세) 이번 호출이 끝나면 버려지고, 다음 턴에도 호출자가 다시 실어 보낸다.

    실패(LLM 장애, 또는 병합한 값이 Spec 모양이 아님)하면 원래 spec을 그대로
    돌려주고 두 번째 값을 True로 준다. 명세: "LLM 장애면 1과 3을 건너뛰고
    요청의 spec으로 2만 돌려 점수 상위 3권을 낸다" — 호출부(chat())가 이
    신호를 보고 3단계(카드 생성)를 건너뛴다.
    """
    chain = SPEC_PROMPT | get_chat_model() | message_text | parse_json_response
    try:
        patch = await invoke_chain(
            chain,
            {
                "recent_turns": _format_recent_turns(recent_turns),
                "spec_json": json.dumps(spec.model_dump(), ensure_ascii=False),
                "message": message,
            },
        )
        return Spec.model_validate(_merge_spec_patch(spec, patch)), False
    except LLMUnavailableError:
        logger.exception("spec 갱신 실패")
        return spec, True
    except ValidationError as exc:
        # pydantic ValidationError의 문자열 표현은 각 오류에 input_value=...로 실제
        # 값을 싣는다. LLM이 patch에 사용자 발화를 옮겨 적어 여기 걸리면(SPEC_PROMPT
        # 주석의 실제 재현 사례들처럼), logger.exception()을 그대로 쓰면 그 값이
        # ERROR 로그에 남는다(리뷰 지적, #202). 어느 필드가 왜 틀렸는지(loc·type)만
        # 남긴다.
        logger.error(
            "spec 갱신 실패: 병합 결과가 Spec 모양이 아님 %s",
            [(err["loc"], err["type"]) for err in exc.errors()],
        )
        return spec, True


def _query_text(spec: Spec) -> str:
    """spec에서 ①에 넘길 검색어 한 줄을 만든다.

    intent가 exact면 지정된 제목·저자를 합친다. 출판사는 일부러 뺀다 — ①의
    키워드 검색은 제목·저자·소개글에서만 낱말을 찾고 평균 커버리지가 기준
    (0.6)을 못 채우면 후보가 통째로 빠지는데, 출판사는 이 셋 어디에도 없는
    낱말이라 평균만 깎아 정확한 책을 탈락시킨다(예: "마음 현암사", 리뷰 지적).
    출판사는 검색 결과를 받은 뒤 get_candidates()가 publisher 필드로 거른다
    (이슈 #137). 제목·저자·semantic이 전부 비어 출판사만 남으면, 출판사를
    최후 수단으로 검색어에 써서 최소한 service.search()까지는 가게 한다 —
    안 그러면 빈 검색어로 바로 return [] 돼 그 뒤의 publisher 필터가 걸릴
    기회조차 없다.
    어느 쪽도 없으면 빈 문자열(호출부가 후보 없음으로 처리).
    """
    exact_query = " ".join(p for p in (spec.exact.title, spec.exact.author) if p)
    text = (
        exact_query
        if spec.intent == "exact" and exact_query
        else spec.semantic or exact_query or spec.exact.publisher
    )
    return (text or "").strip()[:MAX_QUERY_CHARS]


async def _fetch_descriptions(book_ids: list[int]) -> dict[int, str]:
    """①의 book_id 목록에 description을 붙인다.

    app.search.books.fetch()는 description을 안 돌려준다 — ①의 응답 계약에
    없는 필드라, 거기 추가하면 /search 응답에도 새 나간다. ①의 파일은 건드리지
    않고 ③에서 직접 조회한다(엔드포인트 소유권 교차 금지).
    """
    if not book_ids:
        return {}
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT book_id, description FROM v_books WHERE book_id = ANY($1::int[])",
            book_ids,
        )
    return {r["book_id"]: r["description"] for r in rows}


# 카탈로그는 판본·권마다 book_id가 달라 같은 책이 후보와 카드에 여러 번 나온다(#281).
# 저자 표기도 "지은이: 한강", "J.K. 롤링 지음 ;강동혁 옮김"처럼 들쭉날쭉해서, 역할 말을 빼고
# 첫 저자만 본다. 역할 접두어는 콜론이 있을 때만, 접미어는 앞에 공백이 있을 때만 뗀다 —
# 안 그러면 이름 속 글자("글", "저")가 같이 잘린다.
_AUTHOR_ROLE_PREFIX = re.compile(
    r"^(지은이|저자|글쓴이|글|그림|그린이|원작|옮긴이|역자|엮은이|편저)\s*[:：]\s*"
)
_AUTHOR_ROLE_SUFFIX = re.compile(r"\s+(지음|옮김|엮음|그림|글|저|역|편)\s*$")
_NON_WORD = re.compile(r"[\W_]+")


def _edition_key(title: str | None, author: str | None) -> tuple[str, str] | None:
    """같은 책의 다른 판본을 한 키로 묶는다. 제목이나 저자가 비면 None — 다른 책일 수 있어 안 묶는다."""
    first_author = re.split(r"[;,]", author or "")[0].strip()
    first_author = _AUTHOR_ROLE_PREFIX.sub("", first_author)
    first_author = _AUTHOR_ROLE_SUFFIX.sub("", first_author)
    title_key = _NON_WORD.sub("", (title or "").casefold())
    author_key = _NON_WORD.sub("", first_author.casefold())
    if not title_key or not author_key:
        return None
    return title_key, author_key


def _dedupe_editions(pool: list[dict]) -> list[dict]:
    """같은 책의 판본은 앞에 나온 하나만 남긴다. 앞 판본에 소개글이 없고 뒤에 있으면 뒤 것으로 바꾼다."""
    kept: list[dict] = []
    position: dict[tuple[str, str], int] = {}
    for candidate in pool:
        key = _edition_key(candidate.get("title"), candidate.get("author"))
        if key is None:
            kept.append(candidate)
        elif key not in position:
            position[key] = len(kept)
            kept.append(candidate)
        elif not kept[position[key]]["description"] and candidate["description"]:
            kept[position[key]] = candidate
    return kept


async def _fetch_edition_keys(book_ids: list[int]) -> set[tuple[str, str]]:
    """이미 보여 준(제외한) 책들의 판본 키. 그 책의 다른 판본이 다시 추천되지 않게 한다."""
    if not book_ids:
        return set()
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT title, author FROM v_books WHERE book_id = ANY($1::int[])",
            book_ids,
        )
    keys = {_edition_key(r["title"], r["author"]) for r in rows}
    keys.discard(None)
    return keys


async def _fetch_categories(book_ids: list[int]) -> dict[int, str | None]:
    """①의 book_id 목록에 카탈로그 분류(v_books.category)를 붙인다.

    _fetch_descriptions와 같은 이유로 ①의 파일은 안 건드리고 여기서 직접 조회한다
    (엔드포인트 소유권 교차 금지). 장르 필터 정규화(_catalog_categories_for)에서 쓴다.
    """
    if not book_ids:
        return {}
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT book_id, category FROM v_books WHERE book_id = ANY($1::int[])",
            book_ids,
        )
    return {r["book_id"]: r["category"] for r in rows}


def _catalog_categories_for(category: str) -> tuple[str, ...] | None:
    """온보딩 장르 값(예: "소설")이면 대응하는 카탈로그 분류들(핵심+일부, #110).

    대응표에 없는 값(예: 이미 카탈로그 분류명 그대로거나, 대응표가 모르는 값)이면
    None — 호출부가 예전처럼 ①의 정확 일치 필터를 그대로 쓴다(categories.py
    docstring: "여기 없는 값은 쓰는 쪽에서 건너뛴다").
    """
    match = categories.ONBOARDING_TO_CATALOG.get(category)
    if match is None:
        return None
    return match.core + match.partial


async def _fetch_history(user_id: int) -> history.History:
    """user_id의 구매·라이브러리·리뷰 이력을 읽는다.

    ⑥이 취향 벡터를 계산할 때 쓰는 것과 같은 공용 함수(app/core/history.py)를
    그대로 쓴다 — 이력을 읽는 규칙(가중치, 어느 테이블을 보는지)이 갈라지면
    안 되기 때문이다.
    """
    pool = db.get_pool()
    async with pool.acquire() as conn:
        return await history.read(conn, user_id)


def _exclude_owned_or_disliked(pool: list[dict], hist: history.History) -> list[dict]:
    """이미 구매했거나 별점 1–2점을 준 책은 후보에서 뺀다(명세 2단계).

    history.weights는 책마다 가장 큰 가중치 하나만 남긴다 — PURCHASE(3)는
    구매 행에만 매겨지는 값이라, weights가 이 값이면 구매한 책이라고 안전하게
    가를 수 있다(라이브러리 담기·긍정 리뷰만 있는 책은 안 걸린다). 저평점
    (2.0점 이하)은 hist.disliked_book_ids로 이미 따로 온다.
    """
    return [
        c
        for c in pool
        if hist.weights.get(c["book_id"]) != history.PURCHASE
        and c["book_id"] not in hist.disliked_book_ids
    ]


async def _fetch_similarities(
    conn: asyncpg.Connection, book_ids: list[int], centroid: list[float]
) -> dict[int, float]:
    """후보 book_id들과 취향 centroid의 코사인 유사도(#180).

    ④(app.feed.personalized)는 centroid와 가까운 책을 벡터 색인으로 "찾는다"(수십만
    권 중 500권). ③은 후보가 이미 정해져 있어(①의 키워드 검색 결과, 최대
    CANDIDATE_LIMIT권) 그 책들의 거리만 "재는" 거라 색인 튜닝 없이 WHERE IN 하나로
    충분하다 — book_embeddings는 book_id가 PK라 어차피 색인을 탄다.

    pgvector `<=>`는 코사인 거리라 1을 빼야 유사도다(④와 같은 식, personalized.py 참고).
    """
    if not book_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT book_id, 1 - (embedding <=> $1::vector) AS similarity
        FROM book_embeddings
        WHERE book_id = ANY($2::int[])
        """,
        to_vector_literal(centroid),
        book_ids,
    )
    return {r["book_id"]: r["similarity"] for r in rows}


async def _embed_query_semantic(text: str) -> list[float] | None:
    """spec.semantic을 벡터로 바꾼다. 실패하면 None — match_score는 취향 기반 옛 식으로 폴백한다(#222).

    ①(app.search.service._embed_query)과 같은 방어 패턴이다 — 임베딩 장애로
    ③ 전체가 500이 되면 안 된다. ①이 이미 검색어(_query_text(spec))를
    임베딩하지만, exact와 semantic이 함께 오면 그 검색어가 spec.semantic과
    달라질 수 있어(예: 제목은 지정하고 분위기도 말한 경우) 여기서 따로 부른다.

    intent가 semantic이면(제일 흔한 경우) ①과 같은 문장을 중복으로 임베딩하는
    셈이지만, 실측(로컬 CPU, 모델 로딩 후) p95 11ms로 턴 목표(6~8초)에 비해
    미미해 지금은 그대로 둔다. ①이 벡터를 밖으로 안 돌려줘 재사용하려면 그
    파일(팀원 소유)을 고쳐야 한다.
    """
    try:
        vectors, _, _ = await embedding.embed([text], "query")
    except Exception:
        logger.exception("질의 임베딩 실패. 취향 기반 점수로 대신한다")
        return None
    return vectors[0]


def _chat_match_score(
    query_similarity: float | None,
    taste_similarity: float | None,
    category_raw: float | None,
    popularity_value: float | None,
    catalog_max: float | None,
    *,
    has_taste: bool,
) -> int:
    """③ 전용 매칭 점수(#222) — 이번 요청 조건과의 유사도를 메인으로 쓴다.

    ④(app.feed.scoring.match_score)와 계산식이 다르다(명세 ③ 각주). 취향
    유사도는 ④와 같은 원칙으로 없으면 0으로 두고 남은 비중을 재분배하지
    않는다 — 축소 응답도 같은 잣대를 쓰기 위해서다.
    """
    total = QUERY_SIMILARITY_WEIGHT * scoring.similarity_part(query_similarity)
    total += CHAT_CATEGORY_WEIGHT * scoring.category_part(category_raw)
    total += CHAT_POPULARITY_WEIGHT * scoring.popularity_part(
        popularity_value, catalog_max
    )
    if has_taste:
        total += TASTE_SIMILARITY_WEIGHT * scoring.similarity_part(taste_similarity)
    return round(total * 100)


async def _attach_match_scores(
    conn: asyncpg.Connection, candidates: list[dict], user_id: int, spec: Spec
) -> None:
    """후보마다 match_score를 채운다(명세 2단계 축소판, #129).

    ①의 검색 결과에는 카테고리·인기 점수가 없다(응답 계약 밖이라 ①의 파일은
    안 건드리고 여기서 직접 조회한다 — _fetch_descriptions와 같은 이유,
    엔드포인트 소유권 교차 금지). 취향 벡터 유사도(책 임베딩 대 취향
    centroid)는 프로필이 있는 사용자만 붙는다(#180) — cold_start(프로필이
    없거나 아직 벡터를 못 만든 사용자)는 ④의 cold_start 목록과 같은 이유로
    유사도가 없다.

    spec.semantic이 있으면(#222) 이번 요청 조건과 후보 책의 유사도를 메인으로
    쓰는 ③ 전용 식(_chat_match_score)으로 계산한다. semantic이 없거나(exact
    의도라 비교할 조건이 없음) 그 임베딩이 실패하면, ④와 같은 식
    (app.feed.scoring.match_score, with_similarity=has_centroid)으로 그대로
    폴백한다 — 지금까지의 동작을 그대로 유지한다.

    candidates를 그 자리에서 고친다(각 항목에 match_score·popularity 칸을
    더한다) — _fetch_descriptions가 description을 더하는 것과 같은 방식이다.
    conn을 받는다(app.feed의 함수들과 같은 방식) — 테스트가 트랜잭션 하나로
    묶어 흔적 없이 돌릴 수 있게 한다.
    """
    if not candidates:
        return
    book_ids = [c["book_id"] for c in candidates]
    rows = await conn.fetch(
        f"""
        SELECT b.book_id, b.category,
               coalesce({popularity.score_sql("p")}, 0) AS popularity
        FROM v_books b
        LEFT JOIN v_book_popularity p USING (book_id)
        WHERE b.book_id = ANY($1::int[])
        """,
        book_ids,
    )
    profile_row = await conn.fetchrow(
        "SELECT centroid::text AS centroid, tag_weights::text AS tag_weights, "
        "cold_start FROM taste_profile WHERE user_id = $1",
        user_id,
    )
    catalog_max = await personalized.catalog_max_popularity(conn)

    # ④의 _profile()과 같은 조건(app/feed/service.py) — cold_start거나 centroid가
    # 없으면 벡터를 못 쓰는 사용자다. 저장된 벡터가 없으면 구매·리뷰 이력이 있어도 취향을
    # 만들지 않는다(#266) — BE는 개인화 추천에 동의한 사용자만 ⑥을 부르므로, 프로필이 없는
    # 사용자는 동의하지 않은 사용자다.
    has_centroid = bool(
        profile_row and not profile_row["cold_start"] and profile_row["centroid"]
    )
    tag_weights = json.loads(profile_row["tag_weights"]) if profile_row else {}
    centroid = json.loads(profile_row["centroid"]) if has_centroid else None
    similarities: dict[int, float] = {}
    if has_centroid:
        similarities = await _fetch_similarities(conn, book_ids, centroid)

    # #222 — semantic이 있으면(이번 요청의 조건) 그 임베딩과 후보 책들의 유사도를
    # 구해 ③ 전용 식으로 점수를 낸다. 없거나 임베딩이 실패하면 has_query가 False로
    # 남아 아래에서 옛 식(④와 동일)으로 그대로 폴백한다.
    has_query = False
    query_similarities: dict[int, float] = {}
    if spec.semantic:
        query_vector = await _embed_query_semantic(spec.semantic)
        if query_vector is not None:
            has_query = True
            query_similarities = await _fetch_similarities(conn, book_ids, query_vector)

    fields = {r["book_id"]: r for r in rows}
    for c in candidates:
        f = fields.get(c["book_id"])
        category = f["category"] if f else None
        c["popularity"] = f["popularity"] if f else 0
        if has_query:
            c["match_score"] = _chat_match_score(
                query_similarities.get(c["book_id"]),
                similarities.get(c["book_id"]),
                tag_weights.get(category),
                c["popularity"],
                catalog_max,
                has_taste=has_centroid,
            )
        else:
            c["match_score"] = scoring.match_score(
                similarities.get(c["book_id"]),
                tag_weights.get(category),
                c["popularity"],
                catalog_max,
                with_similarity=has_centroid,
            )


def _rule_based_cards(candidates: list[dict]) -> list[dict]:
    """LLM 장애 시 match_score 상위 CARD_LIMIT권으로 카드를 채운다(명세 3단계 축소판).

    명세: 이때 reason_short는 규칙으로 만들고 reason_long은 null이다. 동점이면
    ④(#172)와 같은 규칙으로 인기 → 번호 순으로 가른다(candidates에는 신간
    여부(pub_year)가 없어 그 항만 뺀다).
    """
    ranked = sorted(
        candidates, key=lambda c: (-c["match_score"], -c["popularity"], c["book_id"])
    )
    return [
        {
            "book_id": c["book_id"],
            "rank": rank,
            "match_score": c["match_score"],
            "title": c["title"],
            "author": c["author"],
            "price": c.get("price"),
            "cover_url": c.get("cover_url"),
            "reason_short": "지금 조건에 잘 맞는 책이에요.",
            "reason_long": None,
            "match_basis": [],
        }
        for rank, c in enumerate(ranked[:CARD_LIMIT], start=1)
    ]


async def get_candidates(
    spec: Spec, exclude_book_ids: list[int], user_id: int
) -> list[dict]:
    """2단계 — spec으로 후보를 뽑는다. ①의 하이브리드 검색(제목 완전 일치 먼저, 나머지는 순위 합치기)을 쓴다.

    match_score는 _attach_match_scores가 채운다(#129). 이미 구매했거나
    저평점 준 책 제외는 점수와 무관하게 바로 되므로 여기서 한다(#144).
    exclude·publisher는 ①에 없는 개념이라(①은 이 필요가 없음) 결과를 받은
    뒤 여기서 직접 거른다. ①이 keyword-only로 축소됐는지는 지금은 안 본다
    (후속 판단).

    semantic이면 소개글 없는 책도 여기서 뺀다 — 3단계는 소개글만 근거로
    카드를 쓰는데, 빈 소개글을 그대로 넘기면 모델이 제목·저자만 보고 이유를
    지어낸다. exact는 분위기를 안 보므로 소개글 유무와 무관하게 그대로 둔다
    (리뷰 — ①의 하이브리드 검색이 소개글 없는 책도 키워드로 찾아주는 걸
    exact에서는 그대로 살린다). exclude와 마찬가지로 CANDIDATE_LIMIT으로
    자르기 전에 걸러야, 소개글 없는 책이 그 자리를 먹고 뒤쪽의 쓸 수 있는
    후보가 밀려나지 않는다.
    """
    query = _query_text(spec)
    if not query:
        return []

    # 장르는 온보딩 값(예: "소설")으로 오는데 ①의 category 필터는 카탈로그 분류명과
    # 정확히 같아야 해서 그대로 넘기면 0건이 된다(#203). 대응표에 있는 값이면 ①에는
    # category를 안 보내고(정확 일치를 걸면 애초에 0건이라 넓게 받아야 한다) 아래에서
    # 카탈로그 분류로 직접 거른다 — publisher와 같은 패턴(엔드포인트 소유권 교차 금지).
    search_filters = spec.filters
    catalog_categories = None
    if spec.filters.category is not None:
        catalog_categories = _catalog_categories_for(spec.filters.category)
        if catalog_categories is not None:
            search_filters = spec.filters.model_copy(update={"category": None})

    outcome = await service.search(
        SearchRequest(query=query, filters=search_filters, size=SEARCH_SIZE)
    )
    exclude = set(exclude_book_ids) | set(spec.exclude)
    pool = [r for r in outcome.results if r["book_id"] not in exclude]
    if catalog_categories is not None:
        book_categories = await _fetch_categories([c["book_id"] for c in pool])
        pool = [
            r for r in pool if book_categories.get(r["book_id"]) in catalog_categories
        ]
    if spec.exact.publisher:
        # _query_text()는 출판사를 검색어에 안 섞는다(위 docstring) — 그래서 여기서
        # 결과를 따로 거른다. exclude와 같은 이유로 CANDIDATE_LIMIT 전에 거른다.
        # 완전 일치가 아니라 포함 관계로 본다 — "(주)현암사", "현암주니어 :현암사"처럼
        # 표기가 섞여 있어 완전 일치면 같은 출판사가 조용히 빠진다(리뷰 지적).
        publisher = _normalize_publisher(spec.exact.publisher)
        pool = [
            r
            for r in pool
            if publisher in _normalize_publisher(r.get("publisher") or "")
        ]
    if not pool:
        return []

    hist = await _fetch_history(user_id)
    pool = _exclude_owned_or_disliked(pool, hist)
    if not pool:
        return []

    descriptions = await _fetch_descriptions([c["book_id"] for c in pool])
    for c in pool:
        c["description"] = descriptions.get(c["book_id"]) or ""

    if spec.intent == "semantic":
        pool = [c for c in pool if c["description"]]

    # 이미 보여 준 책의 다른 판본, 그리고 후보끼리 겹치는 판본을 CANDIDATE_LIMIT 전에 걷어 낸다(#281).
    shown_keys = await _fetch_edition_keys(list(exclude))
    if shown_keys:
        pool = [
            c
            for c in pool
            if _edition_key(c.get("title"), c.get("author")) not in shown_keys
        ]
    pool = _dedupe_editions(pool)
    pool = pool[:CANDIDATE_LIMIT]
    async with db.get_pool().acquire() as conn:
        await _attach_match_scores(conn, pool, user_id, spec)
    return pool


def _card_prompt_intro(spec: Spec) -> str:
    """카드 생성 프롬프트 첫 줄 — spec 상태에 따라 다른 문장을 쓴다(#153).

    semantic이 없는데도 예전처럼 "분위기: None"을 그대로 박아 넣으면 모델이
    카드를 거의 못 만든다(실측: semantic만 채워 넣으면 카드 3장, null이면
    0장). exact 의도는 애초에 semantic이 없는 게 정상이라 대부분의 exact
    대화가 여기 걸렸다 — semantic 유무뿐 아니라 exact.title/author 유무까지
    봐서, 상황에 맞는 문장을 고른다.
    """
    if spec.semantic:
        return f'사용자가 원하는 책 분위기: "{spec.semantic}"'
    if spec.exact.title and spec.exact.author:
        return (
            f'사용자가 "{spec.exact.title}"(저자: {spec.exact.author})를 '
            "콕 집어 찾았다. 아래는 그 후보 목록이다."
        )
    if spec.exact.title:
        return (
            f'사용자가 "{spec.exact.title}"를 콕 집어 찾았다. 아래는 그 후보 목록이다.'
        )
    if spec.exact.author:
        return f'사용자가 저자 "{spec.exact.author}"의 책을 찾았다. 아래는 그 후보 목록이다.'
    return "아래는 사용자 조건에 맞는 책 후보 목록이다."


# {intro}·{limit}·{listing}이 채워지는 자리다. JSON 예시의 중괄호는 자리 표시로
# 오해받지 않게 {{ }}로 겹쳐 쓴다 — 실제로 모델에 가는 글자는 겹치기 전과 같다.
CARD_PROMPT = ChatPromptTemplate.from_template(
    "{intro}\n\n"
    "아래 책 목록 중 위 조건에 맞는 책을 최대 {limit}권 골라라.\n"
    "반드시 한국어로만 답하라. 다른 언어를 섞지 마라.\n"
    "book_id는 반드시 아래 목록에 적힌 값을 그대로 써라. 순서 번호가 아니다.\n"
    "match_basis는 reason_short·reason_long과 같은 근거를 label(예: 분위기, "
    "장르, 가격)과 detail(그 근거의 구체적 내용) 짝으로 1~3개 적어라 — 새로운 "
    "근거를 지어내지 말고 두 이유 문장에 이미 쓴 근거만 옮겨 적어라.\n"
    "reply는 고른 책을 언급하며 사용자에게 말하듯 1-2문장으로 답하라(최대 200자).\n"
    '다음 JSON 형식으로만 답하라: {{"reply": "말풍선에 보여줄 1-2문장", '
    '"cards": [{{"book_id": 정수, '
    '"reason_short": "한 줄 이유(80자 이내)", "reason_long": "긴 이유(2-4문장)", '
    '"match_basis": [{{"label": "분위기", "detail": "잔잔함"}}]}}]}}\n\n'
    "책 목록:\n{listing}"
)


def _format_listing(candidates: list[dict]) -> str:
    # 목록 번호("1. 2. 3...")만 주면 모델이 book_id 대신 그 번호를 돌려준다
    # (실제로 재현됨). 각 줄에 book_id 값을 명시하고, 반드시 그 값을
    # 그대로 쓰라고 못박는다.
    return "\n".join(
        f"- book_id {c['book_id']}: {c['title']} - {c['author']} - {c['description']}"
        for c in candidates
    )


def _parse_match_basis(raw: Any) -> list[dict]:
    """모델이 준 match_basis를 정리한다. 형식이 틀리면 빈 배열로 폴백한다.

    label·detail이 둘 다 문자열인 항목만 남긴다 — 모델이 리스트가 아닌 걸
    주거나 항목에 다른 키를 섞어 보내도 카드 조립이 깨지지 않게 한다.
    """
    if not isinstance(raw, list):
        return []
    return [
        {"label": item["label"], "detail": item["detail"]}
        for item in raw
        if isinstance(item, dict)
        and isinstance(item.get("label"), str)
        and isinstance(item.get("detail"), str)
    ]


async def generate_cards(
    candidates: list[dict], spec: Spec
) -> tuple[list[dict], str | None, bool]:
    """3단계 — 후보 중에서 골라 카드를 만든다.

    명세대로 reason_short·reason_long·match_basis·말풍선 reply를 한 번의
    LLM 호출로 만든다(이슈 #139, #158 — reply를 위해 새 호출을 늘리지 않고
    카드 생성 호출에 얹는다). match_basis는 모델이 형식을 안 지키면 빈
    배열로 폴백하듯(_parse_match_basis), reply도 문자열이 아니면 None으로
    폴백한다 — candidates가 비어 호출 자체가 없었을 때와 같은 신호라,
    호출부(chat())가 이 경우들을 규칙 기반 문구로 채운다. LLM 장애 시
    degraded로 match_score 상위 카드를 대신 돌려준다(명세: reason_short는
    규칙, reason_long은 null. #129).

    돌려주는 튜플은 (cards, reply, degraded) 세 값이다.
    """
    if not candidates:
        return [], None, False

    id_by_book = {c["book_id"]: c for c in candidates}
    # 프롬프트 채우기 → 모델 호출 → 답에서 글자 꺼내기 → JSON 읽기, 네 칸을 잇는다.
    chain = CARD_PROMPT | get_chat_model() | message_text | parse_json_response

    try:
        parsed = await invoke_chain(
            chain,
            {
                "intro": _card_prompt_intro(spec),
                "limit": CARD_LIMIT,
                "listing": _format_listing(candidates),
            },
        )
    except LLMUnavailableError:
        logger.exception("카드 생성 실패")
        return _rule_based_cards(candidates), None, True

    raw_cards = parsed.get("cards")
    if not isinstance(raw_cards, list):
        # 명세를 어긴 모양(cards가 null·문자열 등)을 그냥 슬라이스하면 TypeError가
        # 나 라우터의 어떤 try/except도 안 거치고 그대로 샌다(#201에서 실제로 확인).
        # LLM 장애와 똑같이 degraded로 규칙 기반 카드를 대신 낸다(#203) — 원문은
        # 안 남기고 타입만 로그에 남긴다(#202처럼 대화 내용을 로그에 남기지 않는다).
        logger.error(
            "카드 생성 응답의 cards가 배열이 아님: %s", type(raw_cards).__name__
        )
        return _rule_based_cards(candidates), None, True

    reply = parsed.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = None

    cards = []
    for rank, item in enumerate(raw_cards[:CARD_LIMIT], start=1):
        if not isinstance(item, dict):
            # 개별 항목이 문자열 등 dict가 아니면 그 카드만 건너뛴다(#203) — 다른
            # 항목이 멀쩡하면 그것까지 통째로 degraded 처리할 이유는 없다.
            continue
        book_id = item.get("book_id")
        book = id_by_book.get(book_id)
        reason_short = item.get("reason_short")
        if book is None or not reason_short:
            # 명세: 한 줄 이유를 못 만든 책은 카드에서 뺀다.
            continue
        cards.append(
            {
                "book_id": book_id,
                "rank": rank,
                "match_score": book["match_score"],
                "title": book["title"],
                "author": book["author"],
                "price": book.get("price"),
                "cover_url": book.get("cover_url"),
                "reason_short": reason_short,
                "reason_long": item.get("reason_long"),
                "match_basis": _parse_match_basis(item.get("match_basis")),
            }
        )
    return cards, reply, False


# 되묻기 1단계(#283) — 카드가 없을 때 followup을 상황에 맞는 질문으로 만든다. LLM은 더 안 부른다.
_CLARIFY_CATEGORIES = "소설, 에세이, 인문, 경제경영, 자기계발, 과학"
_CLARIFY_EXAMPLE = '"경제경영 책 추천해줘"'
_CLARIFY_ECHO_MAX_CHARS = 20


def _won(amount: int) -> str:
    return f"{amount // 10000}만원" if amount % 10000 == 0 else f"{amount:,}원"


def _active_conditions(spec: Spec) -> list[tuple[str, str]]:
    """걸려 있는 조건을 (사용자에게 보일 말, 풀어 달라고 할 때 따라 말할 예시)로 돌려준다."""
    f = spec.filters
    found: list[tuple[str, str]] = []
    if f.price_max is not None:
        found.append((f"{_won(f.price_max)} 이하", "가격은 상관없어요"))
    if f.price_min:  # 0은 조건이 아니다. LLM이 "저렴한"에 0을 넣는 경우가 있다.
        found.append((f"{_won(f.price_min)} 이상", "가격은 상관없어요"))
    if f.pub_year_from is not None:
        found.append((f"{f.pub_year_from}년 이후", "출간연도는 상관없어요"))
    if f.pub_year_to is not None:
        found.append((f"{f.pub_year_to}년 이전", "출간연도는 상관없어요"))
    if f.category:
        found.append((f.category, "분야는 상관없어요"))
    # 재고 조건은 짚지 않는다. 챗봇은 이 조건을 쓰지 않는다(#319).
    if spec.exact.publisher:
        found.append((f"{spec.exact.publisher} 책", "출판사는 상관없어요"))
    return found


def _no_card_response(
    spec: Spec,
    message: str,
    *,
    had_candidates: bool,
    slot_question: str | None = None,
) -> tuple[str, str]:
    """카드가 없을 때의 (reply, followup). reply는 짧은 안내, followup은 사용자가 답할 질문이다.

    검색어가 비었으면(조건이 걸려 있어도) 주제부터 묻는다 — 조건을 풀어도 찾을 게 없다.
    검색어가 있고 걸린 조건이 있으면 그 조건을 풀지 묻고(따라 말할 예시를 준다), 검색어는
    있는데 못 찾았으면 제목·저자나 분야를 묻는다. 다음 턴의 답은 1단계(spec 갱신)가 그대로
    받는다. 답 예시는 한 단어가 아니라 문장으로 줘서 사용자가 문장으로 답하게 한다.
    """
    if had_candidates:
        # 후보는 있었는데 카드를 못 만들었다(카드 생성이 비어 돌아옴). 조건 탓이 아니다.
        return (
            "추천을 만들지 못했어요.",
            "같은 요청을 한 번만 다시 말씀해 주시겠어요?",
        )
    if slot_question:
        # "아이한테 읽어줄 책"에서 LLM이 지어낸 조건 대신, 정말 빠진 정보(나이)를 묻는다.
        return "조건에 맞는 책을 아직 못 찾았어요.", slot_question
    conditions = _active_conditions(spec)
    names = ", ".join(name for name, _ in conditions)
    if not _query_text(spec):
        held = f" (지금 걸린 조건: {names})" if conditions else ""
        return (
            "추천해 드리려면 조금 더 알아야 해요.",
            (
                f"어떤 분야가 좋으세요? {_CLARIFY_CATEGORIES} 중에서 골라 "
                f"{_CLARIFY_EXAMPLE}처럼 말씀해 주시거나, 알고 있는 제목·저자를 알려 주세요.{held}"
            ),
        )
    if conditions:
        examples = " / ".join(
            f'"{example}"' for example in dict.fromkeys(e for _, e in conditions)
        )
        return (
            "조건에 맞는 책을 아직 못 찾았어요.",
            f"{names} 조건이 걸려 있어요. 조건을 넓혀서 다시 찾아볼까요? 예) {examples}",
        )
    said = message.strip()
    head = (
        f'"{said}"만으로는 찾기 어려워요.'
        if said and len(said) <= _CLARIFY_ECHO_MAX_CHARS
        else "말씀하신 내용만으로는 찾기 어려워요."
    )
    return (
        "조건에 맞는 책을 아직 못 찾았어요.",
        f"{head} 제목이나 저자를 알려 주시거나, 분야({_CLARIFY_CATEGORIES}) 중 하나를 골라 주세요.",
    )


# 되묻기 2단계(#283) — 모호한 요청은 규칙으로 가려 낸다. LLM은 더 안 부른다.
# 1) 답할 수 없는 요청(가리킬 책이 없는 지시어, 알아들을 수 없는 입력)은 검색·추천을 건너뛰고
#    cards: [] + followup으로 되묻는다(명세: 카드가 없을 때 followup).
# 2) 답은 할 수 있지만 정보가 빠진 요청(선물, 아이, 저렴한, 최근)은 첫 턴에만 추천을 주고
#    reply 끝에 질문 한 줄을 붙인다. 명세의 reply 최대 200자는 지킨다.
_REPLY_MAX_CHARS = 200
# 지시어는 낱말 앞머리에서만 잡는다. "학생이거든요", "어린이 책", "아까운"처럼 다른 낱말 속에 든 글자는 지시어가 아니다.
_REFERENCE = re.compile(
    r"((?<![가-힣])(이거|그거|저거|이것(?!저것)|그것|저것|[이그저] 책)|아까(?![운워웠우])|방금|조금 전|위에서|앞에서"
    r"|[첫두세네]\s?번째|\d+번째|\d+번 책)"
)
_ONLY_JAMO = re.compile(r"^[\sㄱ-ㅎㅏ-ㅣ!?.~,;]+$")
_DIGIT = re.compile(r"\d")
_YEAR_WORD = re.compile(r"(\d{4}\s*년|올해|작년|재작년|금년)")
_RECIPIENT = re.compile(
    r"(엄마|아빠|어머니|아버지|부모|친구|연인|남자친구|여자친구|남편|아내|동료|상사"
    r"|선생|할머니|할아버지|형|누나|동생|언니|오빠|딸|아들)"
)
_AGE_GIVEN = re.compile(r"(\d+\s*(세|살|학년)|유치원|초등|중학|고등)")
# (이름, 모호하다고 보는 말, 이미 구체적이라 묻지 않는 말, 질문)
_SLOT_RULES = (
    (
        "age",
        # "아이슬란드", "아이유", "아이스크림"처럼 이어지는 낱말은 거르려고, 뒤에 한글이 없거나 조사·책이 올 때만 잡는다.
        re.compile(
            r"(?<![가-힣])(아이|아기|애기|어린이|유아|꼬마)"
            r"(?=$|[^가-힣]|가|는|은|를|을|도|한테|에게|에|랑|와|과|용|들|의|께|만|로|책)"
        ),
        _AGE_GIVEN,
        '아이가 몇 살쯤인가요? 나이를 알려 주시면 더 맞게 골라 드려요. 예) "5세", "초등 저학년"',
    ),
    (
        "gift",
        re.compile(r"선물"),
        _RECIPIENT,
        "받는 분이 어떤 분인가요? 나이, 취향, 예산을 알려 주시면 더 맞게 골라 드려요.",
    ),
    (
        "budget",
        re.compile(r"(저렴|가성비|값싼|(?<![가-힣])(싼|싸게))"),
        _DIGIT,
        '예산은 어느 정도로 생각하세요? 예) "1만원 이하"',
    ),
    (
        "recent",
        # "요즘"은 "요새"라는 뜻으로 더 많이 쓰여서("요즘 우울한데") 신간 의도로 보지 않는다.
        re.compile(r"(최근|신간|새로\s?나온)"),
        _YEAR_WORD,
        '"최근"은 어느 정도까지 보시나요? 예) "작년 이후", "2024년 이후"',
    ),
)


def _unanswerable_request(
    message: str, spec: Spec, recent_turns: list[Turn]
) -> tuple[str, str] | None:
    """검색·추천을 해 봐야 소용없는 요청의 (reply, followup). 해당 없으면 None."""
    text = message.strip()
    if _ONLY_JAMO.match(text):
        return (
            "무슨 말씀인지 잘 모르겠어요.",
            '찾고 싶은 책이나 읽고 싶은 분위기를 다시 말씀해 주시겠어요? 예) "잔잔한 소설 추천해줘"',
        )
    if not recent_turns and spec.anchor_book is None and _REFERENCE.search(text):
        # 첫 턴이라 "이거", "그 책"이 가리킬 책이 없다. 서버는 대화를 저장하지 않아 요청에 실린 것만 안다.
        return (
            "어떤 책을 말씀하시는 건가요?",
            "책 제목을 알려 주시면 그 책을 기준으로 찾아 드릴게요.",
        )
    return None


def _slot_question(message: str) -> str | None:
    """추천은 할 수 있지만 빠진 정보가 있는 요청이면 물을 질문 한 줄. 여럿이어도 하나만 묻는다."""
    for _, trigger, specific, question in _SLOT_RULES:
        if trigger.search(message) and not specific.search(message):
            return question
    return None


def _append_question(reply: str, question: str) -> str:
    """reply 끝에 질문을 붙인다. 200자를 넘으면 reply의 뒷 문장부터 덜어 내고, 그래도 안 되면 안 붙인다."""
    sentences = re.split(r"(?<=[.!?])\s+", reply.strip())
    while sentences:
        combined = " ".join(sentences) + "\n\n" + question
        if len(combined) <= _REPLY_MAX_CHARS:
            return combined
        sentences = sentences[:-1]
    return reply


@router.post("/chat")
async def chat(request: Request) -> JSONResponse:
    raw = await body.read_limited(request, MAX_BODY_BYTES)
    if raw is None:
        return responses.error(413, "payload_too_large")

    try:
        payload = json.loads(raw)
    except ValueError:
        return responses.error(400, "invalid_request")

    req = parse_request(payload)
    if isinstance(req, tuple):
        status, message = req
        return responses.error(status, message)

    unanswerable = _unanswerable_request(req.message, req.spec, req.recent_turns)
    if unanswerable:
        # LLM도 검색도 부르지 않는다. spec은 요청에 실린 그대로 돌려준다(6키 계약 유지).
        reply, followup = unanswerable
        return responses.success(
            "recommend_success",
            {
                "reply": reply,
                "spec": req.spec.model_dump(),
                "recognition": None,
                "cards": [],
                "followup": followup,
                "buttons": [],
                "degraded": False,
            },
        )

    spec, spec_degraded = await update_spec(req.message, req.spec, req.recent_turns)
    try:
        candidates = await get_candidates(spec, req.exclude_book_ids, req.user_id)
    except Exception:
        # 그대로 두면 FastAPI 기본 500(text/plain)이 나가 공통 응답 형식이 깨진다.
        logger.exception("후보 검색 실패")
        return responses.error(500, "internal_server_error")

    if spec_degraded:
        # 명세: 1단계가 실패하면 3단계(카드 생성)도 건너뛴다. 점수 상위
        # CARD_LIMIT권을 규칙으로 낸다(#129).
        cards, llm_reply, degraded = _rule_based_cards(candidates), None, True
    else:
        cards, llm_reply, degraded = await generate_cards(candidates, spec)

    # reply 우선순위(#158): LLM 장애 시엔 고정 안내, 정상 호출이면 그 턴의
    # 추천을 반영한 LLM reply, 카드가 있는데 reply만 비면 최후 폴백, 카드
    # 자체가 없으면(검색 결과 없음 등 — 이때는 generate_cards가 LLM을 아예
    # 안 부르므로 llm_reply도 없다) "골라봤어요"가 어색해 못 찾음 안내로 바꾼다.
    # 첫 턴에만 묻는다. 이어지는 턴은 사용자가 이미 답하는 중이라 같은 걸 또 묻지 않는다.
    slot_question = None if req.recent_turns else _slot_question(req.message)
    no_card_reply, followup = None, None
    if not cards:
        no_card_reply, followup = _no_card_response(
            spec,
            req.message,
            had_candidates=bool(candidates),
            slot_question=slot_question,
        )

    if degraded:
        reply = "지금은 추천이 어려워요. 조건에 맞는 책을 찾아볼게요."
    elif llm_reply:
        reply = llm_reply
    elif cards:
        reply = "골라봤어요."
    else:
        reply = no_card_reply

    if cards and slot_question and not degraded:
        reply = _append_question(reply, slot_question)

    return responses.success(
        "recommend_success",
        {
            "reply": reply,
            "spec": spec.model_dump(),
            "recognition": None,
            "cards": cards,
            "followup": followup,
            "buttons": [],
            "degraded": degraded,
        },
    )
