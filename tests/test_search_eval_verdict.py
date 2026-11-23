"""검색 평가 결과 짝 판정(evals/search/verdict.py, #752) 테스트. DB 없이 돈다."""

import json
from pathlib import Path

import pytest

from evals.search import verdict
from evals.search.verdict import Run


def _meta(**over) -> dict:
    return {
        "bundle_version": 3,
        "catalog": {"books": 1},
        "database": {"name": "x_eval", "books": 1, "with_vectors": 1},
        "sort": "popular",
        "top": 12,
        "look": 12,
        **over,
    }


def _title(i: int, hit: bool, kind: str = "title", ok: bool = True) -> dict:
    return {
        "id": f"{kind}-{i}",
        "kind": kind,
        "ok": ok,
        "rank": 1 if hit else 2,
        "book_ids": [i],
    }


def _author(i: int, share: float) -> dict:
    return {
        "id": f"author-{i}",
        "kind": "author",
        "ok": True,
        "author_share": share,
        "book_ids": [i],
    }


def _filter(i: int, violations: int) -> dict:
    return {
        "id": f"filter-{i}",
        "kind": "filter",
        "ok": True,
        "violations": violations,
        "book_ids": [i],
    }


def _described(i: int, ndcg: float, judged: float = 1.0) -> dict:
    return {
        "id": f"descriptive-{i}",
        "kind": "descriptive",
        "ok": True,
        "ndcg": ndcg,
        "ndcg_labeled": ndcg,
        "irrelevant": 0,
        "judged": judged,
        "book_ids": [i],
    }


def _run(rows: list[dict], name: str = "new", **meta) -> Run:
    return Run(name, _meta(**meta), {row["id"]: row for row in rows})


def _line(result: verdict.Comparison, starts: str) -> verdict.Line:
    return next(x for x in result.lines if x.name.startswith(starts))


def test_제목은_맞힌_수가_2건_이상_줄어야_탈락이다() -> None:
    base = _run([_title(i, True) for i in range(10)], "base")
    one = _run([_title(i, i != 0) for i in range(10)])
    two = _run([_title(i, i > 1) for i in range(10)])
    assert _line(verdict.compare(base, one), "제목").verdict == verdict.PASS
    assert _line(verdict.compare(base, two), "제목").verdict == verdict.FAIL
    # 잃은 만큼 얻으면 줄지 않았다
    back = _run([_title(i, i not in (0, 1)) for i in range(10)], "base")
    swap = _run([_title(i, i not in (2, 3)) for i in range(10)])
    assert _line(verdict.compare(back, swap), "제목").verdict == verdict.PASS


def test_표기_변형은_McNemar_로_탈락을_구간으로_좋아짐을_본다() -> None:
    def rows(hits: set[int]) -> list[dict]:
        return [_title(i, i in hits, "title-variant") for i in range(100)]

    base = _run(rows(set(range(80))), "base")
    fell5 = _run(rows(set(range(5, 80))))
    fell6 = _run(rows(set(range(6, 80))))
    rose = _run(rows(set(range(90))))
    assert _line(verdict.compare(base, fell5), "표기").verdict == verdict.UNCLEAR
    assert _line(verdict.compare(base, fell6), "표기").verdict == verdict.FAIL
    assert _line(verdict.compare(base, rose), "표기").verdict == verdict.BETTER


def test_저자는_구간_하한이_영점영이_아래면_탈락이고_그대로면_통과다() -> None:
    base = _run([_author(i, 1.0) for i in range(30)], "base")
    same = _run([_author(i, 1.0) for i in range(30)])
    fell = _run([_author(i, 1.0 if i % 2 else 0.9) for i in range(30)])
    assert _line(verdict.compare(base, same), "저자").verdict == verdict.PASS
    assert _line(verdict.compare(base, fell), "저자").verdict == verdict.FAIL


