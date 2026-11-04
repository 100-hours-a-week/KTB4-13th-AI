"""① 검색 평가 묶음을 뽑는 스크립트(evals/search/build.py) 테스트(#495). DB 없이 돈다."""

import asyncio
import json
from collections import Counter

import pytest

from evals.search import build


def test_제목의_표기를_바꾼다() -> None:
    change = build.change_title
    assert change("희망의 속삭임", "nospace") == "희망의속삭임"
    assert change("링월드 3: 링월드의 왕좌", "no-subtitle") == "링월드 3"
    assert change("삼국지 2", "volume-roman") == "삼국지 Ⅱ"
    assert change("삼국지 15", "volume-circled") == "삼국지 ⑮"
    assert change("PSAT 300제", "case") == "psat 300제"
    assert change("Wonder 파이썬", "case") == "WONDER 파이썬"


def test_그_방법으로_바꿀_수_없는_제목은_쓰지_않는다() -> None:
    change = build.change_title
    assert change("희망의속삭임", "nospace") is None
    # 부제를 빼고 남은 제목이 짧으면 그 제목의 책이 너무 많다
    assert change("정보: 코드와 비트", "no-subtitle") is None
    # 로마 숫자는 Ⅻ 까지, 네 자리 숫자는 권수가 아니다
    assert change("삼국지 15", "volume-roman") is None
    assert change("엑셀 2021", "volume-circled") is None
    assert change("한글 제목", "case") is None
    with pytest.raises(ValueError):
        change("제목", "모르는 방법")


def test_저자_칸의_첫_이름이_사람_이름인지_가른다() -> None:
    assert build.first_author("한강, 데버라 스미스") == "한강"
    assert build.first_author("존 앵커버그·존 웰던, 김이사야") == "존 앵커버그"
    assert build.first_author(None) == ""
    assert build.person_like("한강") and build.person_like("무라카미 하루키")
    names = ("편집부", "SD 경찰공무원시험연구소", "아작 그림 홍우정", "작가불명", "")
    assert [name for name in names if build.person_like(name)] == []


def _pool(*titles: str) -> list[dict]:
    return [
        {"isbn13": f"97800000{i:05d}", "title": t, "author": "저자"}
        for i, t in enumerate(titles)
    ]


def test_제목이_같은_책은_한_권만_쓰고_표기_변형은_표본부터_고른다(monkeypatch) -> None:
    monkeypatch.setattr(build, "TITLES", 2)
    monkeypatch.setattr(build, "CHANGES", {"no-subtitle": 1, "nospace": 2})
    pool = _pool("붉은 달", "붉은달!", "푸른 숲", "검은 바다: 깊은 곳의 이야기")
    titles, variants = build.sample(pool)

    assert [c["query"] for c in titles] == ["붉은 달", "푸른 숲"]
    assert [(c["change"], c["query"], c["of"]) for c in variants] == [
        ("no-subtitle", "검은 바다", None),  # 표본에 없어 표본 뒤에서 골랐다
        ("nospace", "붉은달", "title-001"),
        ("nospace", "푸른숲", "title-002"),
    ]
    # 부제를 뺀 검색어는 부제 없는 제목의 책도 정답이다
    assert variants[0]["expect"]["titles"][1] == "검은 바다"
    # 모자라면 멈춘다
    monkeypatch.setattr(build, "CHANGES", {"volume-roman": 1})
    with pytest.raises(ValueError, match="volume-roman"):
        build.sample(pool)


class _Conn:
    def __init__(self, books: dict[str, list[str]]) -> None:
        self.books = books

    async def fetch(self, sql: str, name: str) -> list[dict]:
        return [{"author": a} for a in self.books.get(name, [])]


def test_저자는_사람_이름이고_이름이_통째로_든_책이_충분한_사람만_고른다(
    monkeypatch,
) -> None:
    monkeypatch.setattr(build, "AUTHORS", 1)
    monkeypatch.setattr(build, "AUTHOR_MIN_BOOKS", 2)
    pool = [{"author": a} for a in ("편집부", "한강준, 김민", "한강", "한강, 김민")]
    conn = _Conn({"한강준": ["한강준"], "한강": ["한강", "한강준", "한강, 김민"]})
    authors = asyncio.run(build.pick_authors(conn, pool))
    assert [(c["query"], c["expect"]["author"]) for c in authors] == [("한강", "한강")]


def test_필터는_낱말마다_조건_넷이고_없는_말은_쉰_키워드_0건은_서른_서술형은_마흔이다() -> (
    None
):
    cases = build.fixed_cases()
    assert Counter(c["kind"] for c in cases) == {
        "filter": 20,
        "nonsense": 50,
        "no-keyword": 30,
        "descriptive": 40,
    }
    filters = [c["filters"] for c in cases if c["kind"] == "filter"]
    assert filters[:4] == [
        {"category": "소설"},
        {"pub_year_from": 2020},
        {"price_max": 12000},
        {"category": "소설", "pub_year_from": 2020, "price_max": 12000},
    ]
    assert cases[-1] == {
        "id": "descriptive-40",
        "kind": "descriptive",
        "query": "아이와 함께 읽는 공룡 그림책",
        "facet": "mixed",
        "source": "written",
    }


def test_묶음_파일은_사례를_한_줄에_하나씩_쓴다() -> None:
    bundle = {"version": 1, "top": 20, "cases": [{"id": "a"}, {"id": "b"}]}
    text = build.dump(bundle)
    assert json.loads(text) == bundle
    assert '  {"id": "a"},\n  {"id": "b"}\n' in text
