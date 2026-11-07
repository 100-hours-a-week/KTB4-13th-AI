"""검색 시간 한 회차 재기(evals/searchlab/measure.py) 테스트(#593). DB 없이 돈다.

DB 에 붙어 끝까지 도는 것은 로컬 평가 DB 에서 손으로 확인했다(PR 본문). `--server-stats` 는 pg_stat_statements 를
켠 임시 로컬 DB(평가 DB 의 구조와 책 8천 권)에서 확인했다. 검색마다 비우는 `--pgss` 는 실험 서버에서 처음 돈다.
"""

import asyncio

import pytest

from app.search import keyword, service, sorting, vector
from evals.searchlab import measure


def test_앱이_바꾸지_않는_설정만_연결의_세션_설정으로_건다() -> None:
    assert measure.Knobs().session_settings() == {}
    knobs = measure.Knobs(
        ef_search=100, random_page_cost=1.1, effective_io_concurrency=64
    )
    # 탐색 폭은 앱이 SET LOCAL 로 거는 값이라 세션 설정이 아니라 앱 상수를 바꾼다
    assert knobs.session_settings() == {
        "random_page_cost": "1.1",
        "effective_io_concurrency": "64",
    }


def test_처음_조회는_한_번씩_반복_조회는_일곱_번씩() -> None:
    items = [{"id": "a"}, {"id": "b"}]
    assert [(x["id"], rep) for x, rep in measure.plan(items, "first")] == [
        ("a", 1),
        ("b", 1),
    ]
    repeat = measure.plan(items, "repeat")
    assert len(repeat) == 2 * measure.REPEATS
    assert [rep for x, rep in repeat if x["id"] == "a"] == list(
        range(1, measure.REPEATS + 1)
    )
    with pytest.raises(ValueError):
        measure.plan(items, "warm")


def test_순서는_시드가_같으면_같고_다르면_달라진다() -> None:
    items = [{"id": f"q{i}"} for i in range(30)]
    assert measure.ordered(items, None) == items
    one = measure.ordered(items, "r1")
    assert measure.ordered(list(reversed(items)), "r1") == one
    assert measure.ordered(items, "r2") != one
    assert sorted(x["id"] for x in one) == sorted(x["id"] for x in items)


def test_요청은_평가와_같이_위_20권을_한_번에() -> None:
    req = measure.request_of(
        {"query": "사랑", "filters": {"category": "소설"}, "sort": "popular"}
    )
    assert req == {
        "query": "사랑",
        "filters": {"category": "소설"},
        "sort": "popular",
        "size": 20,
    }
    assert measure.request_of({"query": "x"})["sort"] == "relevance"


class _Conn:
    async def fetch(self, *_args, **_kwargs):
        return []


def test_재는_동안만_벡터와_단계_함수를_바꾸고_끝나면_되돌린다() -> None:
    before = (
        service._embed_query,
        keyword.search_ids,
        sorting.sort_ids,
        vector.SEARCH_EF_SEARCH,
    )
    with measure.patched(measure.Knobs(ef_search=123), {"q": [0.5]}) as stages:
        assert vector.SEARCH_EF_SEARCH == 123
        assert asyncio.run(service._embed_query("q")) == [0.5]
        with pytest.raises(KeyError, match="벡터 파일에 없는"):
            asyncio.run(service._embed_query("없는 말"))
        assert asyncio.run(sorting.sort_ids(_Conn(), [1], "popular")) == []
        assert list(stages.ms) == ["sort"]
        assert len(stages.ms["sort"]) == 1
    assert (
        service._embed_query,
        keyword.search_ids,
        sorting.sort_ids,
        vector.SEARCH_EF_SEARCH,
    ) == before


def test_실패해도_되돌린다() -> None:
    before = service._embed_query
    with pytest.raises(RuntimeError), measure.patched(measure.Knobs(), {}):
        raise RuntimeError
    assert service._embed_query is before


