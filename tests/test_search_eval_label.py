"""① 서술형 검색어에 점수를 매기는 도구(evals/search/label.py) 테스트(#502). DB 와 앱 없이 돈다."""

import json
from pathlib import Path

import pytest

from evals.search import label

CASE = {
    "id": "descriptive-01",
    "kind": "descriptive",
    "facet": "topic",
    "query": "우주",
}


def _books(*ids: int) -> dict[int, dict]:
    return {
        i: {
            "isbn13": f"97800000000{i:02d}",
            "title": f"책 {i}",
            "author": "저자",
            "category": "천문학",
            "intro": "소개글\n둘째 줄",
        }
        for i in ids
    }


def test_여러_코드의_위_권들을_ISBN으로_합치고_점수가_있는_책은_뺀다() -> None:
    books = _books(1, 2, 3, 4)
    found = {"main": [{"book_ids": [1, 2, 3]}], "ef200": [{"book_ids": [2, 4]}]}
    labels = {CASE["id"]: {books[3]["isbn13"]: 1}}
    text, grades, sources = label.gather([CASE], found, books, labels)

    assert list(grades[CASE["id"]].values()) == [None, None, None]
    by_isbn = {s["isbn"]: s["ranks"] for s in sources[CASE["id"]].values()}
    assert by_isbn == {
        books[1]["isbn13"]: {"main": 1},
        books[2]["isbn13"]: {"main": 2, "ef200": 1},
        books[4]["isbn13"]: {"ef200": 2},
    }
    # 채점 파일에는 어느 코드의 몇 등이었는지가 없다. 소개글은 한 줄로 편다
    assert "## descriptive-01 · topic · 우주" in text
    assert "main" not in text and "ef200" not in text
    assert "| 소개글 둘째 줄" in text
    # 같은 입력이면 같은 순서로 섞는다
    assert label.gather([CASE], found, books, labels) == (text, grades, sources)


def test_매긴_점수를_라벨에_넣고_빈칸과_이미_있는_라벨은_건너뛴다(
    tmp_path: Path,
) -> None:
    data = label.load_labels(tmp_path / "없는 파일.json")
    sources = {
        "d-1": {"c01": {"isbn": "B"}, "c02": {"isbn": "A"}, "c03": {"isbn": "C"}}
    }
    grades = {"d-1": {"c01": 2, "c02": 0, "c03": None}}

    assert label.apply(grades, sources, data, "세션, 10/19") == 2
    assert data["labels"] == {"d-1": {"A": 0, "B": 2}}
    # 다시 넣어도 더하지 않고, 판정자별 권수도 그대로다
    assert label.apply(grades, sources, data, "세션, 10/19") == 0
    assert data["judges"] == [{"by": "세션, 10/19", "labels": 2}]
    with pytest.raises(ValueError, match="0, 1, 2"):
        label.apply({"d-1": {"c03": 3}}, sources, data, "세션, 10/19")


def test_사람_확인은_시드로_10퍼센트를_고르고_고친_수를_남긴다() -> None:
    data = {
        "labels": {f"d-{i}": {f"isbn-{j}": j % 3 for j in range(5)} for i in range(5)}
    }
    review = label.pick_review(data)
    assert len(review) == 3  # 25개의 10%를 올림
    assert review == label.pick_review(data)

    marks = {f"r{n:02d}": item for n, item in enumerate(review, 1)}
    first, second = marks["r01"], marks["r02"]
    first["fixed"] = (first["grade"] + 1) % 3
    second["fixed"] = second["grade"]  # 같은 점수는 고친 것이 아니다
    assert label.apply_review(marks, data, "사용자, 10/19") == 1
    assert data["labels"][first["case"]][first["isbn"]] == first["fixed"]
    assert (data["review"]["sampled"], data["review"]["changed"]) == (3, 1)


def test_라벨_파일은_검색어마다_한_줄이다() -> None:
    data = {"version": 1, "labels": {"d-1": {"A": 2}, "d-2": {"B": 0}}}
    text = label.dump(data, "labels")
    assert json.loads(text) == data
    assert '  "d-1": {"A": 2},\n  "d-2": {"B": 0}\n' in text
