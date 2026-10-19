"""① 검색 평가 서술형 라벨(evals/search/labels.json)의 형식 검사(#502). DB 없이 돈다."""

import re

from evals.search import label, run

DATA = label.load_labels()
DESCRIPTIVE = {
    case["id"] for case in run.load_bundle()["cases"] if case["kind"] == "descriptive"
}


def test_서술형_검색어마다_라벨이_있고_점수는_0_1_2_다() -> None:
    assert set(DATA["labels"]) == DESCRIPTIVE
    for case_id, marks in DATA["labels"].items():
        assert marks, case_id
        for isbn, grade in marks.items():
            assert re.fullmatch(r"97[89]\d{10}", isbn), (case_id, isbn)
            assert grade in (0, 1, 2), (case_id, isbn)


def test_누가_몇_개를_매겼고_사람이_몇_개를_보고_고쳤는지_남아_있다() -> None:
    total = sum(len(marks) for marks in DATA["labels"].values())
    assert sum(judge["labels"] for judge in DATA["judges"]) == total
    review = DATA["review"]
    assert review["by"]
    assert 0 < review["sampled"] <= total
    assert 0 <= review["changed"] <= review["sampled"]
