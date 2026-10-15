"""⑨ 대체 자리 다시 뽑기 — 밤 배치 맨 끝에 BE 가 부른다(#456, 명세 ⑨, 설계 #395).

⑥ 이 목록을 뽑다 시간 예산을 넘긴 관심사는 그 분류의 인기 책으로 대신 채워 두고 표시한다(feed_partial).
바뀐 것이 없는 사용자에게는 ⑥ 이 다시 불리지 않아 그 자리가 남는다. 그래서 표시된 목록을 저장해 둔 프로필로
다시 뽑는다. BE 가 목록 하나를 기다리는 것이 아니라 ⑥ 보다 넉넉한 예산을 준다. 한 밤에 한 목록은 한 번만
시도하고, 또 넘기면 표시를 남겨 다음 밤에 다시 한다. LLM 과 임베딩을 부르지 않는다.
"""

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from app.core import history
from app.feed import interest_feed
from app.profile import interests
from app.profile import service as profile_service

# 한 번 불렸을 때 쓰는 시간. BE 는 30초 클라이언트로 부르고 remaining 이 0 이 될 때까지 다시 부른다(명세 ⑨)
CALL_SECONDS = 20.0
# 목록 하나를 뽑는 시간 예산(⑥ 은 3초). 관심사 하나에 ⑥ 보다 넉넉한 시간을 준다
LIST_BUDGET_SECONDS = 6.0
# 다음 목록을 시작하려면 남아 있어야 하는 시간. 예산에 대신할 책 읽기와 저장할 몫을 더했다
LIST_RESERVE_SECONDS = 8.0
_KST = ZoneInfo("Asia/Seoul")

# 다음 목록 하나를 집으며 오늘 밤 시도했다고 표시한다. 다시 뽑다 실패해도 표시가 남아 그 밤에는 다시 집지
# 않는다. ⑨ 둘이 겹쳐 불려도 같은 목록을 집지 않는다(SKIP LOCKED)
_CLAIM_SQL = """
UPDATE taste_profile
SET feed = jsonb_set(feed, '{retried_on}', to_jsonb($1::text))
WHERE user_id = (
    SELECT user_id FROM taste_profile
    WHERE feed_partial AND coalesce(feed->>'retried_on', '') <> $1
    ORDER BY user_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
RETURNING user_id
"""
_REMAINING_SQL = """
SELECT count(*) FROM taste_profile
WHERE feed_partial AND coalesce(feed->>'retried_on', '') <> $1
"""
_PROFILE_SQL = """
SELECT interest_scores::text, interest_vectors::text, conditions::text
FROM taste_profile WHERE user_id = $1
"""
# ⑥ 과 같이 판 번호를 올리고 바로 앞 판을 남긴다
_SAVE_SQL = """
UPDATE taste_profile
SET feed_prev = feed, feed = $2::jsonb, feed_version = feed_version + 1,
    feed_partial = $3
WHERE user_id = $1
"""


def night_of(now: datetime) -> str:
    """그 시각이 든 밤. 밤 배치가 자정을 넘겨도 같은 밤으로 치려고 한국 시간 정오를 경계로 한다."""
    return (now.astimezone(_KST) - timedelta(hours=12)).date().isoformat()


async def refresh(
    conn: asyncpg.Connection,
    *,
    now: datetime | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, int]:
    """대체 자리가 표시된 목록을 CALL_SECONDS 안에서 다시 뽑는다.

    다시 뽑은 목록 수와, 오늘 밤 아직 시도하지 않은 목록 수를 돌려준다(명세 ⑨ refreshed·remaining).
    """
    night = night_of(now or datetime.now(UTC))
    deadline = clock() + CALL_SECONDS
    refreshed = 0
    while clock() + LIST_RESERVE_SECONDS <= deadline:
        user_id = await conn.fetchval(_CLAIM_SQL, night)
        if user_id is None:
            break
        await _refresh_one(conn, user_id, night)
        refreshed += 1
    remaining = await conn.fetchval(_REMAINING_SQL, night)
    return {"refreshed": refreshed, "remaining": remaining}


async def _refresh_one(conn: asyncpg.Connection, user_id: int, night: str) -> None:
    """저장된 프로필로 목록을 다시 뽑아 저장한다. 가진 책은 복제 표의 이력으로 뺀다(요청이 없어서)."""
    async with conn.transaction():
        # ⑥ 과 같은 사용자별 잠금이라 같은 사용자의 ⑥ 과 겹치지 않는다
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1, $2)", profile_service.LOCK_SPACE, user_id
        )
        row = await conn.fetchrow(_PROFILE_SQL, user_id)
        vectors = json.loads(row["interest_vectors"])
        hist = await history.read(conn, user_id)
        feed = await interest_feed.build(
            conn,
            interests.combined(json.loads(row["interest_scores"])),
            {k: v for k, v in vectors.items() if not k.startswith(interests.KEYWORD)},
            {k: v for k, v in vectors.items() if k.startswith(interests.KEYWORD)},
            json.loads(row["conditions"]),
            set(hist.weights) | hist.disliked_book_ids,
            seconds=LIST_BUDGET_SECONDS,
        )
        await conn.execute(
            _SAVE_SQL,
            user_id,
            json.dumps({**feed.stored(), "retried_on": night}, ensure_ascii=False),
            bool(feed.substituted),
        )