def test_필터는_조건을_어긴_책이_1권이라도_나오면_탈락이다() -> None:
    base = _run([_filter(i, 0) for i in range(5)], "base")
    new = _run([_filter(i, 1 if i == 0 else 0) for i in range(5)])
    assert verdict.compare(base, new).verdict == verdict.FAIL


def test_서술형은_자동으로_탈락시키지_않고_좋아지면_후보다() -> None:
    base = _run([_described(i, 0.5) for i in range(40)], "base")
    rose = _run([_described(i, 0.6 + 0.001 * i) for i in range(40)])
    fell = _run([_described(i, 0.3 + 0.001 * i) for i in range(40)])
    assert verdict.compare(base, rose).verdict == verdict.BETTER
    result = verdict.compare(base, fell)
    assert result.verdict == verdict.UNCLEAR
    assert "확실히 내려감" in _line(result, "서술형 nDCG").detail


def test_Judged_가_모자라면_라벨을_보충한_뒤_판정하되_탈락은_먼저다() -> None:
    base = _run([_described(i, 0.5) for i in range(40)], "base")
    thin = _run([_described(i, 0.7, judged=0.8) for i in range(40)])
    assert verdict.compare(base, thin).verdict == verdict.NEEDS_LABELS
    both = [_filter(1, 0)]
    bad = _run([_described(i, 0.7, judged=0.8) for i in range(40)] + [_filter(1, 2)])
    assert (
        verdict.compare(
            _run([_described(i, 0.5) for i in range(40)] + both, "base"), bad
        ).verdict
        == verdict.FAIL
    )


def test_재지_못한_사례가_있으면_판정하지_않고_다시_잰다() -> None:
    base = _run([_title(i, True) for i in range(5)], "base")
    new = _run([_title(i, True, ok=i != 0) for i in range(5)])
    result = verdict.compare(base, new)
    assert result.verdict == verdict.REMEASURE
    assert result.dropped == ["title-0"]


def test_관련도순_파일은_원리_확인용이고_판정하지_않는다() -> None:
    rows = [_title(i, True) for i in range(5)]
    base = _run(rows, "base", sort="relevance", top=20, look=None)
    new = _run(rows, sort="relevance", top=20, look=None)
    assert verdict.compare(base, new).verdict == verdict.NOT_JUDGED


def test_정렬이나_DB_가_다른_파일끼리는_견주지_않는다() -> None:
    rows = [_title(i, True) for i in range(5)]
    with pytest.raises(ValueError, match="sort"):
        verdict.compare(_run(rows, "base"), _run(rows, sort="relevance"))
    with pytest.raises(ValueError, match="database"):
        verdict.compare(_run(rows, "base"), _run(rows, database={"name": "y_eval"}))


def test_위_권이_바뀐_사례는_보는_권수까지만_센다() -> None:
    base = _run([{**_title(1, True), "book_ids": [1] * 12 + [2]}], "base")
    new = _run([{**_title(1, True), "book_ids": [1] * 12 + [3]}])
    assert verdict.compare(base, new).changed == {"title": 0}


def test_서술형은_파일의_숫자가_아니라_지금의_라벨로_다시_매긴다(
    tmp_path: Path,
) -> None:
    row = {
        **_described(1, 0.0),
        "book_ids": [1],
        "isbns": ["9780000000001"],
    }
    path = tmp_path / "a.json"
    path.write_text(json.dumps({**_meta(), "scores": [row]}), encoding="utf-8")
    labels = {"descriptive-1": {"9780000000001": 2}}
    again = verdict.load(path, labels).rows["descriptive-1"]
    assert again["ndcg"] > 0 and again["judged"] == pytest.approx(1 / 12)


