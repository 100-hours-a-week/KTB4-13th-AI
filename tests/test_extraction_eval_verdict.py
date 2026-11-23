"""취향 뽑기 평가 결과 짝 판정(evals/extraction/verdict.py, #753) 테스트. 답을 아는 결과로 판정을 본다."""

import json

import pytest

from evals.extraction import verdict


def _runs(
    passes: dict[str, list[bool]], kind: str = "request", failure: str = "missed"
) -> list[dict]:
    return [
        {"case_id": cid, "kind": kind, "passed": p, "failures": [] if p else [failure]}
        for cid, ps in passes.items()
        for p in ps
    ]


def _file(passes, *, model="m", bundle=7, prompt="v1", **kw) -> dict:
    return {
        "bundle_version": bundle,
        "prompt_version": prompt,
        "model": model,
        "runs": _runs(passes, **kw),
    }


def test_다수결로_통과를_정한다() -> None:
    assert verdict.majority([True, True, False]) is True
    assert verdict.majority([True, False, False]) is False
    # 짝수 회에서 반반이면 통과로 보지 않는다
    assert verdict.majority([True, False]) is False


def test_같으면_통과고_크게_나빠지면_탈락이다() -> None:
    base = {f"c{i}": [i % 10 < 7] * 3 for i in range(85)}
    same = verdict.paired(base, base)
    assert same["diff"] == 0 and same["verdict"] == verdict.PASS

    worse = {k: [False] * 3 if i < 20 else v for i, (k, v) in enumerate(base.items())}
    got = verdict.paired(base, worse)
    assert got["diff"] < -0.10
    assert got["verdict"] == verdict.FAIL
    assert got["base_only"] > 0 and got["new_only"] == 0


def test_차이가_작아도_구간이_한도_아래로_내려가면_탈락이다() -> None:
    # 5%p 낮지만 엇갈리는 대화가 많아 구간이 넓다
    base = {f"c{i}": [i % 2 == 0] for i in range(85)}
    new = {
        f"c{i}": [(i % 2 == 1) if i < 40 else (i % 2 == 0) and i % 20 != 0]
        for i in range(85)
    }
    got = verdict.paired(base, new)
    assert -0.10 < got["diff"] < 0
    assert got["low"] < -0.10
    assert got["verdict"] == verdict.FAIL


def test_같은_결과면_같은_구간이_나온다() -> None:
    base = {f"c{i}": [i % 3 == 0] for i in range(85)}
    new = {f"c{i}": [i % 4 == 0] for i in range(85)}
    assert verdict.paired(base, new) == verdict.paired(base, new)


def test_회차_사이에_섞인_대화를_센다() -> None:
    assert (
        verdict.unstable(
            {"a": [True, False, True], "b": [True, True, True], "c": [False]}
        )
        == 1
    )


def test_묶음이_다르면_견주지_않고_프롬프트가_달라도_견준다() -> None:
    # 프롬프트를 고친 PR 도 판정한다. 프롬프트 판은 판정표 머리에 둘 다 나온다
    a = _file({"x": [True]})
    assert verdict.mismatch(a, _file({"x": [True]})) is None
    assert verdict.mismatch(a, _file({"x": [True]}, prompt="v2")) is None
    assert "bundle_version" in verdict.mismatch(a, _file({"x": [True]}, bundle=8))


def test_보고서에_판정과_엇갈림과_실패_이유가_나온다(tmp_path, capsys) -> None:
    base = _file({f"c{i}": [True, True, i % 5 != 0] for i in range(10)}, model="API")
    new = _file(
        {f"c{i}": [i % 2 == 0] * 3 for i in range(10)},
        model="GPU",
        failure="wrong_kind",
    )
    (tmp_path / "b.json").write_text(json.dumps(base), encoding="utf-8")
    (tmp_path / "n.json").write_text(json.dumps(new), encoding="utf-8")

    assert (
        verdict.main(
            ["--base", str(tmp_path / "b.json"), "--new", str(tmp_path / "n.json")]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "**탈락**" in out
    assert "기준만 5개" in out
    assert "기준 2개, 후보 0개" in out  # 회차 사이에 섞인 대화
    assert "| wrong_kind | 0 | 15 |" in out


def test_한도_기본값은_569_에_고정한_10퍼센트포인트이고_시드도_569다() -> None:
    assert verdict.MARGIN == pytest.approx(0.10)
    assert verdict.SEED == 569


def test_V3_판정과_같은_구간이다() -> None:
    # V3 판정 코드(#569 compare.py)에 같은 값을 넣어 나온 구간이다(evals/stats.py 와 같은 계산)
    base = {f"c{i}": [i % 4 != 0] * 3 for i in range(20)}
    new = {f"c{i}": [i % 5 != 0] * 3 for i in range(20)}
    got = verdict.paired(base, new)
    assert got["diff"] == pytest.approx(0.05)
    assert (got["low"], got["high"]) == pytest.approx((-0.2, 0.3))


def test_묶음을_골라_그_대화만_견준다(tmp_path, capsys) -> None:
    from evals.extraction.run import load_cases

    borrowed = sorted(c["id"] for c in load_cases() if c["group"] == "borrowed")
    other = next(c["id"] for c in load_cases() if c["group"] != "borrowed")
    passes = {case: [True] * 3 for case in borrowed[:4]} | {other: [False] * 3}
    for name in ("b", "n"):
        (tmp_path / f"{name}.json").write_text(json.dumps(_file(passes)), "utf-8")
    argv = ["--base", str(tmp_path / "b.json"), "--new", str(tmp_path / "n.json")]
    assert verdict.main([*argv, "--group", "borrowed"]) == 0
    assert "대화 4개" in capsys.readouterr().out
    assert verdict.main([*argv, "--group", "없는묶음"]) == 2
