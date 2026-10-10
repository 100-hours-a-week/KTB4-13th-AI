"""이력 가중치 테스트. 앞부분은 DB 없이, 끝의 조회 테스트는 실제 PostgreSQL 이 있어야 돈다."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import asyncpg
import pytest

from app.core import history

_T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _row(
    book_id: int, kind: str, days: int, rating: str | None = None, category="에세이"
):
    return {
        "book_id": book_id,
        "kind": kind,
        "rating": Decimal(rating) if rating else None,
        "at": _T0 + timedelta(days=days),
        "category": category,
    }


@pytest.mark.parametrize(
    ("rating", "weight"),
    [
        ("5.0", 2),
        ("4.0", 2),
        ("3.5", 0),  # 명세의 정수 구간 사이. 중립으로 본다
        ("3.0", 0),
        ("2.5", 0),
        ("2.0", -2),
        ("0.5", -2),
    ],
)
def test_별점_경계(rating: str, weight: int) -> None:
    assert history.review_weight(Decimal(rating)) == weight


def test_구매_담기_리뷰에_명세의_가중치를_매긴다() -> None:
    h = history.summarize(
        [
            _row(1, "purchase", 0),
            _row(2, "review", 1, "4.5"),
            _row(3, "library", 2),
            _row(4, "review", 3, "3.0"),
            _row(5, "review", 4, "1.5"),
        ]
    )

    assert h.weights == {1: 3, 2: 2, 3: 1, 4: 0, 5: -2}


def test_같은_책이_여러_곳에_있으면_가장_큰_가중치_하나만_쓴다() -> None:
    # 담고(1), 사고(3), 재구매(3), 좋은 리뷰(2) — 더하면 9 지만 명세는 가장 큰 값 하나다.
    h = history.summarize(
        [
            _row(1, "library", 0),
            _row(1, "purchase", 1),
            _row(1, "purchase", 2),
            _row(1, "review", 3, "5.0"),
        ]
    )

    assert h.weights == {1: 3}
    assert h.category_scores == {"에세이": 3}


def test_2점_이하_리뷰를_단_책은_산_책이어도_싫어한_책으로_따로_남긴다() -> None:
    # 사고(3) 1점 리뷰(−2) — weights 는 명세대로 큰 값 3 이지만, 취향 벡터와 추천에서
    # 빼야 할 책이라는 사실은 disliked_book_ids 로 남는다.
    h = history.summarize(
        [
            _row(1, "purchase", 0),
            _row(1, "review", 1, "1.0"),
            _row(2, "review", 2, "2.0"),
            _row(3, "review", 3, "2.5"),  # 중립은 싫어한 책이 아니다
            _row(4, "purchase", 4),
        ]
    )

    assert h.weights[1] == 3
    assert h.disliked_book_ids == {1, 2}


def test_카테고리_점수는_책별_가중치를_더하고_비선호도_깎는다() -> None:
    h = history.summarize(
        [
            _row(1, "purchase", 0, category="에세이"),
            _row(2, "library", 1, category="에세이"),
            _row(3, "review", 2, "1.0", category="에세이"),
            _row(4, "purchase", 3, category="한국소설"),
            _row(5, "review", 4, "3.0", category="과학"),  # 0 은 칸을 만들지 않는다
            _row(6, "purchase", 5, category=None),  # 카탈로그에 없거나 카테고리가 빈 책
        ]
    )

    assert h.category_scores == {"에세이": 3 + 1 - 2, "한국소설": 3}


def test_computed_at은_읽은_이력의_최대_시각이고_가중치_0인_행도_넣는다() -> None:
    h = history.summarize(
        [
            _row(1, "purchase", 0),
            _row(2, "review", 9, "3.0"),  # 중립이지만 가장 늦은 행
            _row(3, "library", 5),
        ]
    )

    assert h.computed_at == _T0 + timedelta(days=9)


def test_이력이_없으면_computed_at은_None이다() -> None:
    h = history.summarize([])

    assert h.computed_at is None
    assert not h.any()
    assert h.weights == {}


# ---------------------------------------------------------------------------
# 요청으로 받은 이력(#327). 복제 표에서 읽은 행과 같은 모양으로 만들어 같은 계산에 넣는다.
# ---------------------------------------------------------------------------

_A, _B, _C = "9788936434120", "9788954651135", "9788937460449"
_IDS = {_A: 1, _B: 2}
_CATEGORIES = {1: "에세이", 2: "한국소설"}


def _reported(purchased=(), library=(), reviews=()) -> history.History:
    rows = history.reported_rows(
        _IDS, _CATEGORIES, list(purchased), list(library), list(reviews), _T0
    )
    return history.summarize(rows)


def test_요청으로_받은_이력에도_같은_가중치를_매긴다() -> None:
    h = _reported(purchased=[_A], library=[_B])

    assert h.weights == {1: 3, 2: 1}
    assert h.category_scores == {"에세이": 3, "한국소설": 1}


@pytest.mark.parametrize(
    ("rating", "weight"),
    [(5.0, 2), (4.0, 2), (4, 2), (3.5, 0), (2.5, 0), (2.0, -2), (0.5, -2), (0, -2)],
)
def test_요청의_별점은_숫자로_와도_같은_경계로_가른다(
    rating: float, weight: int
) -> None:
    assert _reported(reviews=[(_A, rating)]).weights == {1: weight}


def test_요청으로_받아도_같은_책은_가장_큰_가중치_하나만_쓴다() -> None:
    h = _reported(purchased=[_A], library=[_A], reviews=[(_A, 4.5)])

    assert h.weights == {1: 3}


def test_요청으로_받아도_2점_이하_리뷰를_단_책은_싫어한_책으로_남긴다() -> None:
    h = _reported(purchased=[_A], reviews=[(_A, 1.0)])

    assert h.weights == {1: 3}
    assert h.disliked_book_ids == {1}


def test_책_표에_없는_ISBN의_이력은_빠진다() -> None:
    h = _reported(purchased=[_A, _C], library=[_C], reviews=[(_C, 5.0)])

    assert h.weights == {1: 3}


# ---------------------------------------------------------------------------
# 실제 DB 가 필요한 테스트
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER = 9_100_001
_OTHER_USER = 9_100_002


@needs_db
def test_세_테이블을_user_id로_읽어_모은다() -> None:
    async def _go() -> history.History:
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.executemany(
                "INSERT INTO v_books (book_id, title, author, publisher, price, in_stock,"
                " cover_url, category, pub_year, description)"
                " VALUES ($1, $2, '저자', '출판사', 10000, true, NULL, $3, 2024, NULL)",
                [(9100101, "책1", "에세이"), (9100102, "책2", "한국소설")],
            )
            await conn.execute(
                "INSERT INTO v_user_purchases VALUES ($1, 9100101, $2)", _USER, _T0
            )
            await conn.execute(
                "INSERT INTO v_user_library VALUES ($1, 9100102, $2)",
                _USER,
                _T0 + timedelta(days=1),
            )
            await conn.execute(
                "INSERT INTO v_user_reviews VALUES ($1, 9100102, 4.5, $2)",
                _USER,
                _T0 + timedelta(days=2),
            )
            # 다른 사용자의 이력은 섞이지 않는다
            await conn.execute(
                "INSERT INTO v_user_purchases VALUES ($1, 9100102, $2)",
                _OTHER_USER,
                _T0 + timedelta(days=30),
            )
            return await history.read(conn, _USER)
        finally:
            await tx.rollback()
            await conn.close()

    h = asyncio.run(_go())

    assert h.weights == {9100101: 3, 9100102: 2}
    assert h.category_scores == {"에세이": 3, "한국소설": 2}
    assert h.computed_at == _T0 + timedelta(days=2)


@needs_db
def test_until을_주면_그_시각까지의_이력만_읽는다() -> None:
    async def _go() -> history.History:
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                "INSERT INTO v_books (book_id, title, author, publisher, price, in_stock,"
                " cover_url, category, pub_year, description)"
                " VALUES (9100101, '책1', '저자', '출판사', 10000, true, NULL, '에세이',"
                " 2024, NULL)"
            )
            await conn.execute(
                "INSERT INTO v_user_library VALUES ($1, 9100101, $2)", _USER, _T0
            )
            # until 뒤의 구매는 빠진다
            await conn.execute(
                "INSERT INTO v_user_purchases VALUES ($1, 9100101, $2)",
                _USER,
                _T0 + timedelta(days=2),
            )
            return await history.read(conn, _USER, _T0 + timedelta(days=1))
        finally:
            await tx.rollback()
            await conn.close()

    h = asyncio.run(_go())

    assert h.weights == {9100101: 1}
    assert h.computed_at == _T0


@needs_db
def test_프로필_이후_이력이_바꾼_카테고리_점수만_낸다() -> None:
    # 1일 구매 · 2일 담기 · 3일 같은 책에 좋은 리뷰. 프로필이 1일까지 반영했다(computed_at).
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.executemany(
                "INSERT INTO v_books (book_id, title, author, publisher, price, in_stock,"
                " cover_url, category, pub_year, description)"
                " VALUES ($1, $2, '저자', '출판사', 10000, true, NULL, $3, 2024, NULL)",
                [(9100101, "책1", "에세이"), (9100102, "책2", "한국소설")],
            )
            await conn.execute(
                "INSERT INTO v_user_purchases VALUES ($1, 9100101, $2)",
                _USER,
                _T0 + timedelta(days=1),
            )
            await conn.execute(
                "INSERT INTO v_user_library VALUES ($1, 9100102, $2)",
                _USER,
                _T0 + timedelta(days=2),
            )
            await conn.execute(
                "INSERT INTO v_user_reviews VALUES ($1, 9100101, 4.5, $2)",
                _USER,
                _T0 + timedelta(days=3),
            )
            reflected = _T0 + timedelta(days=1)
            return (
                await history.category_scores_since(conn, _USER, reflected),
                await history.category_scores_since(
                    conn, _USER, reflected, _T0 + timedelta(days=1, hours=12)
                ),
                await history.category_scores_since(conn, _USER, None),
            )
        finally:
            await tx.rollback()
            await conn.close()

    since, until_before_new, everything = asyncio.run(_go())

    # 담기(+1)만 더해진다. 산 책에 달린 좋은 리뷰는 큰 값 하나(3) 그대로라 에세이는 바뀌지 않는다.
    assert since == {"한국소설": 1}
    # until 뒤의 이력(커서를 받은 뒤 생긴 것)은 빼고 본다.
    assert until_before_new == {}
    # 반영한 이력이 없으면(computed_at 이 비면) 전부 더한다.
    assert everything == {"에세이": 3, "한국소설": 1}


def _isbn13(book_id: int) -> str:
    return str(9780000000000 + book_id)


@needs_db
def test_요청으로_받은_이력은_복제_표에서_읽은_것과_같은_결과를_낸다() -> None:
    books = [(9100101, "에세이"), (9100102, "한국소설"), (9100103, "에세이")]

    async def _go() -> tuple[history.History, history.History]:
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.executemany(
                "INSERT INTO v_books (book_id, title, price, in_stock, category, isbn13)"
                " VALUES ($1, '책', 10000, true, $2, $3)",
                [(b, category, _isbn13(b)) for b, category in books],
            )
            # 같은 이력을 복제 표에도 넣는다: 1번은 사고 1점, 2번은 담고 4.5점, 3번은 담기만.
            await conn.execute(
                "INSERT INTO v_user_purchases VALUES ($1, 9100101, $2)", _USER, _T0
            )
            await conn.executemany(
                "INSERT INTO v_user_library VALUES ($1, $2, $3)",
                [(_USER, 9100102, _T0), (_USER, 9100103, _T0)],
            )
            await conn.executemany(
                "INSERT INTO v_user_reviews VALUES ($1, $2, $3, $4)",
                [
                    (_USER, 9100101, Decimal("1.0"), _T0),
                    (_USER, 9100102, Decimal("4.5"), _T0),
                ],
            )
            replicated = await history.read(conn, _USER)
            reported = await history.from_request(
                conn,
                purchased_isbns=[_isbn13(9100101)],
                library_isbns=[_isbn13(9100102), _isbn13(9100103)],
                reviews=[(_isbn13(9100101), 1.0), (_isbn13(9100102), 4.5)],
                at=_T0 + timedelta(days=7),
            )
            return replicated, reported
        finally:
            await tx.rollback()
            await conn.close()

    replicated, reported = asyncio.run(_go())

    assert (
        reported.weights == replicated.weights == {9100101: 3, 9100102: 2, 9100103: 1}
    )
    assert reported.category_scores == replicated.category_scores
    assert reported.disliked_book_ids == replicated.disliked_book_ids == {9100101}


@needs_db
def test_요청으로_받은_이력은_받은_시각까지_반영한_것으로_적는다() -> None:
    # 요청에는 이력마다의 시각이 없다. BE 가 그때까지의 전체를 보낸 것이라, 이력이 비어 있어도
    # 받은 시각을 적는다. 비워 두면 ③④ 가 복제 표에 남은 옛 이력을 전부 "나중 이력"으로 더한다.
    at = _T0 + timedelta(days=7)

    async def _go() -> history.History:
        conn = await asyncpg.connect(_DB_URL)
        try:
            return await history.from_request(
                conn, purchased_isbns=[], library_isbns=[], reviews=[], at=at
            )
        finally:
            await conn.close()

    h = asyncio.run(_go())

    assert h.weights == {}
    assert h.computed_at == at
