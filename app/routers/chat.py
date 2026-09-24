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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from langchain_core.prompts import ChatPromptTemplate
from pydantic import ValidationError

from app.chat.schemas import MAX_RECENT_TURNS, ChatRequest, Spec, Turn
from app.core import db, responses
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

# 출판사 비교용 잡음 — "(주)"·"주식회사"·공백. 카탈로그 표기가 "(주)현암사",
# "현암주니어 :현암사"처럼 들쭉날쭉해, 이걸 지우고 포함 관계로 봐야 같은
# 출판사가 표기 차이로 조용히 빠지지 않는다(리뷰 지적).
_PUBLISHER_NOISE = re.compile(r"\(주\)|주식회사|\s+")


def _normalize_publisher(name: str) -> str:
    return _PUBLISHER_NOISE.sub("", name)


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
# "너무 무겁지 않은 걸로" 같은 분위기·톤 조정 표현은 제목·저자·가격·장르·재고·
# 제외 어디에도 안 맞아 지시문이 없으면 그냥 버려진다(실제 Ollama/qwen2.5:7b로
# 재현, 이슈 #132). exact·filters와 달리 semantic은 문장 하나짜리라 하위 키
# patch가 안 되고, 반영하려면 지금 문장을 바탕으로 전체를 다시 써야 한다 —
# 그래서 "언제 semantic을 통째로 다시 쓰는지"를 별도로 못박아둔다.
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
    "exclude는 이번에 새로 빼고 싶은 책 id만 넣어라 — 기존 목록은 서버가 "
    "그대로 유지하니 다시 적을 필요 없다.\n\n"
    "이번 메시지가 제목·저자·가격·장르·재고·제외처럼 구체적인 조건이 아니라 "
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
    '{{"semantic": "비 오는 날 읽을 잔잔한 책", '
    '"filters": {{"in_stock_only": true}}}}\n\n'
    "톤 조정 예시(형식일 뿐 실제 값 아님) — 지금 semantic이 "
    '"비 오는 날 읽을 책"이고 메시지가 "너무 무겁지 않은 걸로"면:\n'
    '{{"semantic": "비 오는 날 읽을 무겁지 않은 책"}}\n\n'
    "조건+톤 혼합 예시(형식일 뿐 실제 값 아님) — 지금 semantic이 "
    '"비 오는 날 읽을 책"이고 메시지가 "가벼운 걸로, 1만원 이하로"면 '
    "둘 다 넣어라:\n"
    '{{"semantic": "비 오는 날 읽을 가벼운 책", '
    '"filters": {{"price_max": 10000}}}}'
)


def _format_recent_turns(turns: list[Turn]) -> str:
    """최근 대화를 "user: …" 줄로 바꾼다. 없으면 빈 대화임을 알리는 문구.

    명세: 최대 20턴, 초과분은 최근 20턴만 사용 — 뒤에서 MAX_RECENT_TURNS개만 자른다.
    """
    if not turns:
        return "(최근 대화 없음)"
    return "\n".join(f"{t.role}: {t.text}" for t in turns[-MAX_RECENT_TURNS:])


def _merge_spec_patch(current: Spec, patch: dict) -> dict:
    """이번 턴에 LLM이 바꾼 값만 담긴 patch를 지금 spec 위에 겹쳐 완전한 spec dict를 만든다.

    patch에 없는 키(하위 키 포함)는 지금 값을 그대로 두고, 있는 키만
    덮어쓴다. exclude는 겹쳐 쓰지 않고 더한다 — 모델이 옛 항목을 안 실어도
    사라지지 않는다.
    """
    merged = current.model_dump()
    for key in ("exact", "filters"):
        sub_patch = patch.get(key)
        if isinstance(sub_patch, dict):
            merged[key] = {**merged[key], **sub_patch}

    new_exclude = patch.get("exclude")
    if isinstance(new_exclude, list):
        merged["exclude"] = merged["exclude"] + [
            book_id for book_id in new_exclude if book_id not in merged["exclude"]
        ]

    for key in ("intent", "semantic", "anchor_book"):
        if key in patch:
            merged[key] = patch[key]

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
    except (LLMUnavailableError, ValidationError):
        logger.exception("spec 갱신 실패")
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


