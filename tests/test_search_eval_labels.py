"""① 검색 평가 서술형 라벨(evals/search/labels.json, confirm-labels.json)의 형식 검사(#502·#751). DB 없이 돈다."""

import json
import re
from pathlib import Path

import pytest

from evals.search import label, run

_DIR = Path(__file__).resolve().parents[1] / "evals" / "search"


def _descriptive(cases: list[dict]) -> set[str]:
    return {case["id"] for case in cases if case["kind"] == "descriptive"}


BUNDLES = {
    "labels.json": _descriptive(run.load_bundle()["cases"]),
    "confirm-labels.json": _descriptive(
        json.loads((_DIR / "confirm.json").read_text(encoding="utf-8"))["cases"]
    ),
}


@pytest.mark.parametrize("name", BUNDLES)
def test_서술형_검색어마다_라벨이_있고_점수는_0_1_2_다(name: str) -> None:
    data = label.load_labels(_DIR / name)
    assert set(data["labels"]) == BUNDLES[name]
    for case_id, marks in data["labels"].items():
        assert marks, case_id
        for isbn, grade in marks.items():
            assert re.fullmatch(r"97[89]\d{10}", isbn), (case_id, isbn)
            assert grade in (0, 1, 2), (case_id, isbn)


@pytest.mark.parametrize("name", BUNDLES)
def test_누가_몇_개를_매겼고_사람이_몇_개를_보고_고쳤는지_남아_있다(name: str) -> None:
    data = label.load_labels(_DIR / name)
    total = sum(len(marks) for marks in data["labels"].values())
    assert sum(judge["labels"] for judge in data["judges"]) == total
    review = data["review"]
    if review is None:
        return
    assert review["by"]
    assert 0 < review["sampled"] <= total
    assert 0 <= review["changed"] <= review["sampled"]
