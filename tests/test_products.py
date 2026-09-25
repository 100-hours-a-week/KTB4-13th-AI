"""책의 가격·재고를 상품 표에서 읽는 식 테스트. 실제 PostgreSQL 이 있어야 돈다."""

import asyncio
import os

import asyncpg
import pytest

from app.core import products

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

# (상품 번호, book_id, 할인가, 재고 수량)
_PRODUCTS = [
    (9200001, 9100001, 12000.00, 999999),  # 상품 하나
    (9200002, 9100002, 9000.00, 0),  # 상품 하나, 재고 없음
    # 상품 둘: 더 싸지만 재고가 없는 것보다 재고 있는 것을 고른다
    (9200003, 9100003, 8000.00, 0),
    (9200004, 9100003, 15000.00, 3),
    # 상품 둘, 둘 다 재고 있음: 더 싼 것
    (9200005, 9100004, 20000.00, 5),
    (9200006, 9100004, 18000.00, 5),
]
_BOOKS = (9100001, 9100002, 9100003, 9100004, 9100005)  # 9100005 는 상품이 없는 책


def _values() -> dict[int, tuple[int | None, bool]]:
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id in _BOOKS:
                # v_books.price·in_stock 은 읽지 않는다. 일부러 틀린 값을 넣는다.
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, price, in_stock)"
                    " VALUES ($1, '테스트', 0, false)",
                    book_id,
                )
            await conn.executemany(
                "INSERT INTO v_products (id, book_id, discounted_price, stock_quantity)"
                " VALUES ($1, $2, $3, $4)",
                _PRODUCTS,
            )
            rows = await conn.fetch(
                f"SELECT b.book_id, {products.price_sql('b')} AS price,"
                f" {products.in_stock_sql('b')} AS in_stock"
                " FROM v_books b WHERE b.book_id = ANY($1::int[])",
                list(_BOOKS),
            )
            return {r["book_id"]: (r["price"], r["in_stock"]) for r in rows}
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


@needs_db
def test_가격과_재고는_상품_표에서_읽는다() -> None:
    values = _values()

    assert values[9100001] == (12000, True)
    assert values[9100002] == (9000, False)


@needs_db
def test_상품이_없는_책은_가격이_없고_품절이다() -> None:
    assert _values()[9100005] == (None, False)


@needs_db
def test_상품이_여럿이면_재고_있는_것_중_가장_싼_것이다() -> None:
    values = _values()

    # 가격과 재고가 같은 상품에서 나온다(8000원 품절 상품이 아님)
    assert values[9100003] == (15000, True)
    assert values[9100004] == (18000, True)