async def get_candidates(spec: Spec, exclude_book_ids: list[int]) -> list[dict]:
    """2단계 — spec으로 후보를 뽑는다. ①의 하이브리드 검색(제목 완전 일치 먼저, 나머지는 순위 합치기)을 쓴다.

    취향 유사도 점수(⑥ 의존)는 아직 없다 — match_score는 계속 null이다.
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

    outcome = await service.search(
        SearchRequest(query=query, filters=spec.filters, size=SEARCH_SIZE)
    )
    exclude = set(exclude_book_ids) | set(spec.exclude)
    pool = [r for r in outcome.results if r["book_id"] not in exclude]
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

    descriptions = await _fetch_descriptions([c["book_id"] for c in pool])
    for c in pool:
        c["description"] = descriptions.get(c["book_id"]) or ""

    if spec.intent == "semantic":
        pool = [c for c in pool if c["description"]]
    return pool[:CANDIDATE_LIMIT]


# {semantic}·{limit}·{listing}이 채워지는 자리다. JSON 예시의 중괄호는 자리 표시로
# 오해받지 않게 {{ }}로 겹쳐 쓴다 — 실제로 모델에 가는 글자는 겹치기 전과 같다.
CARD_PROMPT = ChatPromptTemplate.from_template(
    '사용자가 원하는 책 분위기: "{semantic}"\n\n'
    "아래 책 목록 중 이 분위기에 어울리는 책을 최대 {limit}권 골라라.\n"
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
    degraded로 빈 카드를 돌려준다(명세 5단계 축소판 — 규칙 기반 대체는 아직
    없음, 후속 이슈).

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
                "semantic": spec.semantic,
                "limit": CARD_LIMIT,
                "listing": _format_listing(candidates),
            },
        )
    except LLMUnavailableError:
        return [], None, True

    reply = parsed.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        reply = None

    cards = []
    for rank, item in enumerate(parsed.get("cards", [])[:CARD_LIMIT], start=1):
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
                "match_score": None,  # ②의 취향 스코어링이 붙기 전까지는 없음
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


@router.post("/chat")
async def chat(request: Request) -> JSONResponse:
    try:
        payload = json.loads(await request.body())
    except ValueError:
        return responses.error(400, "invalid_request")

    req = parse_request(payload)
    if isinstance(req, tuple):
        status, message = req
        return responses.error(status, message)

    spec, spec_degraded = await update_spec(req.message, req.spec, req.recent_turns)
    try:
        candidates = await get_candidates(spec, req.exclude_book_ids)
    except Exception:
        # 그대로 두면 FastAPI 기본 500(text/plain)이 나가 공통 응답 형식이 깨진다.
        logger.exception("후보 검색 실패")
        return responses.error(500, "internal_server_error")

    if spec_degraded:
        # 명세: 1단계가 실패하면 3단계(카드 생성)도 건너뛴다. 점수 상위 3권을
        # 규칙으로 채우는 건 candidates에 점수 자체가 아직 없어(⑥ 의존) 못
        # 한다 — 후속 이슈.
        cards, llm_reply, degraded = [], None, True
    else:
        cards, llm_reply, degraded = await generate_cards(candidates, spec)

    # reply 우선순위(#158): LLM 장애 시엔 고정 안내, 정상 호출이면 그 턴의
    # 추천을 반영한 LLM reply, 카드가 있는데 reply만 비면 최후 폴백, 카드
    # 자체가 없으면(검색 결과 없음 등 — 이때는 generate_cards가 LLM을 아예
    # 안 부르므로 llm_reply도 없다) "골라봤어요"가 어색해 못 찾음 안내로 바꾼다.
    if degraded:
        reply = "지금은 추천이 어려워요. 조건에 맞는 책을 찾아볼게요."
    elif llm_reply:
        reply = llm_reply
    elif cards:
        reply = "골라봤어요."
    else:
        reply = "조건에 맞는 책을 아직 못 찾았어요. 다른 조건으로 다시 찾아볼까요?"

    return responses.success(
        "recommend_success",
        {
            "reply": reply,
            "spec": spec.model_dump(),
            "recognition": None,
            "cards": cards,
            "followup": "어떤 책을 찾고 있는지 조금 더 말씀해 주시겠어요?"
            if not cards
            else None,
            "buttons": [],
            "degraded": degraded,
        },
    )
