"""표지 글자로 책 찾기(app/photo/match_cover.py) 테스트(#572, #615).

틀린 책을 확정하지 않는 것이 가장 중요해서, 애매하면 후보로 넘기는지를 주로 본다.
"""

import asyncio

import pytest

from app.photo import catalog
from app.photo import match_cover as match
from app.photo.read import BookText

_next_id = iter(range(1, 10_000))


def _book(
    title: str,
    author: str | None = None,
    publisher: str | None = None,
    *,
    pub_year: int | None = 2020,
    in_stock: bool = True,
    cover_url: str | None = "https://img/1.jpg",
    book_id: int | None = None,
) -> catalog.Book:
    return catalog.Book(
        book_id=book_id if book_id is not None else next(_next_id),
        isbn=None,
        title=title,
        author=author,
        publisher=publisher,
        pub_year=pub_year,
        price=15000,
        in_stock=in_stock,
        cover_url=cover_url,
    )


def _cover(title: str | None, *authors: str, **fields) -> BookText:
    return BookText(is_book_cover=True, title=title, authors=list(authors), **fields)


def test_제목과_저자가_같은_책이_하나면_찾는다() -> None:
    book = _book("불편한 편의점", "김호연 지음", "나무옆의자")

    result = match.decide(_cover("불편한 편의점", "김호연"), [book])

    assert result.kind == "found"
    assert result.books[0].book == book
    assert result.via == "text"


def test_같은_책의_판본이_여럿이면_표지와_더_맞고_살_수_있는_새_판을_고른다() -> None:
    old = _book("불편한 편의점", "김호연", "나무옆의자", pub_year=2021)
    sold_out = _book(
        "불편한 편의점", "김호연", "나무옆의자", pub_year=2024, in_stock=False
    )
    newest = _book("불편한 편의점", "김호연", "나무옆의자", pub_year=2023)
    other_publisher = _book("불편한 편의점", "김호연", "다른출판사", pub_year=2025)

    result = match.decide(
        _cover("불편한 편의점", "김호연", publisher="나무옆의자"),
        [old, sold_out, newest, other_publisher],
    )

    assert result.kind == "found"
    assert result.books[0].book == newest


def test_제목이_같은_다른_저자의_책은_저자로_가른다() -> None:
    soseki = _book("마음", "나쓰메 소세키")
    other = _book("마음", "김철수")

    result = match.decide(_cover("마음", "나쓰메 소세키"), [other, soseki])

    assert result.kind == "found"
    assert result.books[0].book == soseki


def test_저자를_읽었는데_맞는_책이_없으면_확정하지_않는다() -> None:
    books = [_book("마음", "김철수"), _book("마음", "이영희")]

    result = match.decide(_cover("마음", "나쓰메 소세키"), books)

    assert result.kind == "candidates"


def test_저자를_못_읽었고_제목이_같은_책이_여럿이면_후보로_넘긴다() -> None:
    books = [_book("마음", "김철수", "가출판"), _book("마음", "이영희", "나출판")]

    assert match.decide(_cover("마음"), books).kind == "candidates"


def test_저자를_못_읽어도_출판사로_하나가_남으면_찾는다() -> None:
    target = _book("마음", "이영희", "현암사")
    books = [_book("마음", "김철수", "가출판"), target]

    result = match.decide(_cover("마음", publisher="(주)현암사"), books)

    assert result.kind == "found"
    assert result.books[0].book == target


def test_저자를_못_읽었고_제목이_같은_책이_하나뿐이면_찾는다() -> None:
    book = _book("달러구트 꿈 백화점", "이미예")

    assert match.decide(_cover("달러구트 꿈 백화점"), [book]).kind == "found"


def test_권_번호가_있으면_그_권을_찾는다() -> None:
    first = _book("달러구트 꿈 백화점", "이미예")
    second = _book("달러구트 꿈 백화점 2", "이미예")

    result = match.decide(
        _cover("달러구트 꿈 백화점", "이미예", volume="2"), [first, second]
    )

    assert result.kind == "found"
    assert result.books[0].book == second


