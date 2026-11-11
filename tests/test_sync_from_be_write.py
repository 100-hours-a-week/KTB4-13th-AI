"""BE → AI 반영 배치의 AI 쓰기(app/jobs/sync_from_be.py, #710) 시험.

SEARCH_TEST_DATABASE_URL 이 있을 때만 돈다. 트랜잭션 안에서 마이그레이션(20261111)을 적용하고 끝에 되돌려 흔적을 남기지
않는다.
"""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import pytest

from app.jobs import sync_from_be as sync
from tests.test_sync_from_be_convert import _be_book, _be_product, _be_time

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_MIGRATION = (
    Path(__file__).parent.parent
    / "db"
    / "migrations"
    / "20261111_sync_from_be_columns.sql"
)


pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


def _in_tx(body):
    async def go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
            return await body(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(go())


def test_마이그레이션은_두_번_적용해도_실패하지_않는다() -> None:
    async def body(conn):
        await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
        return await conn.fetchval(
            "SELECT to_regclass('replication_state') IS NOT NULL"
        )

    assert _in_tx(body) is True


def test_더_오래된_BE_줄은_새_값을_덮어쓰지_않는다() -> None:
    newer = _be_book(title="새 제목", updated_at=_be_time(2026, 11, 3, 0, 0, 0))
    older = _be_book(title="옛 제목", updated_at=_be_time(2026, 11, 2, 0, 0, 0))

    async def body(conn):
        await sync.upsert_books(conn, [sync.to_book(newer)])
        await sync.upsert_books(conn, [sync.to_book(older)])
        return await conn.fetchval(
            "SELECT title FROM v_books WHERE book_id = 990000001"
        )

    assert _in_tx(body) == "새 제목"


def test_지운_책은_줄을_남기고_지운_시각만_채운다() -> None:
    deleted = _be_book(
        updated_at=_be_time(2026, 11, 3, 0, 0, 0),
        deleted_at=_be_time(2026, 11, 3, 0, 0, 0),
    )

    async def body(conn):
        await sync.upsert_books(conn, [sync.to_book(_be_book())])
        await sync.upsert_books(conn, [sync.to_book(deleted)])
        return await conn.fetchrow(
            "SELECT title, deleted_at FROM v_books WHERE book_id = 990000001"
        )

    row = _in_tx(body)

    assert row["title"] == "시험 책"
    # BE 11/3 00:00(한국) = 11/2 15:00 UTC
    assert row["deleted_at"] == datetime(2026, 11, 2, 15, 0, 0, tzinfo=UTC)


def test_다른_책이_가진_ISBN은_비우고_센다() -> None:
    # AI 표는 isbn13 이 겹치면 안 된다(유일 색인). 한 줄 때문에 묶음 전체가 실패하지 않게 비우고 로그로 센다
    first = _be_book(id=990000001)
    second = _be_book(id=990000002, product_id=880000002)

    async def body(conn):
        dropped = await sync.upsert_books(
            conn, [sync.to_book(first), sync.to_book(second)]
        )
        rows = await conn.fetch(
            "SELECT book_id, isbn13 FROM v_books WHERE book_id = ANY($1) ORDER BY book_id",
            [990000001, 990000002],
        )
        return dropped, [tuple(r) for r in rows]

    dropped, rows = _in_tx(body)

    assert dropped == 1
    assert rows == [(990000001, "9788900000011"), (990000002, None)]


def test_값이_같은_줄은_다시_쓰지_않는다() -> None:
    # 10/22 적재처럼 한 시각에 찍힌 125만 권을 겹쳐 다시 읽을 때, 같은 값이면 줄을 새로 쓰지 않아야 표가 부풀지
    # 않는다. 다시 쓰면 줄의 물리 위치(ctid)가 바뀐다
    book = sync.to_book(_be_book())
    product = sync.to_product(_be_product())

    async def body(conn):
        await sync.upsert_books(conn, [book])
        await sync.upsert_products(conn, [product])
        before = (
            await conn.fetchval(
                "SELECT ctid::text FROM v_books WHERE book_id = 990000001"
            ),
            await conn.fetchval(
                "SELECT ctid::text FROM v_products WHERE id = 880000001"
            ),
        )
        await sync.upsert_books(conn, [book])
        await sync.upsert_products(conn, [product])
        after = (
            await conn.fetchval(
                "SELECT ctid::text FROM v_books WHERE book_id = 990000001"
            ),
            await conn.fetchval(
                "SELECT ctid::text FROM v_products WHERE id = 880000001"
            ),
        )
        return before, after

    before, after = _in_tx(body)

    assert before == after
