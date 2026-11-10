"""③ 저자를 짚은 요청의 후보 좁히기(app/chat/authors.py, #655).

"한강 책 추천해줘"에 제목이 『한강』인 책이 아니라 작가 한강의 책이 나와야 한다. 앞쪽은 DB 없이 돌고,
맨 아래 DB 시험은 SEARCH_TEST_DATABASE_URL 이 있을 때만 돈다.
"""

import asyncio
import contextlib
import os

import asyncpg
import pytest

from app.chat import authors
from app.chat.schemas import Spec, SpecExact
from app.routers import chat
from app.search.schemas import SearchFilters, SearchRequest
from app.search.service import SearchOutcome


def _book(book_id: int, author: str | None = "작가") -> dict:
    return {
        "book_id": book_id,
        "title": f"책{book_id}",
        "author": author,
        "publisher": "출판사",
        "price": 10000,
        "in_stock": True,
        "cover_url": None,
    }


# --- 이름 견주기 ---


@pytest.mark.parametrize(
    "author",
    ["한강", " 한강 ", "김인숙, 공선옥, 한강", "한강;이수환", "김인숙 ,한강"],
)
def test_저자_칸의_이름_하나가_통째로_같으면_그_저자의_책이다(author: str) -> None:
    assert authors.wrote(_book(1, author), "한강")


@pytest.mark.parametrize(
    "author", ["한강현", "김한강", "한강생명포럼", "조정래", "", None]
)
def test_이름_일부만_같으면_그_저자의_책이_아니다(author: str | None) -> None:
    assert not authors.wrote(_book(1, author), "한강")


def test_로마자_이름은_대소문자를_가리지_않는다() -> None:
    assert authors.wrote(_book(1, "Haruki Murakami"), " haruki murakami ")


def test_ILIKE_의_특수_글자는_그대로_찾는다() -> None:
    assert authors._like_pattern("a%b_c") == "%a\\%b\\_c%"


# --- 검색 결과와 합치기 ---


def test_검색_결과의_그_저자_책을_앞에_두고_DB에서_가져온_책을_뒤에_붙인다() -> None:
    results = [_book(1, "조정래"), _book(2, "한강"), _book(3, "한강현")]
    by_author = [_book(2, "한강"), _book(4, "한강")]

    narrowed = authors.narrow(results, "한강", by_author)

    assert [b["book_id"] for b in narrowed] == [2, 4]


def test_그_저자의_책이_하나도_없으면_검색_결과를_그대로_쓴다() -> None:
    # "J.K. 롤링" ↔ "조앤 K. 롤링"처럼 표기가 달라 못 찾은 것일 수 있다. 카드 0장보다 지금까지의 결과가 낫다.
    results = [_book(1, "조앤 K. 롤링"), _book(2, "다른 작가")]

    assert authors.narrow(results, "J.K. 롤링", []) == results


# --- get_candidates 에 이어 붙인 모양 ---


@pytest.fixture(autouse=True)
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """점수·판본 조회는 DB 가 필요하다(tests/test_chat_candidates.py 와 같은 방식)."""

    class _NoPool:
        def acquire(self):
            return contextlib.nullcontext()

    async def _noop(conn, candidates, user_id, spec) -> None:
        pass

    async def _no_keys(book_ids):
        return set()

    async def _descriptions(book_ids):
        return {book_id: "소개" for book_id in book_ids}

    monkeypatch.setattr(chat.db, "get_pool", lambda: _NoPool())
    monkeypatch.setattr(chat, "_attach_match_scores", _noop)
    monkeypatch.setattr(chat, "_fetch_edition_keys", _no_keys)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)


def _spec(title: str | None, author: str | None, semantic: str | None = None) -> Spec:
    return Spec(
        intent="semantic" if semantic else "exact",
        exact=SpecExact(title=title, author=author, publisher=None),
        filters=SearchFilters(),
        semantic=semantic,
        anchor_book=None,
        exclude=[],
    )


def _use(
    monkeypatch: pytest.MonkeyPatch, searched: list[dict], by_author: list[dict]
) -> dict:
    calls = {"search": 0, "by_author": 0}

    async def _search(req: SearchRequest) -> SearchOutcome:
        calls["search"] += 1
        return SearchOutcome(results=searched, degraded=None)

    async def _by_author(author: str, search_filters: SearchFilters) -> list[dict]:
        calls["by_author"] += 1
        return by_author

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_books_by_author", _by_author)
    return calls


