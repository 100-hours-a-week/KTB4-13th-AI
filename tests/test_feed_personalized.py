"""④ 개인화 목록 테스트 — 채점·정렬·제외. 실제 PostgreSQL(pgvector 포함)이 있어야 돈다.

넣은 데이터는 트랜잭션을 되돌려 흔적을 남기지 않는다. 카탈로그의 다른 책과 섞이지 않게
이 파일만 쓰는 카테고리로 거른다.
"""

import asyncio
import os
from datetime import UTC, datetime

import asyncpg
import pytest

from app.core.pgvector import to_vector_literal
from app.feed import personalized
from app.feed.schemas import parse_query

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER = 9_100_701
_CATEGORY = "피드채점분류"
_T0 = datetime(2026, 9, 1, tzinfo=UTC)
DIM = 384


def _vector(first: float) -> list[float]:
    """첫 칸만 다른 단위 벡터. 취향 벡터와의 유사도를 원하는 값으로 만든다."""
    rest = (1 - first**2) ** 0.5
    vector = [0.0] * DIM
    vector[0] = first
    vector[1] = rest
    return vector


# (book_id, 유사도, 가격, 출간연도). 취향 벡터는 _vector(1.0) 이라 첫 칸이 곧 유사도다.
_BOOKS = [
    (9100701, 0.95, 20000, 2020),  # 유사도 만점
    (9100702, 0.90, 15000, 2024),  # 절반
    (9100703, 0.85, 10000, 2022),  # 0점
    (9100704, 0.93, 30000, 2021),  # 산 책 — 빠진다
]
_CENTROID = _vector(1.0)


def _run(check, *, sort: str = "match", extra: list[tuple[str, str]] | None = None):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id, similarity, price, year in _BOOKS:
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, author, publisher, price,"
                    " in_stock, cover_url, category, pub_year, description)"
                    " VALUES ($1, '책', '저자', '출판사', $2, true, NULL, $3, $4, '소개')",
                    book_id,
                    price,
                    _CATEGORY,
                    year,
                )
                await conn.execute(
                    "INSERT INTO book_embeddings VALUES ($1, $2::vector, $3, 'test')",
                    book_id,
                    to_vector_literal(_vector(similarity)),
                    DIM,
                )
            await conn.execute(
                "INSERT INTO v_user_purchases VALUES ($1, 9100704, $2)", _USER, _T0
            )
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


def _request(**params):
    return parse_query(
        [
            ("user_id", str(_USER)),
            ("surface", "recommend_more"),
            ("category", _CATEGORY),
            *params.items(),
        ]
    )


def _fetch(req, tag_weights=None):
    async def check(conn):
        return await personalized.fetch(conn, req, _CENTROID, tag_weights or {})

    return _run(check)


def test_유사도가_높은_책이_먼저고_점수가_함께_나간다() -> None:
    items = _fetch(_request())

    assert [item["book_id"] for item in items] == [9100701, 9100702, 9100703]
    # 인기 집계와 카테고리 점수가 없으면 유사도 항만 남는다(0.6 × 100).
    assert [item["match_score"] for item in items] == [60, 30, 0]


def test_산_책은_빠진다() -> None:
    assert 9100704 not in [item["book_id"] for item in _fetch(_request())]


def test_프로필의_카테고리_점수가_더해진다() -> None:
    # 원점수 4 는 0–1 로 펴면 1.0 이라 카테고리 항(0.25)을 다 받는다.
    items = _fetch(_request(), tag_weights={_CATEGORY: 4})

    assert [item["match_score"] for item in items] == [85, 55, 25]


def test_점수_하한보다_낮은_책은_뺀다() -> None:
    items = _fetch(_request(match_score_min="50"))

    assert [item["book_id"] for item in items] == [9100701]


def test_최신순과_가격순은_후보_안에서_다시_줄_세운다() -> None:
    newest = _fetch(_request(sort="newest"))
    cheapest = _fetch(_request(sort="price_asc"))

    assert [item["book_id"] for item in newest] == [9100702, 9100703, 9100701]
    assert [item["book_id"] for item in cheapest] == [9100703, 9100702, 9100701]


def test_size_만큼만_준다() -> None:
    assert len(_fetch(_request(size="2"))) == 2


def test_응답에는_명세의_칸만_나간다() -> None:
    item = _fetch(_request())[0]

    assert set(item) == {
        "book_id",
        "title",
        "author",
        "price",
        "cover_url",
        "in_stock",
        "match_score",
    }
