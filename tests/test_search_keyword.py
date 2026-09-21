"""키워드 검색 테스트.

앞부분은 DB 없이 돈다. 뒷부분은 실제 PostgreSQL(pg_trgm·색인 포함)이 있어야 해서
SEARCH_TEST_DATABASE_URL 에 DB 주소를 주면 돈다. 넣은 책은 트랜잭션을 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import os

import asyncpg
import pytest

from app.search import books, keyword
from app.search.filters import build_where
from app.search.schemas import SearchFilters


def test_띄어쓰기로_나누고_같은_낱말은_한_번만_쓴다() -> None:
    assert keyword.tokenize("  김영하  여행의 이유 김영하 ") == [
        "김영하",
        "여행의",
        "이유",
    ]


def test_낱말은_MAX_TOKENS_개까지만_쓴다() -> None:
    query = " ".join(f"낱말{i}" for i in range(30))

    assert len(keyword.tokenize(query)) == keyword.MAX_TOKENS


def test_낱말_속_와일드카드_글자를_막는다() -> None:
    assert keyword.like_pattern("100%_") == "%100\\%\\_%"


def test_필터가_없으면_조건도_없다() -> None:
    assert build_where(SearchFilters(), first_param=5) == ("", [])


def test_필터는_값을_자리표시자로_넘긴다() -> None:
    filters = SearchFilters(category="에세이", price_max=20000, in_stock_only=True)

    sql, params = build_where(filters, first_param=5)

    assert sql == " AND b.category = $5 AND b.price <= $6 AND b.in_stock"
    assert params == ["에세이", 20000]


# ---------------------------------------------------------------------------
# 실제 DB 가 필요한 테스트
# ---------------------------------------------------------------------------

# DATABASE_URL 을 그대로 쓰지 않는 이유: conftest 가 .env 없는 환경을 위해 가짜 주소를 넣어 두고,
# 환경변수가 .env 보다 우선이라 로컬에서도 가짜 주소가 읽힌다.
_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

# 실제 카탈로그에 없을 낱말로 책을 만들어, 다른 책이 결과에 끼어들지 않게 한다.
_BOOKS = [
    (9100001, "즈믄가람", "온새미 지음", 12000, True, "에세이", 2024, None),
    (9100002, "즈믄가람 이야기 모음", "다른이", 30000, True, "소설", 2010, None),
    (9100003, "딴 제목", "즈믄가람", 15000, False, "에세이", 2022, None),
    (
        9100004,
        "또 다른 제목",
        "아무개",
        9000,
        True,
        "에세이",
        None,
        "즈믄가람 에 대한 소개글",
    ),
]


def _run_in_rollback(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.executemany(
                "INSERT INTO v_books (book_id, title, author, publisher, price, in_stock,"
                " cover_url, category, pub_year, description)"
                " VALUES ($1, $2, $3, '테스트출판사', $4, $5, NULL, $6, $7, $8)",
                _BOOKS,
            )
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


@needs_db
def test_제목이_정확히_같은_책이_맨_위고_제목_다음_저자_순이다() -> None:
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가람", SearchFilters()))

    assert ids == [9100001, 9100002, 9100003]


@needs_db
def test_소개글에서만_맞은_책은_키워드_결과에_넣지_않는다() -> None:
    # 그런 책은 뜻으로 찾는 벡터 검색의 몫이다. 넣으면 약하게 맞은 책이 결과를 채운다.
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가람", SearchFilters()))

    assert 9100004 not in ids


@needs_db
def test_낱말이_일부만_맞은_책은_넣지_않는다() -> None:
    ids = _run_in_rollback(
        lambda c: keyword.search_ids(
            c, "즈믄가람 없는말 또없는말 다른말", SearchFilters()
        )
    )

    assert ids == []


@needs_db
def test_오타가_있어도_찾는다() -> None:
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가랑", SearchFilters()))

    assert 9100001 in ids


@needs_db
def test_제목과_저자를_섞어_쳐도_둘_다_맞는_책이_위다() -> None:
    ids = _run_in_rollback(
        lambda c: keyword.search_ids(c, "온새미 즈믄가람", SearchFilters())
    )

    assert ids[0] == 9100001


@needs_db
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (SearchFilters(category="소설"), [9100002]),
        (SearchFilters(price_min=10000, price_max=20000), [9100001, 9100003]),
        (SearchFilters(pub_year_from=2020), [9100001, 9100003]),
        (SearchFilters(in_stock_only=True), [9100001, 9100002]),
    ],
)
def test_필터를_적용한다(filters: SearchFilters, expected: list[int]) -> None:
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가람", filters))

    assert ids == expected


@needs_db
def test_책_정보는_받은_순서대로_돌려주고_없는_책은_빠진다() -> None:
    rows = _run_in_rollback(lambda c: books.fetch(c, [9100003, 123456789, 9100001]))

    assert [r["book_id"] for r in rows] == [9100003, 9100001]
    assert rows[0] == {
        "book_id": 9100003,
        "title": "딴 제목",
        "author": "즈믄가람",
        "publisher": "테스트출판사",
        "price": 15000,
        "in_stock": False,
        "cover_url": None,
    }
