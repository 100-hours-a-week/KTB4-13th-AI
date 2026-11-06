"""시험대 검증(evals/searchlab/testbench.py) 테스트(#597). DB 없이 돈다."""

import pytest

from evals.searchlab import testbench


def _search(qid, state="first", rep=1, kw=10.0, vec=100.0, read=5, ids=(1, 2)):
    return {
        "type": "search",
        "id": qid,
        "state": state,
        "rep": rep,
        "ids": list(ids),
        "error": None,
        "server": {
            "keyword": [{"exec_ms": kw, "read": read, "hit": 1, "calls": 4}],
            "vector": [{"exec_ms": vec, "read": read, "hit": 1, "calls": 5}],
            "books": [{"exec_ms": 1.0, "read": 0, "hit": 3, "calls": 1}],
        },
    }


def _statio(hit, read):
    return {
        "type": "statio",
        "relations": [
            {"relname": "idx", "kind": "i", "am": "hnsw", "hit": hit, "read": read}
        ],
    }


def _round(state, started="2026-11-06 00:00:00+00:00"):
    return {
        "type": "round",
        "state": state,
        "server_stats": True,
        "env": {"started": started},
    }


def test_단계별_DB_쪽_값은_검색어마다_더하고_반복_조회는_첫_번을_뺀_중앙값() -> None:
    lines = [
        _round("first"),
        _search("a", kw=10, vec=100),
        _round("repeat"),
        _search("a", "repeat", 1, vec=900),
        _search("a", "repeat", 2, vec=30),
        _search("a", "repeat", 3, vec=50),
    ]
    assert testbench.server_values(lines, "first", "vector") == {"a": 100.0}
    assert testbench.server_values(lines, "first", None, "read") == {"a": 10.0}
    assert testbench.server_values(lines, "repeat", "vector") == {"a": 40.0}


def test_server_값이_없는_기록은_멈춘다() -> None:
    line = _search("a")
    del line["server"]
    with pytest.raises(ValueError, match="--server-stats"):
        testbench.server_values([line], "first", "vector")


def test_캐시_적중률은_처음_조회_구간의_것() -> None:
    lines = [
        _round("first"),
        _search("a"),
        _statio(90, 10),
        _round("repeat"),
        _statio(100, 0),
    ]
    assert testbench.section_hit_ratio(lines, "first") == 0.9


def test_넷을_다_지키면_통과() -> None:
    exp = [
        _round("first"),
        _search("a", kw=12, vec=110, read=6),
        _search("b", kw=20, vec=200),
        _statio(85, 15),
    ]
    prod = [
        _round("first"),
        _search("a", kw=10, vec=100, read=5),
        _search("b", kw=18, vec=190),
        _statio(90, 10),
    ]
    exp1000 = [_search("a", "repeat", 2, vec=60), _search("b", "repeat", 2, vec=60)]
    exp400 = [_search("a", "repeat", 2, vec=100), _search("b", "repeat", 2, vec=100)]
    prod1000 = [_search("a", "repeat", 2, vec=55), _search("b", "repeat", 2, vec=55)]
    prod400 = [_search("a", "repeat", 2, vec=100), _search("b", "repeat", 2, vec=100)]
    got = testbench.judge(exp, prod, (exp1000, exp400), (prod1000, prod400))
    assert [c.name for c in got.checks] == [
        "키워드 처음 조회 중앙값",
        "키워드 처음 조회 p90",
        "벡터 처음 조회 중앙값",
        "벡터 처음 조회 p90",
        "캐시 밖 블록 중앙값",
        "캐시 밖 블록 p90",
        "벡터 색인 캐시 적중률",
        "탐색 폭 1000/400 시간 비",
    ]
    assert all(c.ok for c in got.checks) and got.ok
    assert got.overlap == 1.0
    ratio = got.checks[-1]
    assert ratio.exp == pytest.approx(0.6) and ratio.prod == pytest.approx(0.55)


def test_하나라도_넘으면_통과가_아니다() -> None:
    exp = [_round("first"), _search("a", vec=200), _statio(60, 40)]
    prod = [_round("first"), _search("a", vec=100), _statio(90, 10)]
    got = testbench.judge(exp, prod, None, None)
    by = {c.name: c for c in got.checks}
    assert not by["벡터 처음 조회 중앙값"].ok
    assert not by["벡터 색인 캐시 적중률"].ok
    assert by["키워드 처음 조회 중앙값"].ok
    assert "탐색 폭 1000/400 시간 비" not in by
    assert not got.ok


def test_블록이_둘_다_0_이면_같다고_본다() -> None:
    assert testbench.within_ratio(0, 0, 0.3) == (True, 1.0)
    assert testbench.within_ratio(5, 0, 0.3)[0] is False
    assert testbench.within_ratio(13, 10, 0.3) == (True, 1.3)
