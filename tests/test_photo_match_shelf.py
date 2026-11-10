"""책등 글자로 책 찾기(app/photo/match_shelf.py) 테스트(#615).

카탈로그에 없는 책을 가장 가까운 다른 책에 붙이지 않는지를 주로 본다. 사례는 11/04 책장 실험에서 나온 것이다.
"""

import asyncio

import pytest

from app.photo import catalog, match_shelf
from app.photo.read import BookText

_next_id = iter(range(1, 10_000))


def _book(
    title: str,
    author: str | None = None,
    *,
    pub_year: int | None = 2020,
    book_id: int | None = None,
    isbn: str | None = None,
) -> catalog.Book:
    return catalog.Book(
        book_id=book_id if book_id is not None else next(_next_id),
        isbn=isbn,
        title=title,
        author=author,
        publisher=None,
        pub_year=pub_year,
        price=15000,
        in_stock=True,
        cover_url=None,
    )


def _spine(title: str | None, **fields) -> BookText:
    return BookText(is_book_cover=True, title=title, **fields)


def test_제목이_그대로_같으면_찾는다() -> None:
    book = _book("루비 프로그래밍 언어", "Peter Cooper 외")

    assert match_shelf.decide(_spine("루비 프로그래밍 언어"), [book]) == book


def test_띄어쓰기와_대소문자는_보지_않는다() -> None:
    book = _book("COVERT java")

    assert match_shelf.decide(_spine("Covert Java"), [book]) == book


def test_제목이_같은_책이_여럿이면_새_판을_고른다() -> None:
    old = _book("C프로그래밍", "강희훈", pub_year=2015)
    new = _book("C프로그래밍", "황수철", pub_year=2025)

    assert match_shelf.decide(_spine("C프로그래밍"), [old, new]) == new


def test_제목이_90퍼센트에_못_미치면_찾지_않는다() -> None:
    # 카탈로그 제목에 긴 부제가 붙어 있으면 책등의 짧은 제목과 많이 다르다
    book = _book("Julia 프로그래밍(데이터 처리를 위한 쉬운 고성능 언어)")

    assert match_shelf.decide(_spine("Julia 프로그래밍"), [book]) is None


def test_카탈로그_제목의_부제는_떼고_본다() -> None:
    book = _book("코틀린 인 액션: 코틀린 언어 개발자가 직접 집필한 코틀린 가이드")

    assert match_shelf.decide(_spine("코틀린 인 액션"), [book]) == book


def test_카탈로그_제목_앞의_괄호_말은_떼고_본다() -> None:
    book = _book("(언어 자료 처리를 위한)Perl")

    assert match_shelf.decide(_spine("Perl"), [book]) == book


def test_책등에서_읽은_부제를_붙여도_본다() -> None:
    book = _book("모던 자바스크립트 Deep Dive")

    assert (
        match_shelf.decide(_spine("모던 자바스크립트", subtitle="Deep Dive"), [book])
        == book
    )


@pytest.mark.parametrize(("vol", "found"), [("7", True), ("6", False)])
def test_권_번호_숫자가_어긋나면_찾지_않는다(vol: str, found: bool) -> None:
    book = _book("자바스크립트 완벽 가이드 7")

    result = match_shelf.decide(_spine("자바스크립트 완벽 가이드", volume=vol), [book])

    assert (result == book) is found


def test_판_표시의_숫자는_권_번호로_보지_않는다() -> None:
    book = _book("자바스크립트 완벽 가이드 7")

    result = match_shelf.decide(
        _spine("자바스크립트 완벽 가이드", volume="2nd Edition"), [book]
    )

    assert result == book


def test_저자는_보지_않는다() -> None:
    # 책등의 작은 저자 글씨는 모델이 지어낼 때가 많다(김경환→김기현)
    book = _book("혼자 공부하는 머신러닝+딥러닝", "박해선")

    result = match_shelf.decide(
        _spine("혼자 공부하는 머신러닝+딥러닝", authors=["김기현"]), [book]
    )

    assert result == book


@pytest.mark.parametrize("title", [None, ""])
def test_제목을_못_읽었으면_찾지_않는다(title: str | None) -> None:
    assert match_shelf.decide(_spine(title), [_book("토지")]) is None


