"""④ 목록 흐름 테스트. DB 없이 돈다."""

import asyncio

import pytest

from app.feed import service
from app.feed.schemas import parse_query


def test_cold_start_목록은_모두_0점이라_점수_하한이_있으면_DB를_보지_않고_0건이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_pool():
        raise AssertionError("DB 를 보면 안 된다")

    monkeypatch.setattr(service.db, "get_pool", _no_pool)
    req = parse_query(
        [("user_id", "1"), ("surface", "recommend_more"), ("match_score_min", "1")]
    )

    assert asyncio.run(service.feed(req)) == []
