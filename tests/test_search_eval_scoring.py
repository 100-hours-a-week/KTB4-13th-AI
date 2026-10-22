"""① 검색 평가 묶음의 채점(evals/search/scoring.py) 테스트(#495, #502). DB 없이 돈다."""

import math

import pytest

from evals.search.scoring import (
    Book,
    Response,
    author_share,
    breaks,
    score,
    summarize,
    title_rank,
)


def _book(book_id: int = 1, **over) -> Book:
    base = {
        "book_id": book_id,
        "isbn": f"978000000{book_id:04d}",
        "title": "제목",
        "author": "저자",
        "category": "한국문학",
        "pub_year": 2020,
        "price": 15000,
    }
    return Book(**{**base, **over})


def _title_case(*titles: str, kind: str = "title", change: str | None = None) -> dict:
    case = {"id": f"{kind}-001", "kind": kind, "expect": {"titles": list(titles)}}
    if change:
        case["change"] = change
    return case


def _response(*book_ids: int, status: int = 200, degraded: str | None = None):
    return Response(status=status, degraded=degraded, book_ids=book_ids, seconds=0.1)


def test_제목은_띄어쓰기_문장부호_영문_대소문자를_빼고_견준다() -> None:
    books = [_book(1, title="다른 책"), _book(2, title="희망의속삭임!")]
    assert title_rank(_title_case("희망의 속삭임"), books) == 2
    assert title_rank(_title_case("Clean Code"), [_book(1, title="CLEAN CODE")]) == 1


def test_제목이_같으면_저자가_달라도_맞다() -> None:
    # 검색어에 저자가 없으니 제목이 같은 다른 책이 1등이어도 검색은 맞게 한 것이다
    books = [_book(1, title="고양이", author="최정화")]
    assert title_rank(_title_case("고양이"), books) == 1


def test_정답_제목이_여럿이면_어느_것이든_맞다() -> None:
    # 부제를 뺀 검색어는 부제 없는 제목의 책도 정답이다
    case = _title_case("정보: 코드와 비트", "정보", change="no-subtitle")
    assert title_rank(case, [_book(1, title="정보")]) == 1


def test_위_N권에_없거나_책_표에서_찾지_못한_책은_맞지_않다() -> None:
    assert title_rank(_title_case("희망의 속삭임"), [_book(1), None]) is None


def test_저자는_위_10권_중_이름이_통째로_든_책의_비율이다() -> None:
    case = {"id": "author-01", "kind": "author", "expect": {"author": "한강"}}
    books = [
        _book(1, author="한강"),
        _book(2, author="한강, 데버라 스미스"),
        _book(3, author="한강준"),
        None,
    ]
    # 결과가 10권보다 적어도 10으로 나눈다
    assert author_share(case, books) == pytest.approx(0.2)
    assert author_share(case, [_book(i, author="한강") for i in range(12)]) == 1.0


def test_분야_조건은_검색과_같은_대응표로_가른다() -> None:
    assert not breaks({"category": "소설"}, _book(category="한국문학"))
    assert breaks({"category": "소설"}, _book(category="경제학"))
    # 어린이는 분류로 가를 수 없어 거르지 않는다
    assert not breaks({"category": "어린이"}, _book(category="경제학"))


def test_출간연도와_가격은_구간을_보고_값이_없으면_어긴_것이다() -> None:
    years = {"pub_year_from": 2015, "pub_year_to": 2020}
    assert not breaks(years, _book(pub_year=2015))
    assert breaks(years, _book(pub_year=2021))
    assert breaks(years, _book(pub_year=None))
    assert not breaks({"price_max": 15000}, _book(price=15000))
    assert breaks({"price_min": 20000}, _book(price=15000))
    assert breaks({"price_min": 20000}, _book(price=None))


def test_책_표에서_찾지_못한_책은_조건을_어긴_것으로_센다() -> None:
    assert breaks({}, None)
    assert not breaks({}, _book())


