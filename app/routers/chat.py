"""③ 대화형 도서 추천: POST /recommendations/chat

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #chat
텍스트 턴과 이미지 턴을 여기서 함께 다룬다 — 같은 핸들러가 후보검색·카드생성
파이프라인을 공유하므로 나누지 않는다(개발 워크플로 위키 §9 참고).

처리를 명세 4단계 그대로 함수로 나눴다. 1·3단계는 LLM 체인이고, 2단계는 아직
최소 구현이다(아래 각 함수 docstring 참고). 바깥 흐름(이 4단계가 이 순서로
불리는 것)은 안 바뀐다.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from langchain_core.prompts import ChatPromptTemplate
from pydantic import ValidationError

from app.chat.schemas import ChatRequest, Spec
from app.core import db, responses
from app.gateway.llm import (
    LLMUnavailableError,
    get_chat_model,
    invoke_chain,
    message_text,
    parse_json_response,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

CANDIDATE_LIMIT = 10
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
# 아래 예시 JSON의 값(null, false 등)은 모양만 보여주는 자리 표시다 — 실제로는
# "지금 조건"의 값을 그대로 옮겨 적어야 한다. 처음에는 예시의 false를 그대로
# 베껴서 in_stock_only=true였던 값이 매번 false로 바뀌는 문제가 있었다
# (실제 Ollama/qwen2.5:7b로 재현). "예시일 뿐, 값을 베끼지 마라"를 못박아 고쳤다.
SPEC_PROMPT = ChatPromptTemplate.from_template(
    "너는 책 추천 챗봇의 조건(spec) 갱신기다. 지금까지의 조건에 이번 메시지 "
    "내용만 반영해 갱신하라. 이번 메시지가 언급하지 않은 값은 지금 조건의 값을 "
    "한 글자도 안 바꾸고 그대로 옮겨 적어라. 특히 true/false 같은 값도 지금 "
    "조건에 있는 그대로 옮겨야 한다 — 언급 안 됐다고 false로 바꾸면 안 된다.\n\n"
    "지금 조건(spec):\n{spec_json}\n\n"
    '이번 메시지: "{message}"\n\n'
    "intent는 아래 둘 중 하나다.\n"
    "- exact: 제목이나 저자를 콕 집어 말함\n"
    "- semantic: 분위기나 상황을 말함(기본값)\n\n"
    "반드시 아래 JSON 형식 그대로, 6개 키를 모두 채워 답하라. 값이 없으면 "
    "null이나 빈 배열/빈 객체로 채우고 키 자체를 빼지 마라. 숫자는 따옴표 "
    "없이 써라. 아래 null·false는 형식 예시일 뿐 실제 값이 아니다 — 그대로 "
    "베끼지 말고 '지금 조건'과 '이번 메시지'를 보고 채워라:\n"
    '{{"intent": "exact 또는 semantic", '
    '"exact": {{"title": null, "author": null, "publisher": null}}, '
    '"filters": {{"category": null, "price_min": null, "price_max": null, '
    '"pub_year_from": null, "pub_year_to": null, "in_stock_only": false}}, '
    '"semantic": null, "anchor_book": null, "exclude": []}}'
)


async def update_spec(message: str, spec: Spec) -> tuple[Spec, bool]:
    """1단계 — 메시지로 spec을 갱신한다. 명세대로 LLM이 한다.

    실패(LLM 장애, 또는 spec 모양이 아닌 응답)하면 원래 spec을 그대로 돌려주고
    두 번째 값을 True로 준다. 명세: "LLM 장애면 1과 3을 건너뛰고 요청의 spec
    으로 2만 돌려 점수 상위 3권을 낸다" — 호출부(chat())가 이 신호를 보고
    3단계(카드 생성)를 건너뛴다.
    """
    chain = SPEC_PROMPT | get_chat_model() | message_text | parse_json_response
    try:
        parsed = await invoke_chain(
            chain,
            {
                "spec_json": json.dumps(spec.model_dump(), ensure_ascii=False),
                "message": message,
            },
        )
        return Spec.model_validate(parsed), False
    except (LLMUnavailableError, ValidationError):
        logger.exception("spec 갱신 실패")
        return spec, True


async def get_candidates(spec: Spec, exclude_book_ids: list[int]) -> list[dict]:
    """2단계 — spec으로 후보를 뽑는다.

    명세는 키워드+벡터 순위 결합과 취향 유사도 점수를 요구한다. book_embeddings가
    아직 비어 있어 벡터 검색을 못 쓰므로, 지금은 description이 있는 책을 그냥
    가져오는 최소 구현이다. ①이 끝나면 그쪽 하이브리드 검색 함수로 교체하고,
    ⑥이 끝나면 취향 유사도·이력 제외를 더한다.
    """
    exclude = exclude_book_ids + spec.exclude
    pool = db.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT book_id, title, author, description
            FROM v_books
            WHERE description IS NOT NULL
              AND book_id != ALL($1::int[])
            LIMIT $2
            """,
            exclude or [],
            CANDIDATE_LIMIT,
        )
    return [dict(row) for row in rows]


# {semantic}·{limit}·{listing}이 채워지는 자리다. JSON 예시의 중괄호는 자리 표시로
# 오해받지 않게 {{ }}로 겹쳐 쓴다 — 실제로 모델에 가는 글자는 겹치기 전과 같다.
CARD_PROMPT = ChatPromptTemplate.from_template(
    '사용자가 원하는 책 분위기: "{semantic}"\n\n'
    "아래 책 목록 중 이 분위기에 어울리는 책을 최대 {limit}권 골라라.\n"
    "반드시 한국어로만 답하라. 다른 언어를 섞지 마라.\n"
    "book_id는 반드시 아래 목록에 적힌 값을 그대로 써라. 순서 번호가 아니다.\n"
    '다음 JSON 형식으로만 답하라: {{"cards": [{{"book_id": 정수, '
    '"reason_short": "한 줄 이유(80자 이내)", "reason_long": "긴 이유(2-4문장)"}}]}}\n\n'
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


async def generate_cards(candidates: list[dict], spec: Spec) -> tuple[list[dict], bool]:
    """3단계 — 후보 중에서 골라 카드를 만든다.

    명세는 reason_short·reason_long·match_basis를 한 번의 LLM 호출로 만들라고
    한다. 지금은 match_basis 없이 reason_short·reason_long만 만드는 최소
    구현이다. LLM 장애 시 degraded로 빈 카드를 돌려준다(명세 5단계 축소판 —
    규칙 기반 대체는 아직 없음, 후속 이슈).

    돌려주는 튜플의 두 번째 값이 degraded 여부다.
    """
    if not candidates:
        return [], False

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
        return [], True

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
                "price": None,
                "cover_url": None,
                "reason_short": reason_short,
                "reason_long": item.get("reason_long"),
                "match_basis": [],
            }
        )
    return cards, False


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

    spec, spec_degraded = await update_spec(req.message, req.spec)
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
        cards, degraded = [], True
    else:
        cards, degraded = await generate_cards(candidates, spec)

    return responses.success(
        "recommend_success",
        {
            "reply": "" if degraded else "골라봤어요.",
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
