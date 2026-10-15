"""④ 홈 — 저장된 관심사별 목록에서 한 쪽 읽기(app/feed/interest_page.py) 테스트(#454).

"실제 DB" 절의 테스트만 PostgreSQL 이 필요하다(SEARCH_TEST_DATABASE_URL).
"""

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from app.feed import exclusion, interest_page
from app.feed.cursor import Page
from app.feed.schemas import parse_query

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_NOW = datetime(2026, 10, 16, tzinfo=UTC)

# --- 읽을 판 고르기 ---

_NEW = [{"book_id": 2, "interest": "SF", "match_score": 70}]
_OLD = [{"book_id": 1, "interest": "요리", "match_score": 80}]


@pytest.mark.parametrize(
    ("cursor_version", "expected"),
    [
        (None, (_NEW, 5)),  # 첫 페이지는 지금 판
        (5, (_NEW, 5)),
        (4, (_OLD, 4)),  # 스크롤하는 중에 한 번 다시 뽑혔으면 앞 판
        (3, (_NEW, 5)),  # 두 번 넘게 다시 뽑혔으면 지금 판으로 이어 읽는다
    ],
)
def test_커서가_읽던_판을_고른다(cursor_version: int | None, expected) -> None:
    saved = interest_page.Stored(_NEW, 5, _OLD)

    assert saved.at(cursor_version) == expected


# --- 실제 DB ---

_needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)
_MIGRATION = (
    Path(__file__).parent.parent
    / "db"
    / "migrations"
    / "20261016_taste_profile_interests.sql"
)
_USER = 9_100_002
# book_id → (제목, 저자)
_BOOKS = {
    9100501: ("첫 책", "가"),
    9100502: ("가진 책", "나"),
    9100503: ("가진 책", "나"),  # 9100502 의 다른 판본
    9100504: ("넷째 책", "라"),
    9100505: ("다섯째 책", "마"),
}


def _isbn(book_id: int) -> str:
    return str(9780000000000 + book_id)


async def _with_books(check):
    conn = await asyncpg.connect(_DB_URL)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
        for book_id, (title, author) in _BOOKS.items():
            await conn.execute(
                "INSERT INTO v_books (book_id, title, author, publisher, price,"
                " in_stock, cover_url, category, pub_year, description, isbn13)"
                " VALUES ($1, $2, $3, '출판사', 10000, true, NULL, '에세이', 2024, '소개', $4)",
                book_id,
                title,
                author,
                _isbn(book_id),
            )
        return await check(conn)
    finally:
        await tx.rollback()
        await conn.close()


def _home(**params):
    return parse_query([("user_id", str(_USER)), ("surface", "home"), *params.items()])


_SAVED = [
    {"book_id": book_id, "interest": "에세이", "match_score": 90 - n}
    for n, book_id in enumerate(_BOOKS)
]


@_needs_db
def test_가진_책과_그_다른_판본을_빼고_저장된_순서와_점수로_한_쪽을_읽는다() -> None:
    req = _home(purchased_isbns=_isbn(9100502), size="2")

    async def check(conn):
        excluded = exclusion.owned([9100502])
        first = await interest_page.fetch(
            conn, req, Page(issued_at=_NOW), _SAVED, excluded
        )
        second = await interest_page.fetch(
            conn, req, Page(offset=2, issued_at=_NOW), _SAVED, excluded
        )
        return first, second

    (first, more), (second, more_after) = asyncio.run(_with_books(check))

    assert [(i["book_id"], i["match_score"]) for i in first] == [
        (9100501, 90),
        (9100504, 87),
    ]
    assert more is True
    assert [i["book_id"] for i in second] == [9100505]
    assert more_after is False
    assert set(first[0]) == {
        "book_id",
        "isbn",
        "title",
        "author",
        "price",
        "cover_url",
        "in_stock",
        "match_score",
    }


@_needs_db
def test_저장된_목록과_앞_판을_읽고_없으면_None이다() -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO taste_profile (user_id, cold_start, profile_version, feed,"
            " feed_version, feed_prev) VALUES ($1, false, 1, $2::jsonb, 3, $3::jsonb)",
            _USER,
            json.dumps({"books": _NEW, "substituted": []}),
            json.dumps({"books": _OLD, "substituted": []}),
        )
        return (
            await interest_page.stored(conn, _USER),
            await interest_page.stored(conn, _USER + 1),
        )

    saved, missing = asyncio.run(_with_books(check))

    assert saved == interest_page.Stored(_NEW, 3, _OLD)
    assert missing is None
