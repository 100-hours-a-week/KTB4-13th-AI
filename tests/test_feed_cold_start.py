"""④ cold_start 목록 테스트 — 순서, 제외, 필터. 실제 PostgreSQL 이 있어야 돈다.

로컬 카탈로그 책과 섞이지 않게 테스트 책은 모두 이 파일만 쓰는 카테고리에 넣고 그 카테고리로 거른다.
"""

import asyncio
import os
from datetime import UTC, datetime

import asyncpg
import pytest

from app.feed import cold_start
from app.feed.schemas import parse_query

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER = 9100601
_CATEGORY = "피드테스트분류"
_T0 = datetime(2026, 9, 1, tzinfo=UTC)

# (book_id, 가격, 출간연도)
_BOOKS = [
    (9100601, 15000, 2020),
    (9100602, 9000, 2024),
    (9100603, 12000, None),
    (9100604, 30000, 2022),
    (9100605, 11000, 2023),  # 산 책
    (9100606, 11000, 2023),  # 담은 책
    (9100607, 11000, 2023),  # 2.0점 리뷰
    (9100608, 11000, 2023),  # 4.5점 리뷰만 — 빼지 않는다
]
# (book_id, 판매 수). 나머지는 인기 행이 없어 0점이다.
_SALES = [(9100601, 10), (9100604, 5000)]


def _list(params: list[tuple[str, str]], user_id: int = _USER) -> list[int]:
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id, price, year in _BOOKS:
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, price, in_stock, category, pub_year)"
                    " VALUES ($1, '책', $2, true, $3, $4)",
                    book_id,
                    price,
                    _CATEGORY,
                    year,
                )
            for book_id, sales in _SALES:
                await conn.execute(
                    "INSERT INTO v_book_popularity VALUES ($1, $2, NULL, 0, now())",
                    book_id,
                    sales,
                )
            await conn.execute(
                "INSERT INTO v_user_purchases VALUES ($1, 9100605, $2)", _USER, _T0
            )
            await conn.execute(
                "INSERT INTO v_user_library VALUES ($1, 9100606, $2)", _USER, _T0
            )
            await conn.execute(
                "INSERT INTO v_user_reviews VALUES ($1, 9100607, 2.0, $2),"
                " ($1, 9100608, 4.5, $2)",
                _USER,
                _T0,
            )
            req = parse_query(
                [
                    ("user_id", str(user_id)),
                    ("surface", "recommend_more"),
                    ("category", _CATEGORY),
                    *params,
                ]
            )
            rows = await cold_start.fetch(conn, req)
            return [r["book_id"] for r in rows]
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


def test_기본은_인기순이고_인기_행이_없는_책은_신간순으로_뒤에_온다() -> None:
    assert _list([]) == [9100604, 9100601, 9100602, 9100608, 9100603]


def test_산_책_담은_책_2점_이하_리뷰_책은_빼고_좋은_리뷰만_단_책은_남긴다() -> None:
    ids = _list([])

    assert {9100605, 9100606, 9100607}.isdisjoint(ids)
    assert 9100608 in ids


def test_최신순은_출간연도가_없는_책을_맨_뒤로_보낸다() -> None:
    assert _list([("sort", "newest")]) == [9100602, 9100608, 9100604, 9100601, 9100603]


def test_가격순() -> None:
    assert _list([("sort", "price_asc")]) == [
        9100602,
        9100608,
        9100603,
        9100601,
        9100604,
    ]


def test_출간연도_필터를_걸면_연도가_없는_책은_빠진다() -> None:
    ids = _list([("pub_year_from", "2022"), ("pub_year_to", "2024")])

    assert ids == [9100604, 9100602, 9100608]


def test_size_만큼만_준다() -> None:
    assert _list([("size", "2")]) == [9100604, 9100601]


def test_다른_사용자의_이력으로는_빼지_않는다() -> None:
    # 이력은 _USER 의 것이다. 다른 사람의 목록에는 그 책들이 그대로 나온다.
    ids = _list([], user_id=_USER + 1)

    assert {9100605, 9100606, 9100607} <= set(ids)
