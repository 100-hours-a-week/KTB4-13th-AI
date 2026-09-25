"""④ cold_start 목록 테스트 — 순서, 제외, 필터. 실제 PostgreSQL 이 있어야 돈다.

로컬 카탈로그 책과 섞이지 않게 테스트 책은 모두 이 파일만 쓰는 카테고리에 넣고 그 카테고리로 거른다.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.feed import cold_start
from app.feed.cursor import Page
from app.feed.schemas import parse_query

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER = 9100601
_CATEGORY = "피드테스트분류"
_T0 = datetime(2026, 9, 1, tzinfo=UTC)
# 커서를 받은 시각. 위 이력보다 뒤라 제외가 그대로 걸린다.
_NOW = datetime(2026, 9, 30, tzinfo=UTC)

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


def _fetch(params, user_id: int = _USER, page=None):
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
            items, has_more = await cold_start.fetch(
                conn, req, page or Page(issued_at=_NOW)
            )
            return [item["book_id"] for item in items], has_more
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


def _list(params, user_id: int = _USER, page=None) -> list[int]:
    return _fetch(params, user_id, page)[0]


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


def test_다음_페이지는_이어서_준다() -> None:
    첫쪽, 더_있나 = _fetch([("size", "2")])
    둘째쪽, _ = _fetch([("size", "2")], page=Page(offset=2, issued_at=_NOW))

    assert 더_있나 is True
    assert 첫쪽 == [9100604, 9100601]
    assert 둘째쪽 == [9100602, 9100608]


def test_마지막_페이지면_더_없다고_알린다() -> None:
    _, 더_있나 = _fetch([("size", "50")])

    assert 더_있나 is False


def test_커서를_받은_뒤에_생긴_이력은_제외하지_않는다() -> None:
    # 카드를 보고 돌아와 산 책이 다음 페이지에서 사라지면 목록이 한 칸씩 밀린다(명세 ④).
    이전 = Page(issued_at=_T0 - timedelta(days=1))

    assert 9100605 in _list([], page=이전)


def test_500권에서_끝난다(monkeypatch: pytest.MonkeyPatch) -> None:
    # 쪽을 OFFSET 으로 넘겨서 상한이 없으면 깊은 쪽일수록 느려진다. 개인화 목록과 같게 끝낸다(#189).
    monkeypatch.setattr(cold_start, "MAX_BOOKS", 3)

    첫쪽, 첫쪽_더 = _fetch([("size", "2")])
    둘째쪽, 둘째쪽_더 = _fetch([("size", "2")], page=Page(offset=2, issued_at=_NOW))
    셋째쪽, 셋째쪽_더 = _fetch([("size", "2")], page=Page(offset=4, issued_at=_NOW))

    assert (첫쪽, 첫쪽_더) == ([9100604, 9100601], True)
    assert (둘째쪽, 둘째쪽_더) == ([9100602], False)
    assert (셋째쪽, 셋째쪽_더) == ([], False)


def test_최신순에서_같은_연도는_번호순이다() -> None:
    # 연도 안을 인기순으로 세우면 매번 그 연도 전체를 읽어야 한다(#186). 판매 수와 상관없이 번호순.
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id in (9100611, 9100612):
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, price, in_stock, category, pub_year)"
                    " VALUES ($1, '책', 10000, true, '피드테스트같은연도', 2099)",
                    book_id,
                )
            await conn.execute(
                "INSERT INTO v_book_popularity VALUES (9100612, 9999, NULL, 0, now())"
            )
            req = parse_query(
                [
                    ("user_id", str(_USER)),
                    ("surface", "recommend_more"),
                    ("category", "피드테스트같은연도"),
                    ("sort", "newest"),
                ]
            )
            items, _ = await cold_start.fetch(conn, req, Page(issued_at=_NOW))
            return [item["book_id"] for item in items]
        finally:
            await tx.rollback()
            await conn.close()

    assert asyncio.run(_go()) == [9100611, 9100612]


@pytest.mark.parametrize("sort", ["match", "newest", "price_asc"])
def test_책_표를_통째로_훑지_않는다(sort: str) -> None:
    # 통째로 읽고 정렬하면 261만 권에서 한 쪽에 1초가 넘는다(#186). 색인 순서대로 앞부분만 읽는다.
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        try:
            req = parse_query(
                [("user_id", str(_USER)), ("surface", "recommend_more"), ("sort", sort)]
            )
            where = ""
            if sort == "match":
                sql = cold_start.popular_first_sql(where, limit="$4::int + $5::int")
            else:
                sql = cold_start._SIMPLE_SQL.format(
                    where=where, order_by=cold_start._ORDER_BY[req.sort]
                )
            rows = await cold_start.run_ordered(
                conn,
                "EXPLAIN " + sql + "LIMIT $4 OFFSET $5",
                req.user_id,
                2.0,
                _NOW,
                16,
                0,
            )
            return "\n".join(r[0] for r in rows)
        finally:
            await conn.close()

    plan = asyncio.run(_go())

    assert "Seq Scan on v_books" not in plan