def test_권_번호가_없으면_권_번호_없는_제목을_찾는다() -> None:
    first = _book("달러구트 꿈 백화점", "이미예")
    second = _book("달러구트 꿈 백화점 2", "이미예")

    result = match.decide(_cover("달러구트 꿈 백화점", "이미예"), [second, first])

    assert result.kind == "found"
    assert result.books[0].book == first


def test_카탈로그_제목에_붙은_부제는_떼고_본다() -> None:
    book = _book("달러구트 꿈 백화점 : 주문하신 꿈은 매진입니다", "이미예")

    assert match.decide(_cover("달러구트 꿈 백화점", "이미예"), [book]).kind == "found"


def test_표지의_부제까지_붙인_제목도_본다() -> None:
    book = _book("코스모스 특별판", "칼 세이건")

    result = match.decide(_cover("코스모스", "칼 세이건", subtitle="특별판"), [book])

    assert result.kind == "found"


def test_저자_이름을_줄여_써도_같은_사람으로_본다() -> None:
    book = _book("어린 왕자", "앙투안 드 생텍쥐페리 지음 ;황현산 옮김")

    assert match.decide(_cover("어린 왕자", "생텍쥐페리"), [book]).kind == "found"


def test_두_글자_이름은_끝만_같으면_다른_사람이다() -> None:
    book = _book("소년이 온다", "김한강")

    assert match.decide(_cover("소년이 온다", "한강"), [book]).kind == "candidates"


def test_저자_표기만_달라_갈라진_판본은_출판사로_고른다() -> None:
    short = _book("어린 왕자", "생텍쥐페리", "가출판")
    full = _book("어린 왕자", "앙투안 드 생텍쥐페리", "열린책들")

    result = match.decide(
        _cover("어린 왕자", "생텍쥐페리", publisher="열린책들"), [short, full]
    )

    assert result.kind == "found"
    assert result.books[0].book == full


def test_후보는_서로_다른_책_세_권까지_표지와_많이_맞는_순이다() -> None:
    ga = _book("마음", "가", "가출판")
    na = _book("마음", "나", "현암사")
    da = _book("마음", "다", "현암사")
    ra = _book("마음", "라", "라출판")

    result = match.decide(_cover("마음", publisher="현암사"), [ga, na, da, ra])

    assert result.kind == "candidates"
    # 출판사까지 맞는 두 권이 먼저, 나머지는 검색 순위대로
    assert [s.book for s in result.books] == [na, da, ga]


def test_저자_표기만_다른_판본들이_출판사까지_같으면_한_책으로_본다() -> None:
    short = _book("어린 왕자", "생텍쥐페리, 이정서", "새움", pub_year=2017)
    full = _book(
        "어린 왕자", "앙투안 드 생택쥐페리, 이정서", "새움(새움출판사)", pub_year=2025
    )
    other = _book(
        "어린 왕자", "앙투안 드 생텍쥐페리, 황현산", "열린책들", pub_year=2026
    )

    result = match.decide(
        _cover("어린 왕자", "생텍쥐페리", "이정서", publisher="새움"),
        [short, full, other],
    )

    assert result.kind == "found"
    assert result.books[0].book == full


def test_로마자_제목만_읽었으면_원서와_번역서를_못_가려_확정하지_않는다() -> None:
    original = _book("Demian", "Hermann Hesse", "(도)산호와진주")

    result = match.decide(_cover("DEMIAN", "HERMANN HESSE"), [original])

    assert result.kind == "candidates"


def test_한글이_섞인_제목은_로마자가_있어도_확정한다() -> None:
    book = _book("Do it! 점프 투 파이썬", "박응용")

    assert (
        match.decide(_cover("Do it! 점프 투 파이썬", "박응용"), [book]).kind == "found"
    )


