"""인기 점수 테스트. 값을 확인하는 쪽은 실제 PostgreSQL 이 있어야 돈다."""

import asyncio
import math
import os

import asyncpg
import pytest

from app.core import popularity

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


def test_전체_평균을_식에_한_번만_넣는다() -> None:
    # 두 번 넣으면 같은 값인데도 DB 가 v_book_popularity 전체 집계를 쿼리마다 두 번 돈다.
    assert popularity.score_sql().count("FROM v_book_popularity") == 1


# (book_id, 판매 수, 평점, 리뷰 수). 9100005 는 인기 행이 없는 책이다.
_ROWS = [
    (9100001, 100, 4.0, 1000),  # 전체 평균을 4.0 근처로 잡아 주는 책
    (9100002, 100, 1.0, 2),  # 별점 테러: 리뷰 2개가 모두 1점
    (9100003, 100, 1.0, 1000),  # 리뷰가 많은 진짜 낮은 평점
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
def test_식대로_계산한다() -> None:
    mean = (4.0 * 1000 + 1.0 * 2 + 1.0 * 1000) / 2002
    adjusted = (popularity.PRIOR_REVIEWS * mean + 4.0 * 1000) / (
        popularity.PRIOR_REVIEWS + 1000
    )

    assert _scores()[9100001] == pytest.approx(math.log(101) + adjusted - mean)


@needs_db
def test_리뷰_몇_개의_별점_테러는_리뷰_많은_낮은_평점보다_덜_깎인다() -> None:
    scores = _scores()

    assert scores[9100002] > scores[9100003]
    # 평점 항만 떼어 보면(점수 − ln(1 + 판매 수)) 1점짜리 리뷰 2개로는 평균에서 0.5점도 못 깎는다.
    # 리뷰 1000개가 1점이면 평균(약 2.5)에서 거의 그대로 1.5점이 깎인다.
    assert scores[9100002] - math.log(101) > -0.5
    assert scores[9100003] - math.log(101) < -1.4


@needs_db
def test_판매도_리뷰도_없으면_0점이다() -> None:
    assert _scores()[9100004] == pytest.approx(0.0)


@needs_db
def test_인기_행이_없는_책은_0점이다() -> None:
    assert _scores()[9100005] == 0
