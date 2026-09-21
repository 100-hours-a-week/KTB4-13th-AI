"""① AI 검색: POST /search

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #search
LLM을 쓰지 않는다. 지금은 요청 검사와 응답 모양까지만 있고 항상 0건을 돌려준다.
키워드·벡터 검색, 정렬, 커서는 후속 이슈에서 붙인다.
"""

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core import responses
from app.search.schemas import SearchRequest

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

    # 검색이 붙기 전이라 항상 0건이다. 0건은 오류가 아니라 200 + 안내 문구다(명세 ①).
    return responses.success(
        "search_success",
        {"results": [], "next_cursor": None, "fallback": {"message": FALLBACK_MESSAGE}},
    )
