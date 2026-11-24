"""홈 추천 평가 결과 짝 판정(evals/feed/verdict.py, #753) 테스트. 답을 아는 결과로 판정을 본다."""

import json

from evals.feed import run as runner
from evals.feed import verdict
from evals.feed.scoring import Book, Response

KINDS = {
    "one-field": {"group": "typical"},
    "cold": {"group": "typical"},
    "pile": {"group": "guard"},
}


def _score(
    case_id: str,
    kind: str = "one-field",
    *,
    missing: int = 0,
    ok: bool = True,
    substituted: tuple[str, ...] = (),
    editions: int | None = None,
    show: bool = True,
) -> dict:
    return {
        "case_id": case_id,
        "kind": kind,
        "ok": ok,
        "show": {"a": 0 if missing else 3, "b": 3} if show else {},
        "missing": missing,
        "substituted": list(substituted),
        "editions": editions,
    }


def _file(*scores: dict, bundle: int = 2) -> dict:
    return {
        "bundle_version": bundle,
        "catalog": {"books": 10, "with_vectors": 5},
        "database": {"name": "x_eval", "books": 10, "with_vectors": 5},
        "scores": list(scores),
    }


def _typical_and_guard(typical_missing: list[int], guard_missing: list[int]) -> dict:
    return _file(
        *(
            _score(f"one-field-{n:02}", missing=m)
            for n, m in enumerate(typical_missing)
        ),
        *(
            _score(f"pile-{n:02}", "pile", missing=m)
            for n, m in enumerate(guard_missing)
        ),
    )


def test_같은_결과면_통과다() -> None:
    base = _typical_and_guard([0, 0, 0], [1, 0, 0])
    got = verdict.compare(base, base, KINDS)

    assert got.verdict == verdict.PASS
    assert [(g.name, len(g.base), len(g.new)) for g in got.groups] == [
        ("typical", 0, 0),
        ("guard", 1, 1),
    ]


def test_묶음_하나라도_늘면_합계가_같아도_탈락이다() -> None:
    # 보통 사용자 하나가 새로 빠지고 틀리기 쉬운 사용자 하나가 채워졌다. 합치면 1명 → 1명이다.
    base = _typical_and_guard([0, 0], [1, 0])
    new = _typical_and_guard([1, 0], [0, 0])
    got = verdict.compare(base, new, KINDS)

    assert got.verdict == verdict.FAIL
    typical, guard = got.groups
    assert typical.verdict == verdict.FAIL and typical.worse == ["one-field-00"]
    assert guard.verdict == verdict.PASS and guard.better == ["pile-00"]


def test_수가_같으면_바뀐_사용자가_있어도_통과하고_누가_바뀌었는지_적는다() -> None:
    base = _typical_and_guard([0], [1, 0])
    new = _typical_and_guard([0], [0, 2])
    got = verdict.compare(base, new, KINDS)
    text = verdict.render(got, 2, 20)

    assert got.verdict == verdict.PASS
    assert "| guard | 1/2 | 1/2 | pile-01 | pile-00 | 통과 |" in text


def test_나와야_하는_관심사가_없는_사용자는_빠진_사용자가_아니다() -> None:
    cold = _score("cold-01", "cold", show=False)
    base = _file(cold, _score("one-field-01"))
    got = verdict.compare(base, base, KINDS)

    assert verdict.missed(cold) is False
    assert got.groups[0].users == 2 and got.groups[0].base == ()


def test_재지_못하거나_대신_채운_사용자는_견주지_않고_다시_재기다() -> None:
    base = _file(
        _score("one-field-01"), _score("one-field-02"), _score("pile-01", "pile")
    )
    new = _file(
        _score("one-field-01", ok=False),
        _score("one-field-02"),
        _score("pile-01", "pile", substituted=("소설",), missing=1),
    )
    got = verdict.compare(base, new, KINDS)
    text = verdict.render(got, 2, 20)

    assert got.verdict == verdict.REMEASURE
    assert got.skipped == (
        ("후보", "one-field-01", "재지 못함"),
        ("후보", "pile-01", "대신 채움"),
    )
    # 대신 채운 사용자의 빠진 관심사는 세지 않는다
    assert [g.name for g in got.groups] == ["typical"]
    assert "one-field-01(후보 재지 못함)" in text
    assert "알 수 없습니다" in text