def test_사례마다_종류에_맞는_칸을_채운다() -> None:
    shelf = {1: _book(1, title="다른 책"), 2: _book(2, title="희망의 속삭임")}
    title = score(_title_case("희망의 속삭임"), _response(1, 2), shelf, top=20)
    assert (title.rank, title.returned, title.ok) == (2, 2, True)
    assert title.author_share is None and title.violations is None

    case = {"id": "filter-01", "kind": "filter", "filters": {"price_max": 10000}}
    assert score(case, _response(1, 2, 3), shelf, top=20).violations == 3
    # 위 top 권만 본다
    assert score(case, _response(1, 2, 3), shelf, top=1).violations == 1


def test_200이_아니거나_줄여_답했으면_잰_것으로_치지_않는다() -> None:
    case = _title_case("제목")
    assert not score(case, _response(status=500), {}, top=20).ok
    keyword_only = _response(1, degraded="keyword-only")
    assert not score(case, keyword_only, {1: _book(1)}, top=20).ok


def test_제목_종류는_적중률과_MRR과_못_찾음을_낸다() -> None:
    shelf = {1: _book(1, title="맞는 책"), 2: _book(2, title="다른 책")}
    rows = [[1], [2, 2, 1], [2], [2] * 19 + [1]]  # 1등, 3등, 못 찾음, 20등
    scores = [
        score(_title_case("맞는 책"), _response(*ids), shelf, top=20) for ids in rows
    ]
    scores.append(score(_title_case("맞는 책"), _response(status=500), shelf, top=20))
    row = summarize(scores)["kinds"]["title"]
    assert (row["cases"], row["measured"]) == (5, 4)
    assert (row["hit_1"], row["hit_5"], row["hit_20"]) == (0.25, 0.5, 0.75)
    assert row["mrr"] == pytest.approx((1 + 1 / 3 + 0 + 1 / 20) / 4, abs=1e-3)
    assert row["missed"] == 1


def test_표기_변형은_바꾼_방법별로도_낸다() -> None:
    shelf = {1: _book(1, title="삼국지 2")}
    scores = [
        score(
            _title_case("삼국지 2", kind="title-variant", change=change),
            _response(*ids),
            shelf,
            top=20,
        )
        for change, ids in (("volume-circled", [1]), ("volume-roman", []))
    ]
    summary = summarize(scores)
    assert summary["kinds"]["title-variant"]["hit_1"] == 0.5
    assert summary["changes"]["volume-circled"]["hit_1"] == 1.0
    assert summary["changes"]["volume-roman"]["missed"] == 1


def test_저자_필터_없는_말은_각자의_숫자를_낸다() -> None:
    shelf = {i: _book(i, author="한강", price=20000) for i in range(1, 21)}
    author = {"id": "author-01", "kind": "author", "expect": {"author": "한강"}}
    cheap = {"id": "filter-01", "kind": "filter", "filters": {"price_max": 10000}}
    nonsense = {"id": "nonsense-1", "kind": "nonsense", "expect": {}}
    ids = tuple(range(1, 21))
    kinds = summarize(
        [
            score(case, _response(*ids), shelf, top=20)
            for case in (author, cheap, nonsense)
        ]
    )["kinds"]
    assert kinds["author"]["author_share_mean"] == 1.0
    assert (kinds["filter"]["violations"], kinds["filter"]["violating_cases"]) == (
        20,
        1,
    )
    assert kinds["nonsense"]["returned_mean"] == 20.0
    assert "hit_1" not in kinds["nonsense"]


def test_없는_말과_키워드_0건인_있는_말은_결과가_빈_사례를_센다() -> None:
    shelf = {i: _book(i) for i in range(1, 21)}
    cases = [
        ({"id": "nonsense-1", "kind": "nonsense"}, _response(*range(1, 21))),
        ({"id": "nonsense-2", "kind": "nonsense"}, _response()),
        ({"id": "no-keyword-01", "kind": "no-keyword"}, _response(1, 2)),
        ({"id": "no-keyword-02", "kind": "no-keyword"}, _response()),
    ]
    kinds = summarize([score(case, resp, shelf, top=20) for case, resp in cases])[
        "kinds"
    ]
    assert kinds["nonsense"]["empty"] == 1
    assert kinds["no-keyword"]["empty"] == 1
    assert kinds["no-keyword"]["returned_mean"] == 1.0


