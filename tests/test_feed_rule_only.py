"""④ 벡터를 못 쓸 때의 목록 테스트 — 카테고리·인기 점수로만 채점. 실제 PostgreSQL 이 있어야 돈다.

넣은 데이터는 트랜잭션을 되돌려 흔적을 남기지 않는다. 카탈로그의 다른 책보다 앞에 오도록
출간연도를 2099 로 둔다(인기 집계가 비어 있으면 인기·신간순 후보는 신간순이다).
"""

import asyncio
import os
from datetime import UTC, datetime

import asyncpg
import pytest

from app.feed import rule_only
from app.feed.cursor import Page
from app.feed.schemas import parse_query

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER = 9_100_801
_LIKED = "규칙점수좋아함"
_OTHER = "규칙점수다른분류"
_T0 = datetime(2026, 9, 1, tzinfo=UTC)
_NOW = datetime(2026, 9, 30, tzinfo=UTC)

# (book_id, 분류, 출간연도)
_BOOKS = [
    (9100801, _OTHER, 2099),  # 인기·신간순으로는 맨 앞이지만 좋아하는 분류가 아니다
    (9100802, _LIKED, 2098),
    (9100803, _LIKED, 2097),
    (9100804, _LIKED, 2099),  # 산 책 — 빠진다
]


def _run(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id, category, year in _BOOKS:
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, author, publisher, price,"
                    " in_stock, cover_url, category, pub_year, description)"
                    " VALUES ($1, '책', '저자', '출판사', 10000, true, NULL, $2, $3, '소개')",
                    book_id,
                    category,
                    year,
                )
            await conn.execute(
                "INSERT INTO v_user_purchases VALUES ($1, 9100804, $2)", _USER, _T0
            )
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


def _request(**params):
    return parse_query(
        [("user_id", str(_USER)), ("surface", "recommend_more"), *params.items()]
    )


def _fetch(req, tag_weights):
    async def check(conn):
        items, _ = await rule_only.fetch(conn, req, Page(issued_at=_NOW), tag_weights)
        return [(item["book_id"], item["match_score"]) for item in items]

    return _run(check)


def test_좋아하는_분류의_책이_카테고리_점수로_앞에_온다() -> None:
    # 원점수 4 는 0–1 로 펴면 1.0 이라 카테고리 항(0.25)을 다 받는다. 유사도 항은 없다.
    got = _fetch(_request(size="3"), {_LIKED: 4})

    assert got == [(9100802, 25), (9100803, 25), (9100801, 0)]


def test_점수_하한을_지킨다() -> None:
    got = _fetch(_request(match_score_min="20"), {_LIKED: 4})

    assert [book_id for book_id, _ in got] == [9100802, 9100803]


def test_유사도_항이_빠져_점수가_40을_넘지_않는다() -> None:
    # 비중을 다시 1 로 키우지 않는다(명세: 축소 응답도 같은 잣대).
    assert _fetch(_request(match_score_min="41"), {_LIKED: 4}) == []


def test_산_책은_빠진다() -> None:
    got = _fetch(_request(), {_LIKED: 4})

    assert 9100804 not in [book_id for book_id, _ in got]


def test_점수가_0_이하인_분류는_좋아하는_분류로_치지_않는다() -> None:
    got = _fetch(_request(size="3"), {_LIKED: -2, _OTHER: 0})

    # 카테고리 점수가 없으면 인기·신간순 후보만 남고 모두 0점이다.
    assert got[0] == (9100801, 0)
    assert all(score == 0 for _, score in got)
