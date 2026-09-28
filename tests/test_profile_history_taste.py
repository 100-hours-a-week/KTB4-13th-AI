"""이력만으로 만드는 임시 취향 테스트(#246). 실제 PostgreSQL(pgvector 포함)이 있어야 돈다.

SEARCH_TEST_DATABASE_URL 에 DB 주소를 주면 돈다. 넣은 데이터는 트랜잭션을 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest

from app.core.pgvector import to_vector_literal
from app.profile import history_taste

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER = 9_100_001
_T0 = datetime(2026, 9, 1, tzinfo=UTC)
DIM = 384


def _unit(i: int) -> list[float]:
    vector = [0.0] * DIM
    vector[i] = 1.0
    return vector


# 책마다 서로 다른 축을 가리키는 벡터를 준다. 취향 벡터가 어느 책 쪽으로 끌렸는지 바로 보인다.
# 9100303 은 소개글이 없어 벡터가 없는 책이다.
_BOOKS = {
    9100301: ("에세이", _unit(0)),
    9100302: ("한국소설", _unit(1)),
    9100303: ("경제학", None),
}


def _run(check, until: datetime | None = None):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id, (category, vector) in _BOOKS.items():
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, author, publisher, price,"
                    " in_stock, cover_url, category, pub_year, description)"
                    " VALUES ($1, '책', '저자', '출판사', 10000, true, NULL, $2, 2024, NULL)",
                    book_id,
                    category,
                )
                if vector is not None:
                    await conn.execute(
                        "INSERT INTO book_embeddings VALUES ($1, $2::vector, $3, 'test')",
                        book_id,
                        to_vector_literal(vector),
                        DIM,
                    )
            await check(conn)
            return await history_taste.from_history(conn, _USER, until)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


def test_구매와_좋은_리뷰를_같은_무게로_섞어_취향_벡터를_만든다() -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100301, $2)", _USER, _T0
        )
        await conn.execute(
            "INSERT INTO v_user_reviews VALUES ($1, 9100302, 4.5, $2)", _USER, _T0
        )

    taste = _run(check)

    # 구매(3) : 좋은 리뷰(2)
    assert taste.centroid[0] / taste.centroid[1] == pytest.approx(1.5, rel=1e-5)
    assert taste.tag_weights == {"에세이": 3, "한국소설": 2}


def test_싫다고_한_책은_벡터에서_빼고_카테고리_점수만_깎는다() -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100301, $2)", _USER, _T0
        )
        await conn.execute(
            "INSERT INTO v_user_reviews VALUES ($1, 9100302, 1.5, $2)", _USER, _T0
        )

    taste = _run(check)

    assert taste.centroid[1] == 0
    assert taste.tag_weights == {"에세이": 3, "한국소설": -2}


def test_이력이_없으면_벡터가_없다() -> None:
    async def check(conn):
        return None

    taste = _run(check)

    assert (taste.centroid, taste.tag_weights) == (None, {})


def test_벡터가_없는_책만_있으면_벡터는_없고_카테고리_점수는_남는다() -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100303, $2)", _USER, _T0
        )

    taste = _run(check)

    assert taste.centroid is None
    assert taste.tag_weights == {"경제학": 3}


def test_until_뒤의_이력은_쓰지_않는다() -> None:
    # ④ 는 커서를 받은 시각까지만 본다. 그 뒤에 산 책으로 목록이 밀리면 안 된다.
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100301, $2)", _USER, _T0
        )
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100302, $2)",
            _USER,
            _T0 + timedelta(days=2),
        )

    taste = _run(check, until=_T0 + timedelta(days=1))

    assert taste.centroid[1] == 0
    assert taste.tag_weights == {"에세이": 3}
