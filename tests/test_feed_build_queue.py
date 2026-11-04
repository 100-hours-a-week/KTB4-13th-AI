"""프로필 계산 뒤 홈 목록을 뒤에서 만드는 장치(app/feed/build_queue.py) 테스트(#580).

"실제 DB" 절의 테스트만 PostgreSQL 이 필요하다(SEARCH_TEST_DATABASE_URL).
"""

import asyncio
import json
import os
from pathlib import Path

import asyncpg
import pytest

from app.feed import build_queue, refresh
from app.feed.interest_feed import Feed
from app.feed.interest_list import Placed

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)
_MIGRATION = (
    Path(__file__).parent.parent
    / "db"
    / "migrations"
    / "20261016_taste_profile_interests.sql"
)
_USER = 9_100_401
_OLD = {
    "books": [{"book_id": 1, "interest": "요리", "match_score": 40}],
    "substituted": [],
}
_PREV = {
    "books": [{"book_id": 9, "interest": "SF", "match_score": 10}],
    "substituted": [],
}


def _job(token: str, **changes) -> build_queue.Job:
    values = {
        "user_id": _USER,
        "token": token,
        "table": {"요리": {"onboarding": 1.0}},
        "book_vectors": {"요리": [0.1]},
        "keyword_vectors": {"키워드:고양이": [0.2]},
        "conditions": ["쪽수<300"],
        "owned": frozenset({9_100_032, 9_100_031}),
        "disliked": frozenset({9_100_032}),
        "first_seen": {"9780000000001": "2026-11-03"},
        "book_anchors": {"요리": (9_100_031,)},
        "seconds": 6.0,
    }
    return build_queue.Job(**(values | changes))


def _feed(book_id: int, *substituted: str) -> Feed:
    return Feed([Placed(book_id, "요리", 80)], substituted)


def _books(book_id: int) -> list[dict]:
    return [{"book_id": book_id, "interest": "요리", "match_score": 80}]


async def _insert(conn, feed: dict | None, version: int = 3, prev: dict | None = None):
    await conn.execute(
        "INSERT INTO taste_profile (user_id, cold_start, profile_version, feed,"
        " feed_version, feed_prev, feed_partial) VALUES ($1, false, 1, $2::jsonb, $3,"
        " $4::jsonb, true)",
        _USER,
        json.dumps(feed) if feed is not None else None,
        version,
        json.dumps(prev) if prev is not None else None,
    )


async def _row(conn) -> dict:
    row = await conn.fetchrow(
        "SELECT feed::text, feed_prev::text, feed_version, feed_partial,"
        " interest_scores::text, interest_vectors::text, conditions::text,"
        " book_first_seen::text FROM taste_profile WHERE user_id = $1",
        _USER,
    )
    out = dict(row)
    for key in (
        "feed",
        "feed_prev",
        "interest_scores",
        "interest_vectors",
        "conditions",
        "book_first_seen",
    ):
        out[key] = json.loads(out[key]) if out[key] is not None else None
    return out


def _run(check):
    """새 칸 마이그레이션이 있는 DB 에서 돌고 바깥 트랜잭션을 되돌린다."""

    async def go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(go())


# --- 실제 DB: 요청 표 적기와 표를 확인한 저장 ---


@_needs_db
def test_요청_표를_적어도_목록과_판_번호와_앞_판은_그대로다() -> None:
    # 표 적기에서 판을 올리면 프로필 계산 한 번에 판이 두 번 올라, 더 보기를 넘기던 사람이 앞 판을 잃는다
    async def check(conn):
        await _insert(conn, _OLD, version=3, prev=_PREV)
        first = await build_queue.mark(conn, _USER)
        second = await build_queue.mark(conn, _USER)
        return first, second, await _row(conn)

    first, second, row = _run(check)

    assert first != second
    assert row["feed"] == {**_OLD, "pending": second}
    assert (row["feed_version"], row["feed_prev"]) == (3, _PREV)


@_needs_db
def test_처음_쓰는_사용자는_요청_표만_적는다() -> None:
    async def check(conn):
        await _insert(conn, None, version=0)
        token = await build_queue.mark(conn, _USER)
        return token, await _row(conn)

    token, row = _run(check)

    assert row["feed"] == {"pending": token}
    assert row["feed_version"] == 0


