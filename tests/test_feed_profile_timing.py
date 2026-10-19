"""⑥ 시간을 저장 없이 재는 도구(evals/feed/profile_timing.py) 테스트(#492)."""

import asyncio

import pytest

from evals.feed import profile_timing


class _Conn:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def execute(self, sql: str, *args) -> str:
        self.ran.append(sql)
        return "DONE"

    async def fetchval(self, sql: str, *args) -> int:
        self.ran.append(sql)
        return 1


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO taste_profile VALUES ($1)",
        "\n  UPDATE taste_profile SET feed = $2",
        "delete from taste_profile",
    ],
)
def test_저장하는_문장은_건너뛰고_센다(sql: str) -> None:
    conn = _Conn()
    wrapped = profile_timing.SkipWrites(conn)

    assert asyncio.run(wrapped.execute(sql, 1)) == "SKIPPED"
    assert (conn.ran, wrapped.skipped) == ([], 1)


def test_읽는_문장과_다른_메서드는_그대로_넘긴다() -> None:
    conn = _Conn()
    wrapped = profile_timing.SkipWrites(conn)

    assert asyncio.run(wrapped.execute("SET LOCAL hnsw.ef_search = 200")) == "DONE"
    assert asyncio.run(wrapped.fetchval("SELECT 1")) == 1
    assert (len(conn.ran), wrapped.skipped) == (2, 0)


def test_요약은_가운데값과_예산을_넘긴_수를_센다() -> None:
    rows = [
        {"total": 1.0, "books": 200, "substituted": 0},
        {"total": 3.5, "books": 200, "substituted": 1},
        {"total": 6.0, "books": 0, "substituted": 1},
    ]

    line = profile_timing.summary(rows)

    assert "users=3 median=3.50" in line
    assert "over3=2 over5=1" in line
    assert "substituted_users=2 empty_lists=1" in line
