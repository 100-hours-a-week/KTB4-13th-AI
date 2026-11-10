"""사용자 복제 표를 지우는 마이그레이션(db/migrations/20261109_drop_user_replica_tables.sql) 테스트(#307).

실제 PostgreSQL 이 있어야 돈다. 트랜잭션 안에서 적용하고 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import os
from pathlib import Path

import asyncpg
import pytest

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_MIGRATION = (
    Path(__file__).parent.parent
    / "db"
    / "migrations"
    / "20261109_drop_user_replica_tables.sql"
)

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER_TABLES = ("v_user_purchases", "v_user_library", "v_user_reviews")
# 책 정보는 복제를 계속한다. 지우면 검색·추천이 깨진다
_CATALOG_TABLES = ("v_books", "v_products", "v_book_popularity")


def _tables_after(times: int) -> dict[str, bool]:
    """마이그레이션을 times 번 적용한 뒤 표마다 남아 있는지."""

    async def go() -> dict[str, bool]:
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for _ in range(times):
                await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
            return {
                table: await conn.fetchval("SELECT to_regclass($1) IS NOT NULL", table)
                for table in (*_USER_TABLES, *_CATALOG_TABLES)
            }
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(go())


def test_사용자_표_셋만_지우고_책_정보_표는_남긴다() -> None:
    left = _tables_after(1)

    assert [t for t in _USER_TABLES if left[t]] == []
    assert all(left[t] for t in _CATALOG_TABLES)


def test_두_번_적용해도_실패하지_않는다() -> None:
    # 표가 이미 없는 환경(새로 만든 DB 등)에서 다시 돌려도 멈추지 않는다
    left = _tables_after(2)

    assert [t for t in _USER_TABLES if left[t]] == []
