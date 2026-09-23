"""④ 목록을 고르는 흐름 테스트 — 프로필 유무에 따라 어느 길로 가는가. DB 없이 돈다."""

import asyncio
from contextlib import asynccontextmanager

import pytest

from app.feed import service
from app.feed.schemas import parse_query


class _FakePool:
    def __init__(self, profile_row):
        self._row = profile_row

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self._row)


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, sql, *args):
        return self._row


def _run(req, profile_row, monkeypatch, **fakes):
    monkeypatch.setattr(service.db, "get_pool", lambda: _FakePool(profile_row))
    for name, fn in fakes.items():
        module, attr = name.split("__")
        monkeypatch.setattr(getattr(service, module), attr, fn)
    return asyncio.run(service.feed(req))


def _req(**params):
    return parse_query(
        [("user_id", "1"), ("surface", "recommend_more"), *params.items()]
    )


def test_프로필이_없으면_개인화를_끈_목록으로_간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _cold(conn, req, page):
        return [{"book_id": 7, "match_score": 0}], False

    outcome = _run(_req(), None, monkeypatch, cold_start__fetch=_cold)

    assert [item["book_id"] for item in outcome.items] == [7]
    assert (outcome.cold_start, outcome.degraded) == (True, None)


def test_취향_벡터를_못_만든_사용자도_같은_길로_간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ⑥ 이 cold_start 로 저장한 경우다. 벡터가 없어 채점할 수 없다.
    row = {
        "centroid": None,
        "tag_weights": "{}",
        "cold_start": True,
        "profile_version": 0,
    }

    async def _cold(conn, req, page):
        return [], False

    outcome = _run(_req(), row, monkeypatch, cold_start__fetch=_cold)

    assert outcome.cold_start is True


def test_개인화를_끈_목록은_모두_0점이라_점수_하한이_있으면_조회하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _cold(conn, req, page):
        raise AssertionError("목록을 조회하면 안 된다")

    outcome = _run(
        _req(match_score_min="1"), None, monkeypatch, cold_start__fetch=_cold
    )

    assert outcome.items == []


def test_프로필이_있으면_채점한_목록을_준다(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "centroid": "[0.1, 0.2]",
        "tag_weights": '{"에세이": 3}',
        "cold_start": False,
        "profile_version": 2,
    }

    async def _personalized(conn, req, page, centroid, tag_weights):
        assert centroid == [0.1, 0.2]
        assert tag_weights == {"에세이": 3}
        return [{"book_id": 9, "match_score": 84}], False

    outcome = _run(_req(), row, monkeypatch, personalized__fetch=_personalized)

    assert [item["book_id"] for item in outcome.items] == [9]
    assert (outcome.cold_start, outcome.degraded) == (False, None)


def test_벡터_조회가_안_되면_규칙_점수만으로_답하고_알린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "centroid": "[0.1]",
        "tag_weights": "{}",
        "cold_start": False,
        "profile_version": 1,
    }

    async def _personalized(conn, req, page, centroid, tag_weights):
        raise RuntimeError("벡터 색인 이상")

    async def _cold(conn, req, page):
        return [{"book_id": 3, "match_score": 0}], False

    outcome = _run(
        _req(),
        row,
        monkeypatch,
        personalized__fetch=_personalized,
        cold_start__fetch=_cold,
    )

    assert outcome.degraded == service.RULE_ONLY
    assert outcome.cold_start is False


def test_더_볼_것이_있으면_다음_페이지_커서를_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")

    async def _cold(conn, req, page):
        return [{"book_id": 1, "match_score": 0}], True

    outcome = _run(_req(), None, monkeypatch, cold_start__fetch=_cold)

    assert outcome.next_cursor is not None


def test_마지막_페이지면_커서를_주지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _cold(conn, req, page):
        return [], False

    outcome = _run(_req(), None, monkeypatch, cold_start__fetch=_cold)

    assert outcome.next_cursor is None