def test_pg_stat_statements_는_있는_칸만_읽는다() -> None:
    sql = measure.pgss_select(
        {"queryid", "query", "calls", "shared_blks_hit", "shared_blks_read"}
    )
    assert "generic_plan_calls" not in sql
    assert "shared_blks_read" in sql
    assert "NOT ILIKE '%pg_stat_statements%'" in sql


def test_문장_기록은_값이_없는_문장을_빼고_읽은_블록이_많은_순으로() -> None:
    rows = [
        {
            "query": "BEGIN",
            "calls": 1,
            "rows": 0,
            "shared_blks_hit": 0,
            "shared_blks_read": 0,
        },
        {
            "query": "SELECT  a\n FROM b",
            "calls": 1,
            "rows": 3,
            "shared_blks_hit": 5,
            "shared_blks_read": 1,
            "total_exec_time": 1.23456,
        },
        {
            "query": "SELECT c",
            "calls": 1,
            "rows": 1,
            "shared_blks_hit": 50,
            "shared_blks_read": 9,
            "total_exec_time": 9.0,
        },
    ]
    got = measure.pgss_rows(rows)
    assert [r["query"] for r in got] == ["SELECT c", "SELECT a FROM b"]
    assert got[1]["total_exec_time"] == 1.235


def test_실험용이_아닌_DB_에는_묻지_않으면_돌리지_않는다() -> None:
    assert measure.refusal("bookjeok_ai_eval", allow_any=False) is None
    assert "실험용이 아니다" in measure.refusal("bookjeok_ai", allow_any=False)
    assert measure.refusal("bookjeok_ai", allow_any=True) is None
    assert measure.database_name("postgresql://u:secret@host:5432/db_eval") == "db_eval"


def test_종류와_건수로_고른다() -> None:
    items = [{"kind": "title"}, {"kind": "sort"}, {"kind": "title"}]
    assert measure.select(items, ["title"], None) == [
        {"kind": "title"},
        {"kind": "title"},
    ]
    assert measure.select(items, None, 2) == items[:2]


def test_벡터_한도를_풀면_풀의_문장_한도도_같이_늘린다() -> None:
    assert measure.Knobs().command_timeout() == 5.0
    assert measure.Knobs(vector_timeout=1.0).command_timeout() == 5.0
    assert measure.Knobs(vector_timeout=30.0).command_timeout() == 30.0
    before = vector.QUERY_TIMEOUT_SECONDS
    with measure.patched(measure.Knobs(vector_timeout=30.0), {}):
        assert vector.QUERY_TIMEOUT_SECONDS == 30.0
    assert vector.QUERY_TIMEOUT_SECONDS == before


def test_문장_통계를_비우는_것은_실험_DB_에서만() -> None:
    # 운영·개발 DB 의 pg_stat_statements 는 다른 사람도 본다. 비우지 않고 앞뒤 차이로만 읽는다
    assert measure.pgss_refusal("bookjeok_ai_eval") is None
    assert "--server-stats" in measure.pgss_refusal("bookjeok_ai")


def test_서버_통계는_자기_계정의_바뀐_줄만_묻고_읽는_문장_자신은_뺀다() -> None:
    sql = measure.server_diff_sql(
        {
            "queryid",
            "toplevel",
            "calls",
            "total_exec_time",
            "shared_blks_hit",
            "shared_blks_read",
            "blk_read_time",
        }
    )
    assert "pg_stat_statements(false)" in sql
    assert "current_user" in sql
    assert "pg_backend_pid()" in sql
    assert "blk_read_time AS read_time" in sql
    sql18 = measure.server_diff_sql(
        {
            "queryid",
            "toplevel",
            "calls",
            "total_exec_time",
            "shared_blks_hit",
            "shared_blks_read",
            "shared_blk_read_time",
        }
    )
    assert "shared_blk_read_time AS read_time" in sql18


