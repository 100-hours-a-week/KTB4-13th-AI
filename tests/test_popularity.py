"""인기 점수 테스트. 값을 확인하는 쪽은 실제 PostgreSQL 이 있어야 돈다."""

import asyncio
import os

import asyncpg
import pytest

from app.core import popularity

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


# (book_id, 판매 수, 평점, 리뷰 수). 9100005 는 인기 행이 없는 책이다.
_ROWS = [
    (9100001, 100, 1.0, 1000),  # 많이 팔렸지만 평점이 낮은 책
    (9100002, 10, 5.0, 1000),  # 덜 팔렸지만 평점이 높은 책
    (9100003, 10, None, 0),  # 판매 수가 같고 리뷰는 없는 책
    (9100004, 0, None, 0),  # 판매도 리뷰도 없음
]


def _scores() -> dict[int, float]:
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("DELETE FROM v_book_popularity")
            for book_id in (*[r[0] for r in _ROWS], 9100005):
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, price, in_stock)"
                    " VALUES ($1, '테스트', 10000, true)",
                    book_id,
                )
            await conn.executemany(
                "INSERT INTO v_book_popularity VALUES ($1, $2, $3, $4, now())", _ROWS
            )
            rows = await conn.fetch(
                f"SELECT b.book_id, {popularity.score_sql('p')} AS score"
                " FROM v_books b LEFT JOIN v_book_popularity p USING (book_id)"
                " WHERE b.book_id BETWEEN 9100001 AND 9100005"
            )
            return {r["book_id"]: r["score"] for r in rows}
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


@needs_db
def test_판매_수가_곧_점수다() -> None:
    scores = _scores()

    assert scores[9100001] == 100
    # 평점은 쓰지 않는다(백엔드 MVP 가 판매량 기준, BE #68).
    assert scores[9100002] == scores[9100003]


@needs_db
def test_순서는_판매_수_순서와_같다() -> None:
    scores = _scores()

    # 평점이 낮아도 많이 팔린 책이 앞이다.
    assert scores[9100001] > scores[9100002] > scores[9100004]


@needs_db
def test_판매가_없거나_행이_없는_책은_0점이다() -> None:
    scores = _scores()

    assert scores[9100004] == 0
    assert scores[9100005] == 0