def test_ISBN_이_없는_파일은_다시_매길_수_없어_멈춘다(tmp_path: Path) -> None:
    path = tmp_path / "a.json"
    path.write_text(
        json.dumps({**_meta(), "scores": [_described(1, 0.1)]}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="ISBN"):
        verdict.load(path, {})


def test_후보_확인은_Holm_을_거쳐_좋아진_쪽만_넘긴다() -> None:
    base = _run([_described(i, 0.5) for i in range(40)], "base")
    good = _run([_described(i, 0.7 + 0.001 * i) for i in range(40)], "good")
    worse = _run([_described(i, 0.3 + 0.001 * i) for i in range(40)], "worse")
    results = {
        name: passed for name, _, passed, _ in verdict.confirm(base, [good, worse])
    }
    assert results == {"good": True, "worse": False}


def test_확인은_따로_준_라벨_파일로_다시_매긴다(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    def write(name: str, isbn: str) -> Path:
        row = {**_described(1, 0.0), "isbns": [isbn]}
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({**_meta(), "scores": [row]}), encoding="utf-8")
        return path

    base, new = write("base", "9780000000002"), write("new", "9780000000001")
    labels = tmp_path / "l.json"
    labels.write_text(
        json.dumps({"labels": {"descriptive-1": {"9780000000001": 2}}}),
        encoding="utf-8",
    )
    pair = ["confirm", "--base", str(base), "--new", str(new)]

    assert verdict.main(["--labels", str(labels), *pair]) == 0
    # 새 안의 1등이 2점 책이다. 이상값은 위 12권이 모두 2점이라 3 / 15.3
    assert "new: 차이 +0.196" in capsys.readouterr().out

    # 라벨 파일을 주지 않으면 평가 묶음의 라벨이라 두 책 모두 점수가 없다
    monkeypatch.setattr(verdict, "load_labels", lambda: {"labels": {}})
    assert verdict.main(pair) == 0
    assert "new: 차이 +0.000" in capsys.readouterr().out


def test_실행기가_남긴_첫_화면_파일_둘을_그대로_판정한다(
    tmp_path: Path, capsys
) -> None:
    from evals.search import run
    from evals.search.scoring import Book

    title = next(c for c in run.load_bundle()["cases"] if c["kind"] == "title")
    shelf = {
        1: Book(
            1, "9780000000001", title["expect"]["titles"][0], None, None, None, None
        ),
        2: Book(2, "9780000000002", "다른 책", None, None, None, None),
    }

    def search(requests: list[dict]) -> list[dict]:
        return [
            {"status": 200, "degraded": None, "book_ids": [1, 2], "seconds": 0.1}
            for _ in requests
        ]

    for name in ("base", "new"):
        argv = ["--ids", f"{title['id']},descriptive-01", "--screen", "--yes"]
        argv += ["--out", str(tmp_path / f"{name}.json")]
        assert run.main(argv, search=search, facts=lambda ids: shelf) == 0
    capsys.readouterr()

    pair = ["--base", str(tmp_path / "base.json"), "--new", str(tmp_path / "new.json")]
    assert verdict.main(["compare", *pair]) == 0
    printed = capsys.readouterr().out
    assert "| 제목 1건 1등 | 1 | 1 | 잃음 0 · 얻음 0 | 통과 |" in printed
    # 두 책 모두 라벨이 없어 Judged 가 0 이다. 탈락이 없으니 라벨을 보충한 뒤 판정한다
    assert "판정: **라벨 보충 뒤 판정**" in printed


def test_관련도순_파일과_첫_화면_파일은_견주지_않는다(tmp_path: Path, capsys) -> None:
    rows = [_title(i, True) for i in range(3)]
    for name, meta in (("base", _meta()), ("new", _meta(sort="relevance", top=20))):
        (tmp_path / f"{name}.json").write_text(
            json.dumps({**meta, "scores": rows}), encoding="utf-8"
        )
    pair = ["--base", str(tmp_path / "base.json"), "--new", str(tmp_path / "new.json")]
    assert verdict.main(["compare", *pair]) == 2
    assert "sort 가 다르다" in capsys.readouterr().err
