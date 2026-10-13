"""④ 목록에서 뺄 책의 조건(#330) — 조건을 바꿔 끼워도 조회의 자리 번호가 맞는가. DB 없이 돈다.

CI 는 DB 테스트를 건너뛴다. 자리 번호가 어긋나면 조회가 통째로 실패하므로, 조회문과 넘기는 값의
개수가 맞는지는 여기서 DB 없이 본다.
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.feed import cold_start, exclusion, personalized, rule_only
from app.feed.cursor import Page
from app.feed.schemas import parse_query
from app.search.schemas import SearchFilters

_NOW = datetime(2026, 10, 12, tzinfo=UTC)


def test_복제_표로_빼는_조건은_값을_셋_쓴다() -> None:
    excluded = exclusion.replicated(7, _NOW)

    assert excluded.params[0] == 7
    assert excluded.params[2] == _NOW
    assert set(re.findall(r"\$\d+", excluded.sql(1))) == {"$1", "$2", "$3"}
    # 앞에 다른 값이 있으면 그만큼 밀린다.
    assert set(re.findall(r"\$\d+", excluded.sql(3))) == {"$3", "$4", "$5"}


def test_받은_책으로_빼는_조건은_값을_하나_쓴다() -> None:
    excluded = exclusion.owned([30, 10, 20])

    assert excluded.params == ([10, 20, 30],)
    assert re.findall(r"\$\d+", excluded.sql(4)) == ["$4"]


def test_같은_책을_여러_번_받아도_한_번만_넘긴다() -> None:
    # 산 책이 나의 도서관에도 담겨 있으면 두 칸에 다 실려 온다.
    assert exclusion.owned([10, 20, 10]).params == ([10, 20],)


def test_가진_책이_없으면_빼는_조건도_값도_없다() -> None:
    excluded = exclusion.owned([])

    assert excluded.params == ()
    assert "$" not in excluded.sql(1)


# ---------------------------------------------------------------------------
# 세 목록의 조회: 조건을 바꿔 끼워도 조회문의 자리 번호와 넘기는 값이 맞는다
# ---------------------------------------------------------------------------

_EXCLUSIONS = {
    "복제 표": exclusion.replicated(1, _NOW),
    "받은 책": exclusion.owned([10, 20]),
    "가진 책 없음": exclusion.owned([]),
}
_FILTERS = [
    {},
    {"category": "소설"},
    {"category": "소설", "pub_year_from": "2020", "pub_year_to": "2024"},
]


class _RecordingConn:
    """조회를 실행하지 않고, 받은 조회문과 값을 적어 둔다."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, tuple]] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str) -> None:
        pass

    async def fetchval(self, sql: str, *args, timeout: float | None = None):
        if sql.startswith("EXPLAIN"):
            return json.dumps([{"Plan": {"Plan Rows": 1}}])
        return None

    async def fetch(self, sql: str, *args, timeout: float | None = None):
        self.queries.append((sql, args))
        return []


def _request(sort: str, filters: dict):
    return parse_query(
        [
            ("user_id", "1"),
            ("surface", "recommend_more"),
            ("sort", sort),
            *filters.items(),
        ]
    )


def _assert_numbered(conn: _RecordingConn) -> None:
    assert conn.queries, "조회가 한 번도 나가지 않았다"
    for sql, args in conn.queries:
        used = {int(n) for n in re.findall(r"\$(\d+)", sql)}
        # 빠진 번호도 남는 값도 없어야 한다. 하나라도 어긋나면 PostgreSQL 이 조회를 거절한다.
        assert used == set(range(1, len(args) + 1)), (sorted(used), len(args))


@pytest.mark.parametrize("name", list(_EXCLUSIONS))
@pytest.mark.parametrize("filters", _FILTERS)
@pytest.mark.parametrize("sort", ["match", "newest", "price_asc"])
def test_개인화를_끈_목록의_자리_번호가_맞는다(
    sort: str, filters: dict, name: str
) -> None:
    conn = _RecordingConn()
    req = _request(sort, filters)

    asyncio.run(cold_start.fetch(conn, req, Page(issued_at=_NOW), _EXCLUSIONS[name]))

    _assert_numbered(conn)


@pytest.mark.parametrize("name", list(_EXCLUSIONS))
@pytest.mark.parametrize("filters", _FILTERS)
def test_규칙_점수_목록의_자리_번호가_맞는다(filters: dict, name: str) -> None:
    conn = _RecordingConn()
    req = _request("match", filters)

    asyncio.run(
        rule_only.fetch(
            conn, req, Page(issued_at=_NOW), {"한국문학": 2.0}, _EXCLUSIONS[name]
        )
    )

    _assert_numbered(conn)


@pytest.mark.parametrize("name", list(_EXCLUSIONS))
@pytest.mark.parametrize("filters", _FILTERS)
def test_개인화_목록의_자리_번호가_맞는다(filters: dict, name: str) -> None:
    conn = _RecordingConn()
    req = _request("match", filters)
    search_filters = SearchFilters(
        category=req.category,
        pub_year_from=req.pub_year_from,
        pub_year_to=req.pub_year_to,
    )

    asyncio.run(
        personalized._nearest(
            conn, req, Page(issued_at=_NOW), [0.1], search_filters, _EXCLUSIONS[name]
        )
    )

    _assert_numbered(conn)
