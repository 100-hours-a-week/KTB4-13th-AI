"""취향 뽑기 평가 두 결과 짝 비교(evals/extraction/compare.py) 테스트(#605). 답을 아는 결과로 판정을 본다."""

import json

import pytest

from evals.extraction import compare


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
    assert compare.majority([True, True, False]) is True
    assert compare.majority([True, False, False]) is False
    # 짝수 회에서 반반이면 통과로 보지 않는다
    assert compare.majority([True, False]) is False


def test_같으면_넘고_크게_나빠지면_못_넘는다() -> None:
    base = {f"c{i}": [i % 10 < 7] * 3 for i in range(85)}
    same = compare.paired(base, base)
    assert same["diff"] == 0 and same["verdict"] == "넘음"

    worse = {k: [False] * 3 if i < 20 else v for i, (k, v) in enumerate(base.items())}
    got = compare.paired(base, worse)
    assert got["diff"] < -0.10
    assert got["verdict"] == "못 넘음"
    assert got["base_only"] > 0 and got["new_only"] == 0


def test_차이가_작아도_구간이_한도_아래로_내려가면_못_넘음() -> None:
    # 5%p 낮지만 엇갈리는 대화가 많아 구간이 넓다
    base = {f"c{i}": [i % 2 == 0] for i in range(85)}
    new = {
        f"c{i}": [(i % 2 == 1) if i < 40 else (i % 2 == 0) and i % 20 != 0]
        for i in range(85)
    }
    got = compare.paired(base, new)
    assert -0.10 < got["diff"] < 0
    assert got["low"] < -0.10
    assert got["verdict"] == "못 넘음"


def test_같은_결과면_같은_구간이_나온다() -> None:
    base = {f"c{i}": [i % 3 == 0] for i in range(85)}
    new = {f"c{i}": [i % 4 == 0] for i in range(85)}
    assert compare.paired(base, new) == compare.paired(base, new)


def test_회차_사이에_섞인_대화를_센다() -> None:
    assert (
        compare.unstable(
            {"a": [True, False, True], "b": [True, True, True], "c": [False]}
        )
        == 1
    )


def test_묶음이나_프롬프트가_다르면_견주지_않는다() -> None:
    a = _file({"x": [True]})
    assert compare.mismatch(a, _file({"x": [True]})) is None
    assert "prompt_version" in compare.mismatch(a, _file({"x": [True]}, prompt="v2"))
    assert "bundle_version" in compare.mismatch(a, _file({"x": [True]}, bundle=8))


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
        compare.main(
            ["--base", str(tmp_path / "b.json"), "--new", str(tmp_path / "n.json")]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "**못 넘음**" in out
    assert "기준만 5개" in out
    assert "기준 2개, 새 모델 0개" in out  # 회차 사이에 섞인 대화
    assert "| wrong_kind | 0 | 15 |" in out


def test_한도_기본값은_569_에_고정한_10퍼센트포인트() -> None:
    assert compare.MARGIN == pytest.approx(0.10)
