"""BE → AI 반영 배치의 실행·진행 기록·끝 검증(app/jobs/sync_from_be.py, #710) 시험.

앞 둘은 DB 없이 돌고, 나머지는 SEARCH_TEST_DATABASE_URL 이 있을 때만 돈다(트랜잭션 안에서 하고 되돌린다). BE 는 가짜다.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.jobs import sync_from_be as sync
from tests.test_sync_from_be_convert import _be_book, _be_product
from tests.test_sync_from_be_write import _DB_URL, _in_tx


def test_다시_읽는_시작은_마지막_시각에서_조금_겹친다() -> None:
    last = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)

    assert sync.since_of(last) == last - sync.OVERLAP
    assert sync.since_of(None) is None


def test_BE_주소가_없으면_아무것도_하지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sync,
        "get_settings",
        lambda: type("S", (), {"be_mysql_url": "", "database_url": "unused"})(),
    )

    assert asyncio.run(sync.main()) == 0


needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


class _FakeBe:
    """BE MySQL 대신. books_since·products_since 는 since 뒤의 줄을 묶음으로 준다(BE 시각은 한국 시간)."""

    def __init__(self, books: list[dict], products: list[dict]) -> None:
        self.books = books
        self.products = products
        self.asked: list[tuple[str, datetime | None]] = []

    def books_since(self, since: datetime | None, size: int):
        self.asked.append(("books", since))
        rows = [
            b
            for b in self.books
            if since is None or b["updated_at"].replace(tzinfo=sync.BE_TZ) >= since
        ]
        for start in range(0, len(rows), size):
            yield rows[start : start + size]

    def products_since(self, since: datetime | None, size: int):
        self.asked.append(("products", since))
        rows = [
            p
            for p in self.products
            if since is None or p["updated_at"].replace(tzinfo=sync.BE_TZ) >= since
        ]
        for start in range(0, len(rows), size):
            yield rows[start : start + size]

    def books_by_ids(self, ids: list[int]) -> list[dict]:
        wanted = set(ids)
        return [b for b in self.books if b["id"] in wanted]


# 기록이 없으면 배치가 그 "지금"에서 시작해 시험 줄(11/1)을 건너뛰므로, 기록을 먼저 넣어 둔다
_SEEDED = datetime(2026, 10, 1, tzinfo=UTC)


def _in_seeded_tx(body):
    """마이그레이션을 적용하고 진행 기록을 _SEEDED 로 넣은 트랜잭션 안에서 body 를 돌리고 되돌린다."""

    async def seeded(conn):
        await conn.execute(
            "INSERT INTO replication_state (name, cursor_value) VALUES ($1, $3), ($2, $3)"
            " ON CONFLICT (name) DO UPDATE SET cursor_value = EXCLUDED.cursor_value",
            sync.BOOKS_CURSOR,
            sync.PRODUCTS_CURSOR,
            _SEEDED,
        )
        return await body(conn)

    return _in_tx(seeded)


@needs_db
def test_바뀐_책과_상품을_옮기고_진행_기록을_남긴다() -> None:
    fake = _FakeBe([_be_book()], [_be_product()])

    async def body(conn):
        summary = await sync.run(conn, fake)
        book = await conn.fetchrow(
            "SELECT title, isbn13, price, in_stock, pub_year, updated_at, deleted_at"
            " FROM v_books WHERE book_id = 990000001"
        )
        product = await conn.fetchrow(
            "SELECT book_id, discounted_price, stock_quantity FROM v_products WHERE id = 880000001"
        )
        cursors = dict(
            await conn.fetch(
                "SELECT name, cursor_value FROM replication_state WHERE name = ANY($1)",
                [sync.BOOKS_CURSOR, sync.PRODUCTS_CURSOR],
            )
        )
        return summary, book, product, cursors

    summary, book, product, cursors = _in_seeded_tx(body)

    assert summary.books == 1 and summary.products == 1
    assert dict(book) == {
        "title": "시험 책",
        "isbn13": "9788900000011",
        # 옛 칸은 새 줄에 0·false 로 들어간다(서버 표와 같다)
        "price": 0,
        "in_stock": False,
        "pub_year": 2025,
        "updated_at": datetime(
            2026, 10, 31, 18, 0, 0, tzinfo=UTC
        ),  # BE 11/1 03:00(한국)
        "deleted_at": None,
    }
    assert dict(product) == {
        "book_id": 990000001,
        "discounted_price": Decimal("13500.00"),
        "stock_quantity": 5,
    }
    assert cursors[sync.BOOKS_CURSOR] == datetime(2026, 10, 31, 18, 0, 0, tzinfo=UTC)


@needs_db
def test_진행_기록이_있으면_그_시각에서_겹쳐_읽는다() -> None:
    fake = _FakeBe([_be_book()], [_be_product()])
    last = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)

    async def body(conn):
        await conn.execute(
            "INSERT INTO replication_state (name, cursor_value) VALUES ($1, $3), ($2, $3)"
            " ON CONFLICT (name) DO UPDATE SET cursor_value = EXCLUDED.cursor_value",
            sync.BOOKS_CURSOR,
            sync.PRODUCTS_CURSOR,
            last,
        )
        await sync.run(conn, fake)

    _in_seeded_tx(body)

    assert fake.asked == [
        ("books", last - sync.OVERLAP),
        ("products", last - sync.OVERLAP),
    ]


@needs_db
def test_옮긴_뒤_값이_다르면_오류로_끝난다(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeBe([_be_book()], [_be_product()])
    # 옮기는 중에 BE 값이 바뀐 것처럼, 확인할 때만 다른 제목을 돌려준다
    monkeypatch.setattr(fake, "books_by_ids", lambda ids: [_be_book(title="다른 제목")])

    async def body(conn):
        with pytest.raises(sync.CheckFailed):
            await sync.run(conn, fake)

    _in_seeded_tx(body)


@needs_db
def test_옮긴_책이_없으면_진행_기록을_바꾸지_않는다() -> None:
    fake = _FakeBe([], [])

    async def body(conn):
        summary = await sync.run(conn, fake)
        cursor = await conn.fetchval(
            "SELECT cursor_value FROM replication_state WHERE name = $1",
            sync.BOOKS_CURSOR,
        )
        return summary, cursor

    summary, cursor = _in_seeded_tx(body)

    assert summary.books == 0
    # 옮긴 것이 없으면 기록을 그대로 둔다
    assert cursor == _SEEDED


@needs_db
def test_진행_기록이_없으면_AI_표의_마지막_시각에서_시작한다() -> None:
    fake = _FakeBe([], [])

    async def body(conn):
        await sync.run(conn, fake)
        return (
            await conn.fetchval("SELECT max(updated_at) FROM v_books"),
            await conn.fetchval("SELECT max(updated_at) FROM v_products"),
        )

    books_last, products_last = _in_tx(body)

    assert fake.asked == [
        ("books", books_last - sync.OVERLAP),
        ("products", products_last - sync.OVERLAP),
    ]
