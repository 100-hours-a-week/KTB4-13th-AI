"""사진 한 장에서 책 찾기(app/photo/scan.py) 테스트(#615). 받기·읽기·찾기는 가짜로 두고 순서와 묶기만 본다."""

import asyncio

import pytest

from app.gateway.llm import LLMUnavailableError
from app.photo import catalog, fetch, match_cover, scan
from app.photo.read import BookText, PhotoText


def _book(book_id: int, title: str) -> catalog.Book:
    return catalog.Book(
        book_id=book_id,
        isbn=None,
        title=title,
        author=None,
        publisher=None,
        pub_year=2020,
        price=15000,
        in_stock=True,
        cover_url=None,
    )


class _Pool:
    def __init__(self) -> None:
        self.acquired = 0

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                pool.acquired += 1
                return object()

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


@pytest.fixture
def pool(monkeypatch) -> _Pool:
    p = _Pool()
    monkeypatch.setattr(scan.db, "get_pool", lambda: p)
    return p


def _photo(monkeypatch, photo: PhotoText) -> None:
    async def fake_fetch(url, prefixes):
        return "data:image/jpeg;base64,AAAA"

    async def fake_read(image):
        return photo

    monkeypatch.setattr(scan.fetch, "fetch", fake_fetch)
    monkeypatch.setattr(scan.read, "read", fake_read)


def _shelf_matches(monkeypatch, by_title: dict[str, catalog.Book]) -> list[str]:
    asked: list[str] = []

    async def fake_match(conn, text):
        asked.append(text.title)
        book = by_title.get(text.title)
        return (book, "text") if book is not None else (None, None)

    monkeypatch.setattr(scan.match_shelf, "match", fake_match)
    return asked


def _run(image_ref: str = "https://bucket/x.jpg") -> scan.ScanResult:
    return asyncio.run(scan.scan(image_ref))


def test_책이_없는_사진은_빈_목록이고_책_표를_읽지_않는다(monkeypatch, pool) -> None:
    _photo(monkeypatch, PhotoText(photo_type="none", books=[]))

    result = _run()

    assert result.photo_type == "none"
    assert result.items == []
    assert pool.acquired == 0


def test_책장은_책장_규칙으로_왼쪽부터_찾는다(monkeypatch, pool) -> None:
    ruby = _book(1, "루비 프로그래밍 언어")
    _photo(
        monkeypatch,
        PhotoText(
            photo_type="shelf",
            books=[
                BookText(title="루비 프로그래밍 언어"),
                BookText(title="COVERT JAVA"),
            ],
        ),
    )
    asked = _shelf_matches(monkeypatch, {"루비 프로그래밍 언어": ruby})

    result = _run()

    assert asked == ["루비 프로그래밍 언어", "COVERT JAVA"]
    assert [(i.state, i.read_text) for i in result.items] == [
        ("FOUND", "루비 프로그래밍 언어"),
        ("NOT_FOUND", "COVERT JAVA"),
    ]
    assert result.items[0].book == ruby
    # 한 사진은 연결 하나로 찾는다
    assert pool.acquired == 1


def test_표지는_표지_규칙으로_찾고_후보는_못_찾음이다(monkeypatch, pool) -> None:
    found_book = _book(5, "불편한 편의점")
    _photo(
        monkeypatch,
        PhotoText(
            photo_type="cover",
            books=[BookText(is_book_cover=True, title="불편한 편의점")],
        ),
    )

    async def found(conn, text):
        return match_cover.Match(
            "found", [match_cover.Scored(found_book, True, True, False)], "text"
        )

    monkeypatch.setattr(scan.match_cover, "match", found)
    assert [(i.state, i.book) for i in _run().items] == [("FOUND", found_book)]

    async def candidates(conn, text):
        return match_cover.Match(
            "candidates", [match_cover.Scored(found_book, True, False, False)], "text"
        )

    monkeypatch.setattr(scan.match_cover, "match", candidates)
    assert [(i.state, i.book) for i in _run().items] == [("NOT_FOUND", None)]


