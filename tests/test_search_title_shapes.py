"""제목 검색 꼴 점검(evals/searchlab/title_shapes.py) 테스트. DB 없이 돈다."""

from evals.search.scoring import Book
from evals.searchlab import title_shapes


def _title(i: int, query: str) -> dict:
    return {"query": query, "isbn": str(i), "expect": {"titles": [query]}}


def test_꼴마다_검색어를_만들고_같은_시드면_같다() -> None:
    titles = [_title(0, "세법워크북")] + [
        _title(i, f"가나다 라마바 {i}") for i in range(1, 400)
    ]
    authors = {str(i): f"저자{i}, 옮긴이" for i in range(400)}
    cases = title_shapes.build(titles, authors)
    assert cases == title_shapes.build(titles, authors)
    groups = {c["group"] for c in cases}
    assert groups == {
        "띄어 침",
        "끝 자모",
        "글자 바뀜",
        "제목 + 책 추천",
        "제목 + 책",
        "제목 + 저자",
    }
    assert next(c for c in cases if c["group"] == "띄어 침")["query"] == "세법 워크북"
    by_author = next(c for c in cases if c["group"] == "제목 + 저자")
    assert by_author["query"].endswith(" 저자" + by_author["query"].split("저자")[-1])


def test_꼴마다_1등과_12권_안을_센다() -> None:
    def book(i: int, title: str) -> Book:
        return Book(i, None, title, None, None, None, None)

    cases = [
        {"group": "g", "query": "a", "expect": {"titles": ["별"]}},
        {"group": "g", "query": "b", "expect": {"titles": ["달"]}},
        {"group": "g", "query": "c", "expect": {"titles": ["해"]}},
    ]
    shelf = {1: book(1, "별"), 2: book(2, "달"), 3: book(3, "산")}
    results = [{"book_ids": [1]}, {"book_ids": [3, 2]}, {"book_ids": [3]}]
    assert title_shapes.count(cases, results, shelf) == {"g": [3, 1, 2]}
