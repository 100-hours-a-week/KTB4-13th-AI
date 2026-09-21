"""① /search 의 검색 흐름: 키워드 검색 + 벡터 검색 → 순위 합치기."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.core import db
from app.gateway import embedding
from app.search import books, keyword, rrf, vector
from app.search.schemas import SearchRequest

logger = logging.getLogger(__name__)

KEYWORD_ONLY = "keyword-only"

# 합칠 때 키워드 쪽 등수를 3배로 쳐 준다. 키워드 결과는 낱말이 실제로 들어 있는 책이라 믿을 만하고,
# 벡터 결과는 관련이 없어도 "그나마 가까운 책"이 항상 채워지기 때문이다.
# 1:1 이면 제목을 정확히 쳐도 벡터 쪽 1등과 자리를 다퉈 밀린다(고정 검색어 세트로 확인, 측정 기록은 이슈 #35 코멘트).
KEYWORD_WEIGHT = 3.0
VECTOR_WEIGHT = 1.0


@dataclass
class SearchOutcome:
    results: list[dict[str, Any]]
    # 기능을 줄여 응답했으면 그 이름(X-Degraded 헤더 값). 온전하면 None.
    degraded: str | None


async def _embed_query(query: str) -> list[float] | None:
    """검색어를 벡터로 바꾼다. 실패하면 None — 검색은 키워드만으로 계속한다."""
    try:
        vectors, _, _ = await embedding.embed([query], "query")
    except Exception:
        logger.exception("검색어 임베딩 실패. 키워드 검색만으로 응답한다")
        return None
    return vectors[0]


async def _vector_ids(
    conn: asyncpg.Connection, query_vector: list[float] | None, req: SearchRequest
) -> list[int] | None:
    """벡터 검색 결과. 벡터 검색을 할 수 없는 상태면 None."""
    if query_vector is None:
        return None
    try:
        ids = await vector.search_ids(conn, query_vector, req.filters)
        # 0건이 필터 때문인지, 책 벡터가 아직 없어서인지 가른다(명세: 초기 적재 중이면 축소 응답).
        if not ids and not await vector.has_any(conn):
            return None
    except Exception:
        # 예외 종류를 좁히지 않는다. 벡터 검색이 어떤 이유로 못 되든 할 일은 같다 —
        # 키워드 결과만 돌려주고 헤더로 알린다(명세 ①). 좁게 잡으면 빠지는 게 생긴다:
        # asyncpg.InterfaceError(연결 끊김)와 TimeoutError(command_timeout 초과)는
        # PostgresError 하위가 아니라서, 키워드 결과가 멀쩡한데도 요청 전체가 500이 됐다.
        # 우리 쪽 버그로 여기 들어와도 로그에 스택이 남고 X-Degraded 비율로 드러난다.
        logger.exception("벡터 검색 실패. 키워드 검색만으로 응답한다")
        return None
    return ids


async def search(req: SearchRequest) -> SearchOutcome:
    async with db.get_pool().acquire() as conn:
        # 임베딩은 CPU, 키워드 검색은 DB 를 기다리는 일이라 동시에 돌린다.
        # 키워드 검색이 실패하면 예외가 그대로 올라가 500 이 된다(DB 가 죽은 상태).
        query_vector, keyword_ids = await asyncio.gather(
            _embed_query(req.query),
            keyword.search_ids(conn, req.query, req.filters),
        )
        vector_ids = await _vector_ids(conn, query_vector, req)

        if vector_ids is None:
            ranked, degraded = keyword_ids, KEYWORD_ONLY
        else:
            ranked, degraded = (
                rrf.fuse([keyword_ids, vector_ids], [KEYWORD_WEIGHT, VECTOR_WEIGHT]),
                None,
            )

        results = await books.fetch(conn, ranked[: req.size])
    return SearchOutcome(results=results, degraded=degraded)