def test_같은_책_두_권은_하나로_묶는다(monkeypatch, pool) -> None:
    same = _book(9, "모두의 스크래치 알고리즘")
    _photo(
        monkeypatch,
        PhotoText(
            photo_type="shelf",
            books=[
                BookText(title="모두의 스크래치 알고리즘"),
                BookText(title="Julia 프로그래밍"),
                BookText(title="모두의 스크래치 알고리즘"),
                BookText(title="Julia  프로그래밍"),
            ],
        ),
    )
    _shelf_matches(monkeypatch, {"모두의 스크래치 알고리즘": same})

    result = _run()

    assert [(i.state, i.read_text) for i in result.items] == [
        ("FOUND", "모두의 스크래치 알고리즘"),
        ("NOT_FOUND", "Julia 프로그래밍"),
    ]


def test_읽은_글자도_없고_못_찾은_책은_뺀다(monkeypatch, pool) -> None:
    _photo(
        monkeypatch,
        PhotoText(
            photo_type="shelf", books=[BookText(title=None), BookText(title="토지")]
        ),
    )
    _shelf_matches(monkeypatch, {})

    assert [i.read_text for i in _run().items] == ["토지"]


def test_읽은_글자에_권_번호를_붙인다() -> None:
    assert scan.read_text(BookText(title="전지적 독자 시점", volume="PART 1 03")) == (
        "전지적 독자 시점 PART 1 03"
    )


def test_너무_많이_읽어도_정한_권_수까지만_찾는다(monkeypatch, pool) -> None:
    monkeypatch.setattr(scan, "MAX_BOOKS", 3)
    _photo(
        monkeypatch,
        PhotoText(
            photo_type="shelf", books=[BookText(title=f"책{i}") for i in range(10)]
        ),
    )
    asked = _shelf_matches(monkeypatch, {})

    _run()

    assert asked == ["책0", "책1", "책2"]


def test_제한_시간을_넘기면_시간_초과다(monkeypatch, pool) -> None:
    monkeypatch.setattr(scan.get_settings(), "photo_timeout_seconds", 0.05)

    async def slow_fetch(url, prefixes):
        await asyncio.sleep(1)

    monkeypatch.setattr(scan.fetch, "fetch", slow_fetch)

    with pytest.raises(scan.ScanTimeoutError):
        _run()


def test_사진을_쓸_수_없으면_그대로_알린다(monkeypatch, pool) -> None:
    async def blocked(url, prefixes):
        raise fetch.ImageError("blocked")

    monkeypatch.setattr(scan.fetch, "fetch", blocked)

    with pytest.raises(fetch.ImageError) as e:
        _run()
    assert e.value.reason == "blocked"


def test_모델이_답하지_못하면_그대로_알린다(monkeypatch, pool) -> None:
    async def fake_fetch(url, prefixes):
        return "data:image/jpeg;base64,AAAA"

    async def broken(image):
        raise LLMUnavailableError("모델 장애")

    monkeypatch.setattr(scan.fetch, "fetch", fake_fetch)
    monkeypatch.setattr(scan.read, "read", broken)

    with pytest.raises(LLMUnavailableError):
        _run()


def test_허용_주소는_쉼표로_나누고_빈_칸은_버린다(monkeypatch) -> None:
    monkeypatch.setattr(
        scan.get_settings(),
        "photo_image_url_prefixes",
        " https://a.s3.amazonaws.com/p/ , ,https://b/q/ ",
    )

    assert scan.url_prefixes() == ["https://a.s3.amazonaws.com/p/", "https://b/q/"]


def test_허용_주소가_비면_아무_주소도_열지_않는다(monkeypatch) -> None:
    monkeypatch.setattr(scan.get_settings(), "photo_image_url_prefixes", "")

    assert scan.url_prefixes() == []
    assert not fetch.is_allowed("https://bucket/x.jpg", scan.url_prefixes())
