"""확인용 서술형 검색어(evals/search/confirm.json)의 형식 검사(#556 판정 기준 4). DB 없이 돈다.

마지막 후보에만 한 번 쓰는 검색어라, 평가 묶음(cases.json)과 겹치면 튜닝에 쓴 말로 확인하는 셈이 된다.
"""

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parents[1] / "evals" / "search"


def _load(name: str) -> dict:
    return json.loads((_DIR / name).read_text(encoding="utf-8"))


def test_확인용_검색어는_서술형_40건이고_번호와_검색어가_겹치지_않는다() -> None:
    cases = _load("confirm.json")["cases"]

    assert [c["id"] for c in cases] == [f"confirm-{n:02d}" for n in range(1, 41)]
    assert {c["kind"] for c in cases} == {"descriptive"}
    assert len({c["query"] for c in cases}) == 40


def test_확인용_검색어는_평가_묶음에_없다() -> None:
    confirm = {c["query"] for c in _load("confirm.json")["cases"]}
    bundle = {c["query"] for c in _load("cases.json")["cases"]}

    assert not confirm & bundle


def test_확인용_검색어의_종류는_평가_묶음_서술형과_같은_비율이다() -> None:
    def facets(cases: list[dict]) -> list[str]:
        return sorted(c["facet"] for c in cases if c["kind"] == "descriptive")

    assert facets(_load("confirm.json")["cases"]) == facets(
        _load("cases.json")["cases"]
    )


def test_확인용_묶음은_평가_묶음과_같은_책_표로_만든다() -> None:
    assert _load("confirm.json")["catalog"] == _load("cases.json")["catalog"]