def test_숫자만인_제목은_로마자_제목으로_보지_않는다() -> None:
    book = _book("1984", "조지 오웰")

    assert match.decide(_cover("1984", "조지 오웰"), [book]).kind == "found"


def test_한_글자_틀리게_읽은_저자는_확정하지_않지만_후보_앞에_둔다() -> None:
    sagan = _book("코스모스", "칼 세이건, 홍승수", "사이언스북스")
    comics = _book("코스모스", "Ryuhei Tamura, 김수연")
    druyan = _book("코스모스", "앤 드루얀, 김명남", "사이언스북스")

    result = match.decide(_cover("코스모스", "앤 드루안"), [sagan, comics, druyan])

    assert result.kind == "candidates"
    assert result.books[0].book == druyan


@pytest.mark.parametrize(
    "text",
    [
        BookText(is_book_cover=False, title="불편한 편의점"),
        BookText(is_book_cover=True, title=None),
        BookText(is_book_cover=True, title="없는 제목"),
    ],
)
def test_표지가_아니거나_제목이_같은_책이_없으면_못_찾는다(text: BookText) -> None:
    assert match.decide(text, [_book("불편한 편의점", "김호연")]).kind == "none"


@pytest.mark.parametrize(
    ("isbn", "valid"),
    [
        ("9780306406157", True),
        ("9780306406158", False),  # 마지막 숫자 하나를 잘못 읽음
        ("978030640615", False),
        ("978030640615X", False),
    ],
)
def test_ISBN_검증_숫자(isbn: str, valid: bool) -> None:
    assert catalog.is_valid_isbn13(isbn) is valid


_COLUMNS = (
    "book_id",
    "isbn",
    "title",
    "author",
    "publisher",
    "pub_year",
    "price",
    "in_stock",
    "cover_url",
)


def _row(book_id: int, title: str, author: str, isbn: str | None = None) -> dict:
    return dict(
        zip(
            _COLUMNS,
            (book_id, isbn, title, author, None, 2020, 15000, True, None),
            strict=True,
        )
    )


class _Conn:
    def __init__(self, by_isbn: dict[str, dict], rows: list[dict]) -> None:
        self.by_isbn = by_isbn
        self.rows = rows
        self.fetched_isbns: list[str] = []

    async def fetchrow(self, sql: str, isbn: str):
        self.fetched_isbns.append(isbn)
        return self.by_isbn.get(isbn)

    async def fetch(self, sql: str, ids: list[int]):
        return [r for r in self.rows if r["book_id"] in ids]


def _run(
    text: BookText, conn: _Conn, monkeypatch, search_ids: list[int]
) -> match.Match:
    async def fake_search(conn, query, filters, limit):
        return search_ids

    monkeypatch.setattr(match.keyword, "search_ids", fake_search)
    return asyncio.run(match.match(conn, text))


def test_맞는_ISBN이_보이면_검색하지_않고_그_책이다(monkeypatch) -> None:
    conn = _Conn({"9780306406157": _row(7, "책", "저자", "9780306406157")}, [])

    result = _run(_cover("다른 제목", isbn="9780306406157"), conn, monkeypatch, [])

    assert result.kind == "found"
    assert result.via == "isbn"
    assert result.books[0].book.book_id == 7
    assert result.books[0].score == 100


def test_검증_숫자가_틀린_ISBN은_책_표를_찾지_않고_제목으로_찾는다(monkeypatch) -> None:
    conn = _Conn({}, [_row(3, "불편한 편의점", "김호연")])

    result = _run(
        _cover("불편한 편의점", "김호연", isbn="9780306406158"), conn, monkeypatch, [3]
    )

    assert conn.fetched_isbns == []
    assert result.kind == "found"
    assert result.via == "text"
    assert result.books[0].book.book_id == 3