# 11/10 평가 DB 에서 "한강"을 검색하면 제목이 『한강』인 조정래의 책이 앞을 채웠다
RIVER_BOOKS = [_book(1, "조정래"), _book(2, "조정래"), _book(3, "한강")]


def test_저자만_말하면_검색하지_않고_그_저자의_책을_후보로_쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _use(monkeypatch, RIVER_BOOKS, [_book(10, "한강"), _book(11, "한강")])

    result = asyncio.run(chat.get_candidates(_spec(None, "한강"), [], 1))

    assert [c["book_id"] for c in result] == [10, 11]
    assert calls == {"search": 0, "by_author": 1}


def test_그_저자의_책을_못_찾으면_지금처럼_검색_결과를_쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _use(monkeypatch, RIVER_BOOKS, [])

    result = asyncio.run(chat.get_candidates(_spec(None, "없는 작가"), [], 1))

    assert [c["book_id"] for c in result] == [1, 2, 3]
    assert calls == {"search": 1, "by_author": 1}


def test_제목과_저자를_말하면_검색_결과에서_그_저자의_책만_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "소년이 온다 한강" → 『한강, 소년이 온다 깊게 읽기』 같은 남의 해설서가 빠진다
    searched = [_book(1, "한강"), _book(2, "해설가"), _book(3, "한강")]
    calls = _use(monkeypatch, searched, [_book(99, "한강")])

    result = asyncio.run(chat.get_candidates(_spec("소년이 온다", "한강"), [], 1))

    assert [c["book_id"] for c in result] == [1, 3]
    assert calls == {"search": 1, "by_author": 0}


def test_분위기도_말하면_그_분위기로_찾은_그_저자의_책을_앞에_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    searched = [_book(1, "다른 작가"), _book(2, "히가시노 게이고")]
    by_author = [_book(5, "히가시노 게이고"), _book(2, "히가시노 게이고")]
    calls = _use(monkeypatch, searched, by_author)

    spec = _spec(None, "히가시노 게이고", semantic="반전 있는 추리소설")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [2, 5]
    assert calls == {"search": 1, "by_author": 1}


# --- 실제 DB ---

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)
_NAME = "저자시험갑"


def _run(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


async def _insert(
    conn: asyncpg.Connection,
    book_id: int,
    author: str,
    pub_year: int = 2020,
    description: str | None = "소개",
) -> None:
    await conn.execute(
        "INSERT INTO v_books (book_id, title, author, publisher, price, in_stock,"
        " cover_url, category, pub_year, description)"
        " VALUES ($1, $2, $3, '출판사', 10000, true, NULL, NULL, $4, $5)",
        book_id,
        f"저자시험책{book_id}",
        author,
        pub_year,
        description,
    )


@_needs_db
def test_DB에서_이름이_통째로_같은_책만_첫_저자_소개글_최근_판_순으로_가져온다() -> (
    None
):
    async def check(conn):
        await _insert(conn, 9_655_001, _NAME, pub_year=2010)
        await _insert(conn, 9_655_002, _NAME, pub_year=2024)
        await _insert(conn, 9_655_003, _NAME, pub_year=2030, description=None)
        await _insert(conn, 9_655_004, f"김인숙, {_NAME}", pub_year=2015)
        await _insert(conn, 9_655_005, f"{_NAME}현", pub_year=2024)
        await _insert(conn, 9_655_006, f"김{_NAME}", pub_year=2024)
        return await authors.fetch(conn, _NAME, SearchFilters())

    books = _run(check)

    # 함께 쓴 문집(004)은 소개글이 있고 최근이어도 그 저자가 첫 저자인 책 뒤에 온다
    assert [b["book_id"] for b in books] == [9_655_002, 9_655_001, 9_655_003, 9_655_004]
    assert {"isbn", "title", "author", "publisher", "price"} <= set(books[0])


@_needs_db
def test_DB에서_사용자_조건도_건다() -> None:
    async def check(conn):
        await _insert(conn, 9_655_011, _NAME, pub_year=2010)
        await _insert(conn, 9_655_012, _NAME, pub_year=2024)
        return await authors.fetch(conn, _NAME, SearchFilters(pub_year_from=2020))

    assert [b["book_id"] for b in _run(check)] == [9_655_012]
