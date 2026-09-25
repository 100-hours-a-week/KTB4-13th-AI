"""④ 가까운 책을 어떻게 뽑는지(#196) — 필터에 걸리는 책 수로 방법을 고르는가. DB 없이 돈다."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.feed import personalized
from app.feed.cursor import Page
from app.feed.schemas import parse_query


class _FakeConn:
    """걸리는 책 수(count)와, 벡터 색인으로 뽑을 때 돌려줄 권수(index_rows)를 정해 둔다."""

    def __init__(self, count: int = 0, index_rows: int = 500) -> None:
        self.count = count
        self.index_rows = index_rows
        self.counted = False
        self.ran: list[str] = []
        self.settings: list[str] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str) -> None:
        self.settings.append(sql)

    async def fetchval(self, sql: str, *args):
        self.counted = True
        return self.count

    async def fetch(self, sql: str, *args):
        if "+ 0" in sql:
            self.ran.append("exact")
            return [{"book_id": i} for i in range(personalized.CANDIDATE_LIMIT)]
        self.ran.append("index")
        return [{"book_id": i} for i in range(self.index_rows)]


def _nearest(conn: _FakeConn, **params) -> list:
    req = parse_query(
        [("user_id", "1"), ("surface", "recommend_more"), *params.items()]
    )
    page = Page(issued_at=datetime(2026, 9, 30, tzinfo=UTC))
    filters = personalized.SearchFilters(
        category=req.category,
        pub_year_from=req.pub_year_from,
        pub_year_to=req.pub_year_to,
    )
    return asyncio.run(personalized._nearest(conn, req, page, [0.1], filters))


def test_필터가_없으면_세지_않고_벡터_색인으로_뽑는다() -> None:
    conn = _FakeConn()
    _nearest(conn)

    assert (conn.counted, conn.ran) == (False, ["index"])


def test_걸리는_책이_적으면_정확히_계산한다() -> None:
    conn = _FakeConn(count=personalized.EXACT_LIMIT)
    _nearest(conn, category="법학")

    assert conn.ran == ["exact"]


def test_걸리는_책이_많으면_벡터_색인으로_뽑는다() -> None:
    conn = _FakeConn(count=personalized.EXACT_LIMIT + 1)
    _nearest(conn, category="한국문학")

    assert conn.ran == ["index"]


def test_벡터_색인이_모자라면_정확히_다시_뽑는다() -> None:
    # 취향과 먼 분류면 멀리 훑어도 몇 권 못 찾는다. 명세상 필터로 줄어드는 건 0건일 때뿐이다.
    conn = _FakeConn(count=personalized.FALLBACK_LIMIT, index_rows=18)
    rows = _nearest(conn, category="법학")

    assert conn.ran == ["index", "exact"]
    assert len(rows) == personalized.CANDIDATE_LIMIT


def test_걸리는_책이_너무_많으면_모자라도_다시_뽑지_않는다() -> None:
    # 정확 계산이 너무 비싸다(261만 권에서 67만 권 약 3초).
    conn = _FakeConn(count=personalized.FALLBACK_LIMIT + 1, index_rows=18)
    _nearest(conn, pub_year_from="2024")

    assert conn.ran == ["index"]


@pytest.mark.parametrize("params", [{}, {"category": "법학"}])
def test_매번_이번_값으로_실행_계획을_세운다(params: dict) -> None:
    # 계획이 연결마다 달라지면 쪽마다 뽑는 방법이 갈려 책이 중복되거나 빠진다(#196).
    conn = _FakeConn(count=10)
    _nearest(conn, **params)

    assert "SET LOCAL plan_cache_mode = force_custom_plan" in conn.settings


def test_필터가_있으면_벡터_색인이_더_멀리_훑는다() -> None:
    conn = _FakeConn(count=personalized.EXACT_LIMIT + 1)
    _nearest(conn, category="한국문학")

    assert any("scan_mem_multiplier" in s for s in conn.settings)
    assert any("max_scan_tuples" in s for s in conn.settings)
