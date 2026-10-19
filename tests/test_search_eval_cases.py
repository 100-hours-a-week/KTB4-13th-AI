"""① 검색 평가 묶음(evals/search/cases.json)의 형식 검사(#495). DB 없이 돈다.

묶음을 돌려 채점하는 것은 책 211만 권이 든 평가 DB 가 있어야 해서 CI 에서 하지 않는다. 여기서는 묶음 자체가
깨지지 않았는지만 본다. 형식이 어긋나면 실행기가 그 사례를 틀리게 채점한다.
"""

import json
import re
from collections import Counter
from pathlib import Path

from app.core import editions
from app.core.isbn import ISBN13_PATTERN
from app.routers.search import parse_request
from app.search.schemas import MAX_SIZE, SearchFilters
from evals.search.build import CHANGES, change_title, person_like

_PATH = Path(__file__).resolve().parents[1] / "evals" / "search" / "cases.json"
_DATA = json.loads(_PATH.read_text(encoding="utf-8"))
_CASES = _DATA["cases"]
_BY_ID = {case["id"]: case for case in _CASES}
COUNTS = {"title": 300, "title-variant": 100, "author": 30, "filter": 20, "nonsense": 5}


def _of(kind: str) -> list[dict]:
    return [case for case in _CASES if case["kind"] == kind]


def test_묶음에_버전과_책_표의_정보가_있다() -> None:
    # 사례를 고치면 전후 숫자를 견줄 수 없다. 버전을 올려 결과에 함께 적는다.
    assert isinstance(_DATA["version"], int) and _DATA["version"] >= 1
    # 사례마다 /search 를 한 번 부른다. 한 번에 받을 수 있는 권수를 넘으면 안 된다.
    assert 20 <= _DATA["top"] <= MAX_SIZE
    assert set(_DATA["catalog"]) == {"built", "books", "with_vectors"}
    assert 0 < _DATA["catalog"]["with_vectors"] <= _DATA["catalog"]["books"]


def test_종류마다_사례_수가_정해진_대로다() -> None:
    assert Counter(case["kind"] for case in _CASES) == COUNTS
    assert len(_BY_ID) == len(_CASES)
    for case in _CASES:
        assert re.fullmatch(rf"{case['kind']}-\d+", case["id"]), case["id"]


def test_사례마다_검색_API가_받는_요청이다() -> None:
    # 라우터의 요청 검사를 그대로 거친다. 분야가 온보딩 값이 아니거나 검색어가 비면 400 이라 재지 못한다.
    for case in _CASES:
        body = {
            "query": case["query"],
            "filters": case.get("filters", {}),
            "size": _DATA["top"],
        }
        assert parse_request(body) is not None, case["id"]


def test_제목_사례는_그_책의_제목을_그대로_친다() -> None:
    titles = _of("title")
    for case in titles:
        assert re.fullmatch(ISBN13_PATTERN, case["isbn"]), case["id"]
        assert case["expect"]["titles"] == [case["query"]], case["id"]
    # 같은 제목을 두 번 재지 않는다
    keys = [editions.title_key(case["query"]) for case in titles]
    assert all(keys) and len(set(keys)) == len(keys)


def test_표기_변형은_원래_제목을_정해진_방법으로_바꾼_것이다() -> None:
    variants = _of("title-variant")
    assert Counter(case["change"] for case in variants) == CHANGES
    # 책 한 권은 한 번만 바꾼다
    assert len({case["isbn"] for case in variants}) == len(variants)
    for case in variants:
        original = case["expect"]["titles"][0]
        assert change_title(original, case["change"]) == case["query"], case["id"]
        if case["change"] == "no-subtitle":
            assert case["expect"]["titles"] == [original, case["query"]]
        else:
            assert case["expect"]["titles"] == [original]
        if case["of"] is not None:
            source = _BY_ID[case["of"]]
            assert (source["kind"], source["isbn"]) == ("title", case["isbn"])


def test_저자_사례는_사람_이름을_치고_그_이름의_책을_찾는다() -> None:
    authors = _of("author")
    assert len({case["query"] for case in authors}) == len(authors)
    for case in authors:
        assert case["expect"] == {"author": case["query"]}
        assert person_like(case["query"]), case["id"]


def test_필터_사례는_검색이_받는_조건을_건다() -> None:
    fields = set(SearchFilters.model_fields)
    for case in _of("filter"):
        assert case["filters"] and set(case["filters"]) <= fields, case["id"]
    for case in _of("nonsense"):
        assert "filters" not in case and "expect" not in case
