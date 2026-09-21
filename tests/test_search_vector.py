"""벡터 검색 테스트. 실제 PostgreSQL + pgvector 가 필요해 주소를 줄 때만 돈다."""

import asyncio
import os

import asyncpg
import pytest

from app.core.pgvector import to_vector_literal
from app.search import vector
from app.search.schemas import SearchFilters

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


def _unit(axis: int) -> list[float]:
    """한 축만 1 인 384차원 벡터. 축이 다르면 서로 가장 멀다."""
    v = [0.0] * 384
    v[axis] = 1.0
    return v


# DB 에 실제 책 벡터가 수만 건 있어도 섞이지 않게, 테스트 책만 가진 카테고리로 좁혀 검색한다.
# (책 벡터를 지웠다 되돌리는 방식은 건수가 많으면 느리고 죽은 행을 남긴다.)
_CATEGORY = "즈믄가람분류"

# (book_id, 가격, 재고, 벡터 축)
_BOOKS = [
    (9100001, 12000, True, 0),
    (9100002, 30000, True, 1),
    (9100003, 15000, False, 0),
]


def _run_in_rollback(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id, price, in_stock, axis in _BOOKS:
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, price, in_stock, category)"
                    " VALUES ($1, '테스트', $2, $3, $4)",
                    book_id,
                    price,
                    in_stock,
                    _CATEGORY,
                )
                await conn.execute(
                    "INSERT INTO book_embeddings VALUES ($1, $2::vector, 384, 'test')",
                    book_id,
                    to_vector_literal(_unit(axis)),
                )
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


def test_가까운_순서로_돌려준다() -> None:
    query = [0.0] * 384
    query[0], query[1] = 0.9, 0.1

    ids = _run_in_rollback(
        lambda c: vector.search_ids(c, query, SearchFilters(category=_CATEGORY))
    )

    assert ids == [9100001, 9100003, 9100002]


def test_필터를_적용한다() -> None:
    filters = SearchFilters(category=_CATEGORY, in_stock_only=True, price_max=20000)

    ids = _run_in_rollback(lambda c: vector.search_ids(c, _unit(0), filters))

    assert ids == [9100001]


def test_책_벡터가_있는지_알려준다() -> None:
    assert _run_in_rollback(vector.has_any) is True
