"""멱등 기록 표 테스트. 실제 PostgreSQL 이 있어야 돈다.

SEARCH_TEST_DATABASE_URL 에 DB 주소를 주면 돈다. 넣은 데이터는 트랜잭션을 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import os

import asyncpg
import pytest

from app.core import idempotency

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


def _run(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute("DELETE FROM idempotency_records")
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


async def _old(conn: asyncpg.Connection, key: str, hours: int) -> None:
    await conn.execute(
        "INSERT INTO idempotency_records VALUES ($1, 'h', '{}'::jsonb,"
        " now() - make_interval(hours => $2))",
        key,
        hours,
    )


def test_새_기록을_저장할_때_24시간_넘은_기록을_지운다() -> None:
    # 탈퇴한 사용자의 쇼핑 응답이 끝없이 남지 않게 한다(#270). 명세는 "최소 24시간 보관"이다
    async def check(conn):
        await _old(conn, "agent:old", 25)
        await _old(conn, "agent:recent", 23)
        await idempotency.remember(conn, "agent:new", "h", {"message": "ok"})
        rows = await conn.fetch(
            "SELECT idempotency_key FROM idempotency_records ORDER BY 1"
        )
        return [r["idempotency_key"] for r in rows]

    assert _run(check) == ["agent:new", "agent:recent"]
