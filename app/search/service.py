"""① /search 의 검색 흐름. 지금은 키워드 검색만 있다."""

from dataclasses import dataclass
from typing import Any

from app.core import db
from app.search import books, keyword
from app.search.schemas import SearchRequest


@dataclass
class SearchOutcome:
    results: list[dict[str, Any]]
    # 기능을 줄여 응답했으면 그 이름(X-Degraded 헤더 값). 온전하면 None.
    degraded: str | None


async def search(req: SearchRequest) -> SearchOutcome:
    async with db.get_pool().acquire() as conn:
        ids = await keyword.search_ids(conn, req.query, req.filters)
        results = await books.fetch(conn, ids[: req.size])
    # 벡터 검색이 붙기 전이라 항상 키워드 결과만 나간다. 명세대로 그 사실을 알린다.
    return SearchOutcome(results=results, degraded="keyword-only")