def test_후보가_없으면_찾지_않는다() -> None:
    assert match_shelf.decide(_spine("토지"), []) is None


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    """검색어마다 정해 둔 줄을 돌려주는 가짜 연결. 받은 SQL 과 검색어를 남긴다."""

    def __init__(
        self,
        by_query: dict[str, list[dict]],
        by_isbn: dict[str, dict] | None = None,
        *,
        by_key_query: dict[str, list[dict]] | None = None,
    ) -> None:
        # by_query 는 띄어쓰기만 뺀 제목 검색, by_key_query 는 다듬은 제목 검색이 돌려줄 줄
        self.by_query = by_query
        self.by_key_query = by_key_query or {}
        self.by_isbn = by_isbn or {}
        self.queries: list[str] = []
        self.sqls: list[str] = []
        self.executed: list[str] = []
        self.fetched_isbns: list[str] = []

    def transaction(self):
        return _Transaction()

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)

    async def fetch(self, sql: str, query: str, limit: int):
        if query not in self.queries:
            self.queries.append(query)
        self.sqls.append(sql)
        key_sql = match_shelf._SEARCH_SQLS[1]
        rows = self.by_key_query if sql == key_sql else self.by_query
        return rows.get(query, [])

    async def fetchrow(self, sql: str, isbn: str):
        self.fetched_isbns.append(isbn)
        return self.by_isbn.get(isbn)


def _row(book: catalog.Book) -> dict:
    return dict(book.__dict__)


def test_맞는_ISBN이_보이면_검색하지_않고_그_책이다() -> None:
    book = _book("책", isbn="9780306406157", book_id=7)
    conn = _Conn({}, {"9780306406157": _row(book)})

    found, via = asyncio.run(
        match_shelf.match(conn, _spine("다른 제목", isbn="9780306406157"))
    )

    assert (found, via) == (book, "isbn")
    assert conn.queries == []


def test_제목과_제목_부제로_찾고_같은_검색어는_한_번만_부른다() -> None:
    book = _book("모던 자바스크립트 Deep Dive", book_id=3)
    conn = _Conn({"모던자바스크립트deepdive": [_row(book)]})

    found, via = asyncio.run(
        match_shelf.match(conn, _spine("모던 자바스크립트", subtitle="Deep Dive"))
    )

    assert (found, via) == (book, "text")
    assert conn.queries == ["모던자바스크립트", "모던자바스크립트deepdive"]
    # 비슷함의 하한은 그 검색에서만 바꾼다
    assert all("SET LOCAL pg_trgm.similarity_threshold" in s for s in conn.executed)


def test_한_글자_제목은_검색하지_않는다() -> None:
    conn = _Conn({})

    assert asyncio.run(match_shelf.match(conn, _spine("책"))) == (None, None)
    assert conn.queries == []


def test_띄어쓰기만_뺀_제목으로_찾으면_다듬은_제목으로는_찾지_않는다() -> None:
    book = _book("(박미정의) 깃&깃허브 입문")
    conn = _Conn({"박미정의깃깃허브입문": [_row(book)]})

    found, _ = asyncio.run(match_shelf.match(conn, _spine("박미정의 깃 & 깃허브 입문")))

    assert found == book
    nospace_sql, _ = match_shelf._SEARCH_SQLS
    assert conn.sqls == [nospace_sql]


def test_확정하지_못하면_다듬은_제목으로_다시_찾는다() -> None:
    # 앞 괄호 말이 길면 띄어쓰기만 뺀 제목과는 비슷함이 0.31 이라 후보에 못 든다(#615)
    book = _book(
        "(오라클, 딜로이트 출신 전문가가 알려 주는) 챗GPT시대 기업이 살아남는 법"
    )
    near = _book("챗GPT 시대의 기업")
    conn = _Conn(
        {"챗gpt시대기업이살아남는법": [_row(near)]},
        by_key_query={"챗gpt시대기업이살아남는법": [_row(book)]},
    )

    found, via = asyncio.run(
        match_shelf.match(conn, _spine("챗GPT시대 기업이 살아남는 법"))
    )

    assert (found, via) == (book, "text")
    assert conn.sqls == list(match_shelf._SEARCH_SQLS)


def test_다시_찾아도_확정하지_못하면_못_찾음이다() -> None:
    conn = _Conn({}, by_key_query={"covertjava": [_row(_book("자바의 정석"))]})

    assert asyncio.run(match_shelf.match(conn, _spine("COVERT JAVA"))) == (None, None)


def test_검색이_색인과_같은_식을_쓴다() -> None:
    nospace_sql, key_sql = match_shelf._SEARCH_SQLS
    # 다듬은 제목 색인(20261109 마이그레이션)과 띄어쓰기를 뺀 제목 색인(20260922)은 식이 같아야 탄다
    assert f"{match_shelf._KEY_TITLE} % $1" in key_sql
    assert "translate(b.title, E' \\t\\u3000\\u00a0', '') % $1" in nospace_sql