def test_서버_통계는_앞뒤_차이를_낸다() -> None:
    stats = measure.ServerStats()
    row = {
        "queryid": 1,
        "toplevel": True,
        "calls": 10,
        "total_exec_time": 100.0,
        "shared_blks_hit": 50,
        "shared_blks_read": 5,
        "read_time": 2.0,
    }
    assert stats.apply([row]) is None  # 처음 읽은 것은 기준이다
    changed = dict(
        row,
        calls=11,
        total_exec_time=103.5,
        shared_blks_hit=58,
        shared_blks_read=7,
        read_time=2.5,
    )
    new = {
        "queryid": 2,
        "toplevel": True,
        "calls": 1,
        "total_exec_time": 1.0,
        "shared_blks_hit": 3,
        "shared_blks_read": 0,
        "read_time": 0.0,
    }
    assert stats.apply([changed, new]) == {
        "calls": 2,
        "exec_ms": 4.5,
        "hit": 11,
        "read": 2,
        "read_ms": 0.5,
    }
    assert stats.apply([]) == {
        "calls": 0,
        "exec_ms": 0.0,
        "hit": 0,
        "read": 0,
        "read_ms": 0.0,
    }
    assert stats.known() == ([1, 2], [11, 1])


class _KeywordConn:
    def __init__(self, rows: int) -> None:
        self.rows = rows

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Tx()

    async def execute(self, *_args, **_kwargs):
        return None

    async def fetch(self, *_args, **_kwargs):
        return [{"book_id": i} for i in range(self.rows)]


def test_단계가_끝나면_바깥_단계마다_한_번_부르고_그_시간은_단계_시간에_넣지_않는다() -> (
    None
):
    called: list[str] = []

    async def after(label: str) -> None:
        called.append(label)
        await asyncio.sleep(0.05)

    from app.search.schemas import SearchFilters

    with measure.patched(measure.Knobs(), {}, after_stage=after) as stages:
        # 낱말 둘 → 모든 낱말(1권) 뒤 낱말마다 넓히기. 안쪽 _run 두 번에는 부르지 않는다
        asyncio.run(keyword.search_ids(_KeywordConn(1), "꿀벌 그림책", SearchFilters()))
    assert called == ["keyword"]
    assert len(stages.ms["keyword_query"]) == 2
    assert stages.ms["keyword"][0] < 50


def test_표·색인별로_읽은_블록의_차이와_캐시_적중률() -> None:
    before = [
        {"relname": "hnsw_idx", "kind": "i", "am": "hnsw", "fetched": 100, "hit": 90},
        {"relname": "books", "kind": "r", "am": "heap", "fetched": 10, "hit": 10},
    ]
    after = [
        {"relname": "hnsw_idx", "kind": "i", "am": "hnsw", "fetched": 300, "hit": 240},
        {"relname": "books", "kind": "r", "am": "heap", "fetched": 10, "hit": 10},
        {"relname": "new_idx", "kind": "i", "am": "gin", "fetched": 4, "hit": 1},
    ]
    delta = measure.statio_delta(before, after)
    assert delta == [
        {"relname": "hnsw_idx", "kind": "i", "am": "hnsw", "read": 50, "hit": 150},
        {"relname": "new_idx", "kind": "i", "am": "gin", "read": 3, "hit": 1},
    ]
    assert measure.hit_ratio(delta, "hnsw") == 0.75
    assert measure.hit_ratio(delta, "brin") is None


def test_실험_DB_가_아니면_pgss_는_묻지_않고_멈추고_둘은_같이_못_쓴다(
    monkeypatch, capsys
) -> None:
    from types import SimpleNamespace

    from app.core import config

    monkeypatch.setattr(
        config,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://u@h/bookjeok_ai"),
    )
    assert measure.main(["--label", "x", "--state", "first", "--pgss", "--any-db"]) == 2
    assert "--server-stats" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        measure.main(["--label", "x", "--state", "first", "--pgss", "--server-stats"])


def test_낱말_적중_선은_재는_동안만_바꾸고_끝나면_되돌린다() -> None:
    before = keyword.MIN_COVERAGE
    with measure.patched(measure.Knobs(min_coverage=0.7), {"q": [0.5]}):
        assert keyword.MIN_COVERAGE == 0.7
    assert keyword.MIN_COVERAGE == before
    with measure.patched(measure.Knobs(), {"q": [0.5]}):
        assert keyword.MIN_COVERAGE == before
