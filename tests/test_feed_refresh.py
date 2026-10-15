"""⑨ 대체 자리 다시 뽑기(app/feed/refresh.py) 테스트(#456).

"실제 DB" 절의 테스트만 PostgreSQL 이 필요하다(SEARCH_TEST_DATABASE_URL). 목록 뽑기는 가짜로 바꾼다
(뽑기는 tests/test_feed_interest_feed.py 에서 본다).
"""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from app.core import history
from app.feed import interest_feed, refresh
from app.feed.interest_feed import Feed
from app.feed.interest_list import Placed

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_KST = ZoneInfo("Asia/Seoul")

# --- 밤 ---


@pytest.mark.parametrize(
    ("at", "night"),
    [
        (datetime(2026, 10, 16, 23, 30, tzinfo=_KST), "2026-10-16"),
        (
            datetime(2026, 10, 17, 1, 0, tzinfo=_KST),
            "2026-10-16",
        ),  # 자정을 넘겨도 같은 밤
        (datetime(2026, 10, 17, 13, 0, tzinfo=_KST), "2026-10-17"),
    ],
)
def test_밤_배치가_자정을_넘겨도_같은_밤으로_친다(at: datetime, night: str) -> None:
    assert refresh.night_of(at) == night


# --- 실제 DB ---

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
_NOW = datetime(2026, 10, 16, 23, 30, tzinfo=_KST)
_NIGHT = "2026-10-16"
_OLD_BOOKS = [{"book_id": 1, "interest": "요리", "match_score": 40}]


def _feed(**extra) -> str:
    return json.dumps({"books": _OLD_BOOKS, "substituted": ["요리"], **extra})


class _FakeBuild:
    """다시 뽑은 목록을 차례로 돌려준다. 받은 값도 남긴다."""

    def __init__(self, *feeds: Feed) -> None:
        self.feeds = list(feeds)
        self.calls: list[dict] = []

    async def __call__(
        self, conn, scores, book_vectors, keyword_vectors, *rest, seconds
    ):
        self.calls.append(
            {"book_vectors": book_vectors, "keyword_vectors": keyword_vectors}
        )
        return self.feeds.pop(0)


async def _no_history(conn, user_id, until=None):
    return history.History()


def _run_refresh(monkeypatch, build: _FakeBuild, profiles: dict, clock=None):
    """profiles: user_id → (feed_partial, feed). 바깥 트랜잭션을 되돌려 남기지 않는다."""
    monkeypatch.setattr(interest_feed, "build", build)
    monkeypatch.setattr(history, "read", _no_history)

    async def go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
            for user_id, (partial, feed) in profiles.items():
                await conn.execute(
                    "INSERT INTO taste_profile (user_id, cold_start, profile_version,"
                    " interest_scores, interest_vectors, conditions, feed, feed_version,"
                    " feed_partial) VALUES ($1, false, 1, $2::jsonb, $3::jsonb, '[]',"
                    " $4::jsonb, 1, $5)",
                    user_id,
                    json.dumps({"요리": {"onboarding": 1.0}}),
                    json.dumps({"요리": [0.1], "키워드:고양이": [0.2]}),
                    feed,
                    partial,
                )
            options = {"now": _NOW} | ({"clock": clock} if clock else {})
            result = await refresh.refresh(conn, **options)
            rows = await conn.fetch(
                "SELECT user_id, feed::text, feed_prev::text, feed_version, feed_partial"
                " FROM taste_profile WHERE user_id = ANY($1::int[]) ORDER BY user_id",
                list(profiles),
            )
            return result, {row["user_id"]: dict(row) for row in rows}
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(go())


_A, _B, _C = 9_100_011, 9_100_012, 9_100_013


@_needs_db
def test_대체_자리가_표시된_목록을_저장된_프로필로_다시_뽑아_판을_올린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _FakeBuild(Feed([Placed(2, "요리", 80)], ()))
    profiles = {
        _A: (True, _feed()),
        _B: (True, _feed(retried_on=_NIGHT)),  # 오늘 밤 이미 시도함
        _C: (False, json.dumps({"books": _OLD_BOOKS, "substituted": []})),
    }

    result, rows = _run_refresh(monkeypatch, build, profiles)

    assert result == {"refreshed": 1, "remaining": 0}
    assert json.loads(rows[_A]["feed"]) == {
        "books": [{"book_id": 2, "interest": "요리", "match_score": 80}],
        "substituted": [],
        "retried_on": _NIGHT,
    }
    assert json.loads(rows[_A]["feed_prev"])["books"] == _OLD_BOOKS
    assert (rows[_A]["feed_version"], rows[_A]["feed_partial"]) == (2, False)
    # 키워드 관심사의 핵심어 벡터는 따로 넘긴다
    assert build.calls == [
        {"book_vectors": {"요리": [0.1]}, "keyword_vectors": {"키워드:고양이": [0.2]}}
    ]
    assert rows[_B]["feed_version"] == rows[_C]["feed_version"] == 1


@_needs_db
def test_또_넘기면_표시를_남기고_그_밤에는_다시_집지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _FakeBuild(Feed([Placed(2, "요리", 50)], ("요리",)))

    result, rows = _run_refresh(monkeypatch, build, {_A: (True, _feed())})

    assert result == {"refreshed": 1, "remaining": 0}
    assert rows[_A]["feed_partial"] is True
    assert json.loads(rows[_A]["feed"])["retried_on"] == _NIGHT


@_needs_db
def test_남은_시간이_목록_하나에_모자라면_멈추고_남은_수를_답한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _FakeBuild(Feed([], ()), Feed([], ()))
    ticks = iter([0.0, 0.0, 15.0])  # 시작, 첫 목록 전, 첫 목록 뒤

    result, _ = _run_refresh(
        monkeypatch,
        build,
        {_A: (True, _feed()), _B: (True, _feed())},
        clock=lambda: next(ticks),
    )

    assert result == {"refreshed": 1, "remaining": 1}


@_needs_db
def test_다시_뽑을_목록이_없으면_둘_다_0이다(monkeypatch: pytest.MonkeyPatch) -> None:
    result, _ = _run_refresh(monkeypatch, _FakeBuild(), {})

    assert result == {"refreshed": 0, "remaining": 0}