def test_남은_사용자만으로_늘었으면_다시_재기보다_탈락이_먼저다() -> None:
    base = _file(_score("one-field-01"), _score("one-field-02"))
    new = _file(_score("one-field-01", ok=False), _score("one-field-02", missing=1))
    got = verdict.compare(base, new, KINDS)

    assert got.verdict == verdict.FAIL
    assert "알 수 없습니다" not in verdict.render(got, 2, 20)


def test_가진_책의_다른_판본이_늘면_표시만_한다() -> None:
    base = _file(_score("one-field-01", editions=1), _score("one-field-02"))
    new = _file(_score("one-field-01", editions=4), _score("one-field-02"))
    got = verdict.compare(base, new, KINDS)

    assert got.verdict == verdict.PASS
    assert got.editions == (1, 4)
    assert "기준 1권 → 후보 4권 (늘었습니다" in verdict.render(got, 2, 20)


def test_새로_빠진_사용자가_많으면_앞의_열_명만_적는다() -> None:
    base = _typical_and_guard([0] * 12, [])
    new = _typical_and_guard([1] * 12, [])
    text = verdict.render(verdict.compare(base, new, KINDS), 2, 20)

    assert "one-field-09 외 2명" in text
    assert "one-field-10" not in text


def test_묶음_책_표_DB_잰_사용자가_다르면_견주지_않는다() -> None:
    base = _typical_and_guard([0], [0])
    assert verdict.mismatch(base, base) is None
    assert "bundle_version" in verdict.mismatch(base, {**base, "bundle_version": 3})
    other_db = {**base, "database": {"name": "x_eval", "books": 11, "with_vectors": 5}}
    assert "database" in verdict.mismatch(base, other_db)
    fewer = {**base, "scores": base["scores"][:1]}
    assert "pile-00" in verdict.mismatch(base, fewer)


def test_명령은_실행기가_남긴_파일을_읽어_판정표를_낸다(tmp_path, capsys) -> None:
    # 실행기의 결과 파일 모양이 바뀌어 판정이 못 읽게 되는 것을 막는다
    def book(isbn: str) -> Book:
        return Book(
            isbn,
            f"책 {isbn}",
            "저자",
            "한국문학",
            "813.7",
            "0",
            300,
            2020,
            15000,
            "추리",
        )

    async def target(case: dict) -> Response:
        cold = bool(case["expect"].get("cold_start"))
        return Response(200, 200, cold, None, (f"{case['user_id']}1",), 0.01)

    async def facts(isbns: set[str]) -> dict[str, Book]:
        return {isbn: book(isbn) for isbn in isbns}

    out = tmp_path / "base.json"
    ids = "one-field-01,pile-01,owned-01,cold-01"
    assert (
        runner.main(
            ["--ids", ids, "--yes", "--out", str(out)], target=target, facts=facts
        )
        == 0
    )
    capsys.readouterr()

    assert verdict.main(["--base", str(out), "--new", str(out)]) == 0
    text = capsys.readouterr().out
    assert "판정: **통과**" in text
    assert "| typical |" in text and "| guard |" in text


def test_명령은_견줄_수_없으면_멈춘다(tmp_path, capsys) -> None:
    bundle = runner.load_bundle()
    base, new = tmp_path / "base.json", tmp_path / "new.json"
    base.write_text(json.dumps(_file(_score("one-field-01"), bundle=bundle["version"])))
    new.write_text(json.dumps(_file(_score("one-field-02"), bundle=bundle["version"])))
    assert verdict.main(["--base", str(base), "--new", str(new)]) == 2
    assert "잰 사용자가 다르다" in capsys.readouterr().err

    # 종류와 묶음은 지금 cases.json 에서 읽으니, 그 판과 다른 결과는 견주지 않는다
    old = tmp_path / "old.json"
    old.write_text(
        json.dumps(_file(_score("one-field-01"), bundle=bundle["version"] - 1))
    )
    assert verdict.main(["--base", str(old), "--new", str(old)]) == 2
    assert "cases.json" in capsys.readouterr().err
