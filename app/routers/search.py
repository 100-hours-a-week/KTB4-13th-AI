"""① AI 검색: POST /search

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #search
LLM을 쓰지 않는다. 키워드 검색과 벡터 검색의 순위를 합쳐 돌려준다.
정렬, 커서는 후속 이슈에서 붙인다.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core import responses
from app.search import service
from app.search.schemas import SearchRequest

logger = logging.getLogger(__name__)

router = APIRouter(tags=["search"])

FALLBACK_MESSAGE = "원하는 책을 못 찾았어요. AI 추천에게 물어볼까요?"


def parse_request(payload: Any) -> SearchRequest | None:
    """계약에 맞으면 요청 객체, 아니면 None 을 돌려준다."""
    if not isinstance(payload, dict):
        return None
    try:
        req = SearchRequest.model_validate(payload)
    except ValidationError:
        return None
    # 공백뿐인 검색어는 1자 이상이라는 조건을 글자 수로만 통과한다.
    if not req.query.strip():
        return None
    return req


@router.post("/search")
async def search(request: Request) -> JSONResponse:
    try:
        payload = json.loads(await request.body())
    except ValueError:
        # JSON 문법 오류와, UTF-8 이 아닌 본문(UnicodeDecodeError) 둘 다 ValueError 다.
        # JSONDecodeError 만 잡으면 뒤의 것이 500 으로 샌다.
        return responses.error(400, "invalid_request")

    req = parse_request(payload)
    if req is None:
        return responses.error(400, "invalid_request")

    try:
        outcome = await service.search(req)
    except Exception:
        # 그대로 두면 FastAPI 기본 500 {"detail": ...} 이 나가 공통 응답 형식이 깨진다.
        logger.exception("검색 실패")
        return responses.error(500, "internal_server_error")

    # 0건은 오류가 아니라 200 + 안내 문구다(명세 ①).
    fallback = None if outcome.results else {"message": FALLBACK_MESSAGE}
    headers = {"X-Degraded": outcome.degraded} if outcome.degraded else None
    return responses.success(
        "search_success",
        {"results": outcome.results, "next_cursor": None, "fallback": fallback},
        headers=headers,
    )
