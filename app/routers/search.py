"""① AI 검색: POST /search

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #search
LLM을 쓰지 않는다. 지금은 뼈대만 있다. 실제 키워드+벡터 RRF 결합은
담당자가 후속 이슈에서 채운다.
"""

from typing import Any

from fastapi import APIRouter
from pydantic import ValidationError

from app.search.schemas import SearchRequest

router = APIRouter(tags=["search"])


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
async def search():
    return {"message": "search_success", "data": None}
