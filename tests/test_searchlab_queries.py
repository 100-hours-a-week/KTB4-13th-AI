"""검색 시간 측정에 쓸 검색어 고정(evals/searchlab/queries.py) 테스트(#591). DB 없이 돈다."""

import asyncio
import json
from collections import Counter

import pytest

from evals.searchlab import queries


def _cases() -> list[dict]:
    return json.loads(queries.CASES_PATH.read_text(encoding="utf-8"))["cases"]


def test_잴_검색어는_종류별_수가_측정_계획과_같다() -> None:
    picked = queries.pick_measure(_cases())
    assert len(picked) == 170
    assert Counter(x["kind"] for x in picked) == {
        "title": 60,
        "title-variant": 20,
        "author": 15,
        "filter": 15,
        "descriptive": 40,
        "sort": 20,
    }
    variants = Counter(x["change"] for x in picked if x["kind"] == "title-variant")
    assert variants == queries.VARIANTS
    sorts = Counter(x["sort"] for x in picked if x["kind"] == "sort")
    assert sorts == {"popular": 7, "newest": 7, "price_asc": 3, "price_desc": 3}
    assert all(x["sort"] == "relevance" for x in picked if x["kind"] != "sort")


def test_같은_시드면_같은_검색어가_나오고_시드를_바꾸면_달라진다() -> None:
    cases = _cases()
    first = queries.pick_measure(cases)
    assert queries.pick_measure(list(reversed(cases))) == first
    assert queries.pick_measure(cases, seed="other") != first


def test_정렬_사례의_제목_저자는_따로_재는_사례와_겹치지_않는다() -> None:
    picked = queries.pick_measure(_cases())
    measured = {x["id"] for x in picked if x["kind"] != "sort"}
    sources = [x["of"] for x in picked if x["kind"] == "sort"]
    by_id = {c["id"]: c for c in _cases()}
    assert all(s in by_id for s in sources)
    reused = {s for s in sources if s in measured}
    # 서술형만 같은 검색어를 정렬만 바꿔 다시 쓴다
    assert reused and all(by_id[s]["kind"] == "descriptive" for s in reused)
    assert len({x["id"] for x in picked}) == len(picked)


def test_필터는_평가_묶음의_조건을_그대로_싣는다() -> None:
    by_id = {c["id"]: c for c in _cases()}
    for item in queries.pick_measure(_cases()):
        if item["kind"] == "filter":
            assert item["filters"] == by_id[item["id"]]["filters"]


def test_모자라면_적게_재지_않고_멈춘다() -> None:
    cases = [c for c in _cases() if c["kind"] != "author"]
    with pytest.raises(ValueError, match="author"):
        queries.pick_measure(cases)


def test_저장소의_검색어_파일은_지금_규칙으로_고른_것과_같다() -> None:
    bundle = json.loads(queries.QUERIES_PATH.read_text(encoding="utf-8"))
    assert bundle["seed"] == queries.SEED
    assert bundle["measure"] == queries.pick_measure(_cases())
    assert Counter(x["kind"] for x in bundle["warmup"]) == {
        "title": queries.WARMUP_TITLES,
        "author": queries.WARMUP_AUTHORS,
    }
    warm = {x["query"] for x in bundle["warmup"]}
    assert not warm & {x["query"] for x in bundle["measure"]}
    assert not warm & {c["query"] for c in _cases()}


def test_데우기_제목은_평가_묶음의_책과_제목을_빼고_한_번씩만() -> None:
    pool = [
        {"isbn13": "1", "title": "평가 묶음 책"},
        {"isbn13": "2", "title": "새 책"},
        {"isbn13": "3", "title": "새  책"},  # 띄어쓰기만 다르면 같은 제목이다
        {"isbn13": "4", "title": "다른 책"},
        {"isbn13": "5", "title": "또 다른 책"},
    ]
    taken = queries.taken_keys(
        [{"id": "t", "kind": "title", "query": "평가 묶음 책", "isbn": "9"}]
    ) | {"5"}
    got = queries.pick_warmup_titles(pool, taken, n=2)
    assert [x["query"] for x in got] == ["새 책", "다른 책"]
    with pytest.raises(ValueError, match="데우기 제목"):
        queries.pick_warmup_titles(pool, taken, n=3)


def test_평가_묶음의_정답_제목도_데우기에서_뺀다() -> None:
    keys = queries.taken_keys(
        [
            {
                "id": "v",
                "kind": "title-variant",
                "query": "링월드 3",
                "expect": {"titles": ["링월드 3: 링월드의 왕좌"]},
            }
        ]
    )
    assert queries.editions.title_key("링월드 3: 링월드의 왕좌") in keys


class _Conn:
    """저자 이름으로 그 저자 책 목록을 돌려주는 가짜 연결."""

    def __init__(self, books: dict[str, int]) -> None:
        self.books = books

    async def fetch(self, _sql: str, name: str) -> list[dict]:
        return [{"author": name}] * self.books.get(name, 0)


def test_데우기_저자는_평가_묶음과_같은_규칙으로_고르고_평가_묶음의_저자는_뺀다() -> (
    None
):
    pool = [
        {"author": a}
        for a in ("김민수, 옮긴이", "편집부", "이영희", "박철수", "최지훈")
    ]
    conn = _Conn({"김민수": 12, "이영희": 3, "박철수": 15, "최지훈": 20})
    got = asyncio.run(queries.pick_warmup_authors(conn, pool, {"박철수"}, n=2))
    # 편집부는 사람 이름이 아니고, 이영희는 책이 10권보다 적고, 박철수는 평가 묶음의 저자다
    assert [x["query"] for x in got] == ["김민수", "최지훈"]


def test_파일은_한_줄에_검색어_하나이고_그대로_읽힌다() -> None:
    bundle = {
        "version": 1,
        "measure": [{"id": "a", "query": "가"}],
        "warmup": [{"id": "b", "query": "나"}],
    }
    text = queries.dump(bundle)
    assert json.loads(text) == bundle
    assert '  {"id": "a", "query": "가"}' in text.splitlines()
