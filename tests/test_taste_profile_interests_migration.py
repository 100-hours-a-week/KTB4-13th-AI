"""취향 프로필 새 칸 마이그레이션(db/migrations/20261016_taste_profile_interests.sql) 테스트(#439).

실제 PostgreSQL 이 있어야 돈다. 트랜잭션 안에서 적용하고 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import json
import os
from pathlib import Path

import asyncpg
import pytest

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_MIGRATION = (
    Path(__file__).parent.parent
    / "db"
    / "migrations"
    / "20261016_taste_profile_interests.sql"
)

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

# 지금 ⑥이 저장하는 모양 그대로(app/profile/service.py). 새 칸을 모르는 코드도 그대로 돌아야 한다
_OLD_SAVE = (
    "INSERT INTO taste_profile (user_id, centroid, tag_weights, cold_start, profile_version)"
    " VALUES (9100001, NULL, '{}'::jsonb, true, 1)"
)


def test_적용하면_새_칸이_기본값으로_붙고_두_번_적용해도_된다() -> None:
    async def _go() -> asyncpg.Record:
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            ddl = _MIGRATION.read_text(encoding="utf-8")
            await conn.execute(ddl)
            await conn.execute(ddl)
            await conn.execute(_OLD_SAVE)
            return await conn.fetchrow(
                "SELECT interest_scores::text, interest_vectors::text, conditions::text,"
                " book_first_seen::text, feed, feed_version, feed_prev, feed_partial"
                " FROM taste_profile WHERE user_id = 9100001"
            )
        finally:
            await tx.rollback()
            await conn.close()

    row = asyncio.run(_go())

    assert [json.loads(row[i]) for i in range(4)] == [{}, {}, [], {}]
    assert (
        row["feed"],
        row["feed_version"],
        row["feed_prev"],
        row["feed_partial"],
    ) == (
        None,
        0,
        None,
        False,
    )
