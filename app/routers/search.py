"""① AI 검색: POST /search

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #search
LLM을 쓰지 않는다. 지금은 뼈대만 있다. 실제 키워드+벡터 RRF 결합은
담당자가 후속 이슈에서 채운다.
"""

from fastapi import APIRouter

router = APIRouter(tags=["search"])


@router.post("/search")
async def search():
    return {"message": "search_success", "data": None}
