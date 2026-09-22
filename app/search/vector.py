"""벡터 검색 — 검색어와 뜻이 가까운 책을 찾는다.

소개글이 있는 책만 book_embeddings 에 있으므로, 없는 책은 여기서 나오지 않고
키워드 검색으로만 찾힌다.
"""

import asyncpg

from app.core.pgvector import to_vector_literal
from app.search.filters import build_where
from app.search.schemas import SearchFilters

# 키워드 결과 뒤에 붙일 후보 수. 키워드 쪽(keyword.CANDIDATE_LIMIT)보다 작게 둔다.
# 벡터 검색은 관련이 없어도 "그나마 가까운 책"을 무조건 채워 주기 때문에,
# 길게 받을수록 뒤쪽은 잡음이다.
CANDIDATE_LIMIT = 50

# 색인(HNSW)은 근사 검색이라 탐색 폭(ef_search)이 좁으면 정답을 놓친다. 기본값은 40 이다.
# 이 모델은 모든 책의 유사도가 0.8~0.9 에 빽빽이 몰려 있어 특히 놓치기 쉽다.
# 13만 권에서 정확 검색과 비교하면 50 → 67%, 400 → 92%, 1000 → 97% 를 찾았고
# 1000 이어도 10ms 였다. 그래서 pgvector 가 허용하는 최댓값을 쓴다.
EF_SEARCH = 1000

# iterative_scan 은 필터에 걸러져 후보가 모자라면 더 살펴보게 하는 pgvector 0.8 기능이다.
# relaxed_order 는 빠른 대신 순서가 살짝 어긋날 수 있어, 바깥에서 거리로 다시 줄 세운다.
_SQL = """
WITH nearest AS MATERIALIZED (
    SELECT e.book_id, e.embedding <=> $1::vector AS distance
    FROM book_embeddings e
    JOIN v_books b USING (book_id)
    WHERE true{where}
    ORDER BY distance
    LIMIT $2
)
SELECT book_id FROM nearest ORDER BY distance, book_id
"""


async def search_ids(
    conn: asyncpg.Connection,
    query_vector: list[float],
    filters: SearchFilters,
    limit: int = CANDIDATE_LIMIT,
) -> list[int]:
    """뜻이 가까운 순서대로 book_id 를 돌려준다."""
    where, filter_params = build_where(filters, first_param=3)
    # SET LOCAL 은 트랜잭션 안에서만 먹고, 끝나면 연결이 원래 설정으로 돌아간다.
    async with conn.transaction():
        await conn.execute(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}")
        await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        rows = await conn.fetch(
            _SQL.format(where=where),
            to_vector_literal(query_vector),
            limit,
            *filter_params,
        )
    return [r["book_id"] for r in rows]


async def has_any(conn: asyncpg.Connection) -> bool:
    """책 벡터가 한 권이라도 있는가. 초기 적재 전이면 벡터 검색을 못 하는 상태다."""
    return await conn.fetchval("SELECT EXISTS (SELECT 1 FROM book_embeddings)")
