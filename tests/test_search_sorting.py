"""정렬 테스트. 실제 PostgreSQL 이 있어야 돈다."""

import asyncio
import os

import asyncpg
import pytest

from app.search import sorting

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

# (book_id, 가격, 출간연도)
_BOOKS = [
    (9100001, 15000, 2020),
    (9100002, 9000, 2024),
    (9100003, 15000, None),
    (9100004, 30000, 2024),
]
# (book_id, 판매 수). 9100003·9100004 는 인기 행이 없다.
_SALES = [(9100001, 10), (9100002, 5000)]

# 관련도 순서라고 치고 넘기는 순서
_BY_RELEVANCE = [9100001, 9100002, 9100003, 9100004]


def _sort(sort: str, without_product: tuple[int, ...] = ()) -> list[int]:
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id, price, year in _BOOKS:
                # 책 표의 가격은 읽지 않는다(#206). 일부러 0 을 넣는다.
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, price, in_stock, pub_year)"
                    " VALUES ($1, '테스트', 0, false, $2)",
                    book_id,
                    year,
                )
                if book_id not in without_product:
                    await conn.execute(
                        "INSERT INTO v_products"
                        " (id, book_id, discounted_price, stock_quantity)"
                        " VALUES ($1, $2, $3, 1)",
                        book_id,
                        book_id,
                        price,
                    )
            for book_id, sales in _SALES:
                await conn.execute(
                    "INSERT INTO v_book_popularity VALUES ($1, $2, NULL, 0, now())",
                    book_id,
                    sales,
                )
            return await sorting.sort_ids(conn, _BY_RELEVANCE, sort)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


def test_가격_낮은순이고_같은_가격이면_관련도_순서를_지킨다() -> None:
    assert _sort("price_asc") == [9100002, 9100001, 9100003, 9100004]


def test_가격_높은순() -> None:
    assert _sort("price_desc") == [9100004, 9100001, 9100003, 9100002]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("price_asc", [9100002, 9100003, 9100004, 9100001]),
        ("price_desc", [9100004, 9100003, 9100002, 9100001]),
    ],
)
def test_상품이_없어_가격이_없는_책은_가격순에서_맨_뒤다(
    sort: str, expected: list[int]
) -> None:
    assert _sort(sort, without_product=(9100001,)) == expected


def test_최신순이고_출간연도가_없는_책은_맨_뒤다() -> None:
    assert _sort("newest") == [9100002, 9100004, 9100001, 9100003]


def test_인기순이고_인기_행이_없는_책은_0점이라_뒤로_간다() -> None:
    assert _sort("popular") == [9100002, 9100001, 9100003, 9100004]


def test_빈_목록은_DB를_부르지_않고_빈_목록이다() -> None:
    assert asyncio.run(sorting.sort_ids(None, [], "newest")) == []
