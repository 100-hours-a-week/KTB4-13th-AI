"""검색 시간 결과 정리(evals/searchlab/analyze.py) 테스트(#597). 답을 아는 기록을 만들어 판정을 본다."""

import json
import math

import numpy as np
import pytest

from evals.searchlab import analyze


def _search(qid, ms, *, state="first", rep=1, error=None, ids=(1, 2), statements=None):
    line = {
        "type": "search",
        "id": qid,
        "state": state,
        "rep": rep,
        "total_ms": ms,
        "error": error,
        "ids": list(ids),
    }
    if statements is not None:
        line["statements"] = statements
    return line


def test_두_번_잰_회차는_같은_설정으로_묶는다() -> None:
    assert analyze.config_of("ef400-2") == "ef400"
    assert analyze.config_of("ef400") == "ef400"
    assert analyze.config_of("base-1") == "base"


def test_반복_조회는_첫_번을_빼고_중앙값_실패는_뺀다() -> None:
    lines = [_search("a", 900, state="repeat", rep=1)] + [
        _search("a", ms, state="repeat", rep=i)
        for i, ms in enumerate([10, 20, 30, 40, 50, 60], start=2)
    ]
    lines += [_search("b", 5, state="repeat", rep=2, error="TimeoutError")]
    assert analyze.query_values(lines, "repeat") == {"a": 35.0}
    assert analyze.query_values([_search("a", 7.0)], "first") == {"a": 7.0}


def test_두_회차는_검색어마다_기하평균으로_합치고_둘_다_있는_검색어만() -> None:
    got = analyze.combine([{"a": 10.0, "b": 1.0}, {"a": 40.0}])
    assert got == {"a": pytest.approx(20.0)}


def _pair(factor, n=60, noise=0.05, seed=1):
    rng = np.random.default_rng(seed)
    base = {f"q{i}": float(v) for i, v in enumerate(rng.uniform(100, 1000, n))}
    new = {
        q: v * factor * float(math.exp(rng.normal(0, noise))) for q, v in base.items()
    }
    return new, base


def test_확실히_빨라지면_줄어듦_느려지면_늘어남() -> None:
    new, base = _pair(0.7)
    c = analyze.compare(new, base)
    assert c.verdict == "줄어듦"
    assert c.high < 0
    assert math.exp(c.log_median) == pytest.approx(0.7, rel=0.05)
    new, base = _pair(1.10)
    assert analyze.compare(new, base).verdict == "늘어남"


def test_p95_가_20퍼센트_넘게_늘면_중앙값이_줄어도_탈락() -> None:
    new, base = _pair(0.9)
    slow = sorted(base, key=base.get)[-5:]  # 가장 느린 검색어만 크게 느려짐
    for q in slow:
        new[q] = base[q] * 2.0
    c = analyze.compare(new, base)
    assert c.verdict == "탈락"
    assert c.p95_ratio > analyze.P95_LIMIT


def test_가를_수_없으면_같다가_아니라_판단_불가() -> None:
    new, base = _pair(1.0, noise=0.2, seed=7)
    assert analyze.compare(new, base).verdict == "판단 불가"
    same = {"a": 10.0, "b": 20.0, "c": 30.0}
    assert analyze.compare(same, same).verdict == "판단 불가"
    assert analyze.compare({"a": 1.0}, {"a": 2.0}).notes == ["짝이 모자람"]


def test_같은_기록이면_같은_구간이_나온다() -> None:
    new, base = _pair(0.8, noise=0.3)
    one, two = analyze.compare(new, base), analyze.compare(new, base)
    assert (one.low, one.high, one.p95_low) == (two.low, two.high, two.p95_low)


def test_시간과_읽은_블록의_직선() -> None:
    points = [(x, 12.0 + 0.5 * x) for x in (0, 10, 100, 400)]
    fit = analyze.fit_blocks(points)
    assert fit["a_ms"] == pytest.approx(12.0)
    assert fit["b_ms_per_block"] == pytest.approx(0.5)
    assert fit["r2"] == pytest.approx(1.0)
    assert analyze.fit_blocks(points[:2]) is None
    lines = [
        _search("a", 30, statements=[{"shared_blks_read": 10}, {"shared_blks_read": 5}])
    ]
    assert analyze.block_points(lines) == [(15.0, 30.0)]


def test_일반_계획을_쓴_문장을_센다() -> None:
    lines = [
        _search(
            "a",
            1,
            statements=[
                {"generic_plan_calls": 1},
                {"generic_plan_calls": 0},
                {"calls": 1},
            ],
        )
    ]
    assert analyze.generic_plans(lines) == (1, 2)


def test_위_20권_일치율() -> None:
    new = [_search("a", 1, ids=(1, 2, 3, 4)), _search("b", 1, ids=(9,))]
    base = [_search("a", 1, ids=(1, 2, 5, 6)), _search("b", 1, ids=(9,))]
    assert analyze.overlap(new, base) == pytest.approx((0.5 + 1.0) / 2)


def _round(label, *, before=1.0, after=1.0, latency=0.001, balance=99.0):
    return {
        "type": "round",
        "round": {"label": label},
        "bench_before": before,
        "bench_after": after,
        "rds": {
            "ReadLatency": [{"avg": latency}],
            "EBSIOBalance%": [{"avg": balance}, {"avg": balance + 1}],
        },
    }


def test_버릴_회차와_짝짓지_않을_회차() -> None:
    rounds = [
        _round("a-1"),
        _round("b-1", after=1.5),
        _round("b-2", latency=0.002),
        _round("a-2", balance=60.0),
    ]
    reasons = analyze.discard_reasons(rounds)
    assert reasons["a-1"] == []
    assert "CPU 계산" in reasons["b-1"][0]
    assert "읽기 지연" in reasons["b-2"][0]
    pairs = analyze.unpaired(rounds)
    assert ("a-1", "a-2", pytest.approx(39.0)) in [
        (x, y, pytest.approx(g)) for x, y, g in pairs
    ]
    assert all("a-2" in (x, y) for x, y, _ in pairs)


def test_보고서는_버린_회차를_빼고_기준선과_견준다(tmp_path) -> None:
    rng = np.random.default_rng(3)
    base = {f"q{i}": float(v) for i, v in enumerate(rng.uniform(100, 1000, 40))}

    def write(label, factor):
        lines = [{"type": "round", "state": "first"}] + [
            _search(q, v * factor) for q, v in base.items()
        ]
        (tmp_path / f"{label}.jsonl").write_text(
            "\n".join(json.dumps(x) for x in lines), encoding="utf-8"
        )

    write("ef400-1", 1.0)
    write("ef400-2", 1.0)
    write("ef100-1", 0.6)
    write("ef100-2", 5.0)  # 이 회차는 CPU 계산이 느려 버린다
    rounds = [
        _round("ef400-1"),
        _round("ef400-2"),
        _round("ef100-1"),
        _round("ef100-2", after=2.0),
    ]
    (tmp_path / "rounds.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rounds), encoding="utf-8"
    )

    text = analyze.report(tmp_path, "ef400")
    assert "CPU 계산이 회차 뒤 2.00배 느림" in text
    row = next(line for line in text.splitlines() if line.startswith("| ef100 | first"))
    assert "0.60배" in row and "줄어듦" in row
