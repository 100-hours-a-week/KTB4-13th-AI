"""검색 시간 한 회차 재기(evals/searchlab/measure.py) 테스트(#593). DB 없이 돈다.

DB 에 붙어 끝까지 도는 것은 로컬 평가 DB 에서 손으로 확인했다(PR 본문). pg_stat_statements 는 로컬에 없어
실험 서버에서 처음 돈다.
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