def _descriptive_case(facet: str = "topic") -> dict:
    return {"id": f"descriptive-{facet}", "kind": "descriptive", "facet": facet}


# 위 10권이 모두 2점일 때의 값. nDCG 의 이상값이다
IDEAL = sum(3 / math.log2(rank + 1) for rank in range(1, 11))


def test_서술형은_위_10권이_모두_2점인_순서를_이상값으로_nDCG를_낸다() -> None:
    shelf = {i: _book(i) for i in range(1, 13)}
    case = _descriptive_case()
    labels = {case["id"]: {shelf[1].isbn: 2, shelf[2].isbn: 1, shelf[3].isbn: 0}}
    # 0점, 2점, 1점 순서
    s = score(case, _response(3, 1, 2), shelf, top=20, labels=labels)
    assert s.ndcg == pytest.approx((3 / math.log2(3) + 1 / 2) / IDEAL)
    assert (s.relevant, s.irrelevant, s.unlabeled) == (1, 1, 0)
    # 위 10권이 모두 2점이면 1이고, 11등의 책은 세지 않는다
    labels = {case["id"]: {shelf[i].isbn: 2 for i in range(1, 13)}}
    full = score(case, _response(*range(1, 13)), shelf, top=20, labels=labels)
    assert full.ndcg == pytest.approx(1.0) and full.relevant == 10


def test_2점_책이_없으면_위_10권이_모두_1점이어도_3분의_1이다() -> None:
    # 매긴 책 가운데 가장 좋은 순서를 이상값으로 쓰면 이 경우가 1이 된다(제목에 낱말만 든 책만 나온 검색어)
    shelf = {i: _book(i) for i in range(1, 11)}
    case = _descriptive_case()
    labels = {case["id"]: {book.isbn: 1 for book in shelf.values()}}
    s = score(case, _response(*range(1, 11)), shelf, top=20, labels=labels)
    assert s.ndcg == pytest.approx(1 / 3)


def test_점수를_매기지_않은_책은_0점으로_계산하고_따로_센다() -> None:
    shelf = {1: _book(1), 2: _book(2), 3: _book(3)}
    case = _descriptive_case()
    labels = {case["id"]: {shelf[1].isbn: 2}}
    s = score(case, _response(2, 1, 3), shelf, top=20, labels=labels)
    assert s.ndcg == pytest.approx((3 / math.log2(3)) / IDEAL)
    assert (s.relevant, s.irrelevant, s.unlabeled) == (1, 0, 2)
    # 라벨 파일을 주지 않아도 돈다. 모든 책이 점수 없는 책이다
    assert score(case, _response(1), shelf, top=20).unlabeled == 1


def test_서술형은_종류와_소분류별로_모은다() -> None:
    shelf = {1: _book(1), 2: _book(2)}
    labels = {
        "descriptive-topic": {shelf[1].isbn: 2, shelf[2].isbn: 0},
        "descriptive-mood": {shelf[1].isbn: 0},
    }
    scores = [
        score(
            _descriptive_case("topic"), _response(1, 2), shelf, top=20, labels=labels
        ),
        score(_descriptive_case("mood"), _response(1, 2), shelf, top=20, labels=labels),
    ]
    summary = summarize(scores)
    row = summary["kinds"]["descriptive"]
    assert row["ndcg_mean"] == pytest.approx(3 / IDEAL / 2, abs=1e-3)
    assert (row["relevant_mean"], row["irrelevant_mean"], row["unlabeled"]) == (
        0.5,
        1.0,
        1,
    )
    assert summary["facets"]["topic"]["ndcg_mean"] == pytest.approx(3 / IDEAL, abs=1e-3)
    assert summary["facets"]["mood"]["ndcg_mean"] == 0