@_needs_db
def test_표가_그대로면_목록과_관심사_값을_함께_저장하고_판을_한_번_올린다() -> None:
    async def check(conn):
        await _insert(conn, _OLD, version=3, prev=_PREV)
        token = await build_queue.mark(conn, _USER)
        saved = await build_queue.save(conn, _job(token), _feed(2))
        return saved, await _row(conn)

    saved, row = _run(check)

    assert saved is True
    # 표는 지우고, 밤 다시 뽑기가 쓰도록 가진 책을 목록과 함께 남긴다
    assert row["feed"] == {
        "books": _books(2),
        "substituted": [],
        "owned": [9_100_031, 9_100_032],
        "disliked": [9_100_032],
    }
    assert row["feed_prev"] == _OLD
    assert (row["feed_version"], row["feed_partial"]) == (4, False)
    assert row["interest_scores"] == {"요리": {"onboarding": 1.0}}
    assert row["interest_vectors"] == {"요리": [0.1], "키워드:고양이": [0.2]}
    assert row["conditions"] == ["쪽수<300"]
    assert row["book_first_seen"] == {"9780000000001": "2026-11-03"}


@_needs_db
def test_처음_쓰는_사용자는_앞_판_없이_첫_판을_저장한다() -> None:
    async def check(conn):
        await _insert(conn, None, version=0)
        token = await build_queue.mark(conn, _USER)
        await build_queue.save(conn, _job(token), _feed(2, "에세이"))
        return await _row(conn)

    row = _run(check)

    assert row["feed"]["books"] == _books(2)
    assert row["feed_prev"] is None
    assert (row["feed_version"], row["feed_partial"]) == (1, True)


@_needs_db
@pytest.mark.parametrize("old_finishes_first", [False, True])
def test_끝나는_순서가_바뀌어도_최신_요청의_목록이_남는다(
    old_finishes_first: bool,
) -> None:
    async def check(conn):
        await _insert(conn, _OLD, version=3)
        old = await build_queue.mark(conn, _USER)
        if old_finishes_first:
            # 옛 목록이 먼저 끝나면 잠깐 저장되고, 새 목록이 그 위를 덮는다
            old_saved = await build_queue.save(conn, _job(old), _feed(2))
            new = await build_queue.mark(conn, _USER)
        else:
            # 옛 목록이 늦게 끝나면 그새 새 요청이 표를 바꿔 두었으므로 버린다
            new = await build_queue.mark(conn, _USER)
            old_saved = await build_queue.save(conn, _job(old), _feed(2))
        new_saved = await build_queue.save(conn, _job(new), _feed(3))
        return old_saved, new_saved, await _row(conn)

    old_saved, new_saved, row = _run(check)

    assert (old_saved, new_saved) == (old_finishes_first, True)
    assert row["feed"]["books"] == _books(3)
    assert row["feed_version"] == (5 if old_finishes_first else 4)


@_needs_db
def test_사이에_밤_다시_뽑기가_저장해도_뒤의_작업_목록이_남는다() -> None:
    night = {"books": _books(7), "substituted": []}

    async def check(conn):
        await _insert(conn, _OLD, version=3)
        token = await build_queue.mark(conn, _USER)
        # 밤 다시 뽑기는 옛 관심사 값으로 다시 뽑아 저장한다. 요청 표는 남긴다
        await conn.execute(refresh._SAVE_SQL, _USER, json.dumps(night), False)
        saved = await build_queue.save(conn, _job(token), _feed(2))
        return saved, await _row(conn)

    saved, row = _run(check)

    assert saved is True
    assert row["feed"]["books"] == _books(2)
    assert row["feed_prev"] == night
    assert row["feed_version"] == 5


@_needs_db
def test_밤_다시_뽑기가_그_사용자를_뽑는_중이면_저장은_끝날_때까지_기다린다() -> None:
    # 밤 다시 뽑기는 사용자 잠금을 쥔 채 옛 관심사 값으로 목록을 뽑는다. 저장이 기다리지 않으면, 먼저 저장한
    # 새 목록을 밤 다시 뽑기가 옛 값으로 만든 목록으로 덮는다. 두 연결이 서로를 봐야 해서 넣고 끝나면 지운다
    async def go():
        holder = await asyncpg.connect(_DB_URL)
        worker = await asyncpg.connect(_DB_URL)
        try:
            await _insert(worker, _OLD, version=3)
            token = await build_queue.mark(worker, _USER)
            tx = holder.transaction()
            await tx.start()
            await holder.execute(
                "SELECT pg_advisory_xact_lock($1, $2)",
                refresh.profile_service.LOCK_SPACE,
                _USER,
            )
            saving = asyncio.create_task(
                build_queue.save(worker, _job(token), _feed(2))
            )
            await asyncio.sleep(0.3)
            waited = not saving.done()
            await tx.commit()
            return waited, await saving
        finally:
            await worker.execute("DELETE FROM taste_profile WHERE user_id = $1", _USER)
            await holder.close()
            await worker.close()

    waited, saved = asyncio.run(go())

    assert waited is True
    assert saved is True
