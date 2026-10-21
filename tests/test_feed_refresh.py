"""⑨ 대체 자리 다시 뽑기(app/feed/refresh.py, POST /recommendations/feed/refresh) 테스트(#456).

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
from fastapi.testclient import TestClient

from app.core import history
from app.core.config import get_settings
from app.feed import interest_feed, refresh
from app.feed.interest_feed import Feed
from app.feed.interest_list import Placed
from app.main import app
from app.routers import feed as feed_router

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


# --- 주소 ---

client = TestClient(app)


def test_플래그가_꺼져_있으면_다시_뽑을_것이_없다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "interest_feed", False)

    res = client.post("/recommendations/feed/refresh", json={})

    assert res.status_code == 200
    assert res.json() == {
        "message": "refresh_success",
        "data": {"refreshed": 0, "remaining": 0},
    }


def test_다시_뽑다_실패하면_500이다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "interest_feed", True)

    class _Pool:
        def acquire(self):
            raise RuntimeError("DB 없음")

    monkeypatch.setattr(feed_router.db, "get_pool", lambda: _Pool())

    res = client.post("/recommendations/feed/refresh")

    assert res.status_code == 500
    assert res.json() == {"message": "internal_server_error", "data": None}


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
# ⑥ 이 목록과 함께 남긴 가진 책과 그중 2점 이하 리뷰를 단 책(#537)
_OWNED = {"owned": [9_100_031, 9_100_032], "disliked": [9_100_032]}


def _feed(**extra) -> str:
    return json.dumps({"books": _OLD_BOOKS, "substituted": ["요리"], **_OWNED, **extra})


class _FakeBuild:
    """다시 뽑은 목록을 차례로 돌려준다. 받은 값도 남긴다."""

    def __init__(self, *feeds: Feed) -> None:
        self.feeds = list(feeds)
        self.calls: list[dict] = []

    async def __call__(
        self,
        conn,
        scores,
        book_vectors,
        keyword_vectors,
        *rest,
        seconds,
        book_anchors=None,
    ):
        self.calls.append(
            {
                "book_vectors": book_vectors,
                "keyword_vectors": keyword_vectors,
                "owned": rest[1],
            }
        )
        self.anchors = book_anchors
        return self.feeds.pop(0)


async def _no_replica(conn, user_id, until=None):
    raise AssertionError("⑥ 이 남긴 가진 책이 있으면 복제 표를 읽지 않는다(#537)")


def _run_refresh(
    monkeypatch, build: _FakeBuild, profiles: dict, clock=None, replica=_no_replica
):
    """profiles: user_id → (feed_partial, feed). 바깥 트랜잭션을 되돌려 남기지 않는다.

    replica 는 복제 표의 이력 읽기 대신 쓸 함수다. 기본은 읽으면 실패한다.
    """
    monkeypatch.setattr(interest_feed, "build", build)
    monkeypatch.setattr(history, "read", replica)

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
    # 가진 책은 다음 밤을 위해 새 목록에도 그대로 남긴다
    assert json.loads(rows[_A]["feed"]) == {
        "books": [{"book_id": 2, "interest": "요리", "match_score": 80}],
        "substituted": [],
        **_OWNED,
        "retried_on": _NIGHT,
    }
    assert json.loads(rows[_A]["feed_prev"])["books"] == _OLD_BOOKS
    assert (rows[_A]["feed_version"], rows[_A]["feed_partial"]) == (2, False)
    # 키워드 관심사의 핵심어 벡터는 따로 넘긴다
    # ⑥ 이 남긴 가진 책을 뺀다
    assert build.calls == [
        {
            "book_vectors": {"요리": [0.1]},
            "keyword_vectors": {"키워드:고양이": [0.2]},
            "owned": {9_100_031, 9_100_032},
        }
    ]
    assert rows[_B]["feed_version"] == rows[_C]["feed_version"] == 1


@_needs_db
def test_또_넘기면_저장된_목록을_두고_표시만_남겨_그_밤에는_다시_집지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _FakeBuild(Feed([Placed(2, "요리", 50)], ("요리",)))

    result, rows = _run_refresh(monkeypatch, build, {_A: (True, _feed())})

    # 다시 뽑아 보기는 했으므로 센다
    assert result == {"refreshed": 1, "remaining": 0}
    assert rows[_A]["feed_partial"] is True
    assert rows[_A]["feed_version"] == 1
    assert json.loads(rows[_A]["feed"]) == {
        "books": _OLD_BOOKS,
        "substituted": ["요리"],
        **_OWNED,
        "retried_on": _NIGHT,
    }


@_needs_db
def test_잘_뽑혔던_관심사가_이번에_시간을_넘기면_저장된_목록을_그대로_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 요리는 이번에 뽑혔지만, ⑥ 때 잘 뽑혔던 키워드:고양이가 시간을 넘겨 대신 채워졌다(#482)
    build = _FakeBuild(Feed([Placed(2, "요리", 80)], ("키워드:고양이",)))

    _, rows = _run_refresh(monkeypatch, build, {_A: (True, _feed())})

    assert rows[_A]["feed_version"] == 1
    assert json.loads(rows[_A]["feed"])["books"] == _OLD_BOOKS


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


@_needs_db
def test_매칭_점수에_쓸_내_책은_저장된_처음_본_날의_책을_다시_갈라_넘긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 요리 번호(594.5)인 책 둘. 9100022 는 그 뒤 2점 이하 리뷰를 달아 뺀다(#483)
    books = {9_100_021: "9780009100021", 9_100_022: "9780009100022"}
    build = _FakeBuild(Feed([], ()))
    monkeypatch.setattr(interest_feed, "build", build)
    monkeypatch.setattr(history, "read", _no_replica)

    async def go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
            await conn.execute(
                "CREATE TEMP TABLE book_classes (isbn13 text PRIMARY KEY, class_no text)"
            )
            for book_id, isbn13 in books.items():
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, author, publisher, price,"
                    " in_stock, cover_url, category, pub_year, description, isbn13)"
                    " VALUES ($1, '책', '저자', '출판사', 10000, true, NULL, '생활과학',"
                    " 2024, '소개', $2)",
                    book_id,
                    isbn13,
                )
                await conn.execute(
                    "INSERT INTO book_embeddings VALUES ($1, $2::vector, 384, 'test')",
                    book_id,
                    "[" + ",".join(["1"] + ["0"] * 383) + "]",
                )
                await conn.execute(
                    "INSERT INTO book_classes VALUES ($1, '594.5')", isbn13
                )
            await conn.execute(
                "INSERT INTO taste_profile (user_id, cold_start, profile_version,"
                " interest_scores, interest_vectors, conditions, book_first_seen, feed,"
                " feed_version, feed_partial) VALUES ($1, false, 1, $2::jsonb,"
                " '{}', '[]', $3::jsonb, $4::jsonb, 1, true)",
                _A,
                json.dumps({"요리": {"books": 3.0}}),
                json.dumps({isbn13: "2026-10-01" for isbn13 in books.values()}),
                _feed(owned=[9_100_022], disliked=[9_100_022]),
            )
            await refresh.refresh(conn, now=_NOW)
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(go())

    assert build.anchors == {"요리": (9_100_021,)}


async def _replica_stub(conn, user_id, until=None):
    return history.History(weights={9_100_041: 3}, disliked_book_ids={9_100_042})


@_needs_db
def test_가진_책을_남기기_전에_저장된_목록이면_복제_표의_이력으로_뺀다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 이 칸이 생기기 전에 ⑥ 이 저장한 목록이다. 복제 표를 읽는 길을 지울 때(#307) 같이 지운다
    build = _FakeBuild(Feed([Placed(2, "요리", 80)], ()))
    old = json.dumps({"books": _OLD_BOOKS, "substituted": ["요리"]})

    _, rows = _run_refresh(monkeypatch, build, {_A: (True, old)}, replica=_replica_stub)

    assert build.calls[0]["owned"] == {9_100_041, 9_100_042}
    # 다시 뽑아 저장할 때 남겨, 다음 밤부터는 복제 표를 읽지 않는다
    stored = json.loads(rows[_A]["feed"])
    assert (stored["owned"], stored["disliked"]) == (
        [9_100_041, 9_100_042],
        [9_100_042],
    )