def test_검색_순위를_지켜_후보를_넘긴다(monkeypatch) -> None:
    conn = _Conn({}, [_row(1, "마음", "가"), _row(2, "마음", "나")])

    assert [
        s.book.book_id for s in _run(_cover("마음"), conn, monkeypatch, [1, 2]).books
    ] == [1, 2]
    assert [
        s.book.book_id for s in _run(_cover("마음"), conn, monkeypatch, [2, 1]).books
    ] == [2, 1]


def test_판_표시는_권_번호로_보지_않는다() -> None:
    book = _book("이펙티브 자바", "조슈아 블로크, 이복연", "인사이트")

    result = match.decide(
        _cover("이펙티브 자바", "조슈아 블로크", volume="3/E"), [book]
    )

    assert result.kind == "found"
    assert result.books[0].book == book


def test_카탈로그에_없는_2권_사진을_1권으로_확정하지_않는다() -> None:
    first = _book("쇼펜하우어 인생수업", "쇼펜하우어, 김지민", "하이스트")

    result = match.decide(
        _cover("쇼펜하우어 인생수업", "쇼펜하우어", volume="II"), [first]
    )

    # 제목이 거의 같아 후보로는 보일 수 있지만 확정은 하지 않는다
    assert result.kind != "found"


@pytest.mark.parametrize(
    "catalog_title", ["삼국지 2", "삼국지 Ⅱ", "삼국지 II", "삼국지 2권"]
)
def test_로마_숫자와_아라비아_숫자_권_번호를_같게_본다(catalog_title: str) -> None:
    book = _book(catalog_title, "나관중")

    assert match.decide(_cover("삼국지", "나관중", volume="II"), [book]).kind == "found"


def test_연도판은_그_해의_책을_찾는다() -> None:
    old = _book("머니 트렌드 2026", "김도윤, 정태익")
    new = _book("머니 트렌드 2027", "정태익, 김도윤")

    result = match.decide(_cover("머니 트렌드", "김도윤", volume="2027"), [old, new])

    assert result.kind == "found"
    assert result.books[0].book == new


def test_제목을_한_글자_틀리게_읽으면_확정하지_않고_후보로_보여_준다() -> None:
    book = _book("자바 코드의 품질을 높이는 100가지 방법", "타기르 발레예프, 정병열")
    other = _book("자바 코드 리뷰", "정병열")

    result = match.decide(
        _cover("자바 코드를 품질을 높이는 100가지 방법", "정병열"), [other, book]
    )

    assert result.kind == "candidates"
    assert [s.book for s in result.books] == [book]


def test_짧은_제목은_한_글자_차이를_비슷하다고_보지_않는다() -> None:
    assert (
        match.decide(_cover("마을", "김철수"), [_book("마음", "김철수")]).kind == "none"
    )


@pytest.mark.parametrize("volume", ["PART 1 03", "Part 4-01", "Vol. 2"])
def test_낯선_모양의_권_번호도_빼지_않는다(volume: str) -> None:
    # 『전지적 독자 시점』은 권마다 카탈로그 제목이 같다. 권 번호를 버리면 다른 권을 확정한다(#615 표지 시험)
    book = _book("전지적 독자 시점", "싱숑", "비채")

    result = match.decide(_cover("전지적 독자 시점", "싱숑", volume=volume), [book])

    assert result.kind != "found"


def test_낯선_모양의_권_번호가_카탈로그_제목과_같으면_찾는다() -> None:
    book = _book("전지적 독자 시점 Part 1 03", "싱숑", "비채")

    result = match.decide(
        _cover("전지적 독자 시점", "싱숑", volume="PART 1 03"), [book]
    )

    assert result.kind == "found"


@pytest.mark.parametrize("edition", ["개정판", "3/E", "2nd Edition", "제3판"])
def test_판_표시는_숫자가_있어도_권_번호가_아니다(edition: str) -> None:
    book = _book("이펙티브 자바", "조슈아 블로크", "인사이트")

    result = match.decide(
        _cover("이펙티브 자바", "조슈아 블로크", volume=edition), [book]
    )

    assert result.kind == "found"
