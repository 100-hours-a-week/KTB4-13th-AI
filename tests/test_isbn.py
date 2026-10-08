"""BE 와 주고받는 ISBN 을 book_id 로 바꾸는 공용 코드(#311). 조회 한 가지만 실제 PostgreSQL 이 필요하다."""

import asyncio
import logging
import os

import asyncpg
import pytest
from pydantic import TypeAdapter, ValidationError

from app.core import isbn

# ---------------------------------------------------------------------------
# 형식 검사
# ---------------------------------------------------------------------------

_ISBN13 = TypeAdapter(isbn.Isbn13)


def test_13자리_숫자는_통과한다() -> None:
    assert _ISBN13.validate_python("9788936434120") == "9788936434120"


@pytest.mark.parametrize(
    "value",
    [
        "978-89-364-3412-0",  # 하이픈
        "978893643412",  # 12자리
        "97889364341200",  # 14자리
        "897364341X",  # 10자리 ISBN
        "978893643412X",  # 글자가 섞임
        " 9788936434120",  # 앞 공백
        "9788936434120\n",  # 끝 줄바꿈
        "９７８８９３６４３４１２０",  # 전각 숫자
        "",
    ],
)
def test_13자리_숫자가_아니면_거절한다(value: str) -> None:
    with pytest.raises(ValidationError):
        _ISBN13.validate_python(value)


def test_숫자로_보내면_거절한다() -> None:
    # ISBN 은 글자로 주고받는다(#307). 숫자를 받아 주면 BE 와 표기가 갈린다.
    with pytest.raises(ValidationError):
        _ISBN13.validate_python(9788936434120, strict=True)


# ---------------------------------------------------------------------------
# ISBN → book_id. DB 없이 도는 쪽
# ---------------------------------------------------------------------------


class _FakeConn:
    """v_books 에 있는 (isbn13 → book_id) 를 정해 둔다."""

    def __init__(self, books: dict[str, int]) -> None:
        self.books = books
        self.queries = 0

    async def fetch(self, sql: str, isbns: list[str]):
        self.queries += 1
        return [
            {"isbn13": i, "book_id": self.books[i]}
            for i in dict.fromkeys(isbns)
            if i in self.books
        ]


_A, _B, _C = "9788936434120", "9788954651135", "9788937460449"


def _to_book_ids(conn: _FakeConn, isbns: list[str]) -> list[int]:
    return asyncio.run(isbn.to_book_ids(conn, isbns))


def test_받은_순서대로_돌려준다() -> None:
    # DB 는 찾은 순서를 보장하지 않는다. 온보딩에서 고른 책은 앞에서부터 50권만 쓰므로 순서가 뜻을 가진다.
    conn = _FakeConn({_A: 1, _B: 2, _C: 3})

    assert _to_book_ids(conn, [_C, _A, _B]) == [3, 1, 2]


def test_모르는_ISBN은_건너뛴다() -> None:
    conn = _FakeConn({_A: 1, _C: 3})

    assert _to_book_ids(conn, [_A, _B, _C]) == [1, 3]


def test_모르는_ISBN이_있으면_개수를_남긴다(caplog: pytest.LogCaptureFixture) -> None:
    conn = _FakeConn({_A: 1})

    with caplog.at_level(logging.WARNING, logger="app.core.isbn"):
        _to_book_ids(conn, [_A, _B, _C, _B])

    # 같은 ISBN 을 두 번 보내도 모르는 책은 두 권이다.
    assert "2개" in caplog.text
    # 값은 남기지 않는다. 산 책·담은 책 목록이 로그에 쌓인다.
    assert _B not in caplog.text
    assert _C not in caplog.text


def test_다_아는_ISBN이면_아무것도_남기지_않는다(
    caplog: pytest.LogCaptureFixture,
) -> None:
    conn = _FakeConn({_A: 1, _B: 2})

    with caplog.at_level(logging.WARNING, logger="app.core.isbn"):
        _to_book_ids(conn, [_A, _B])

    assert caplog.text == ""


def test_빈_목록이면_DB에_묻지_않는다() -> None:
    conn = _FakeConn({_A: 1})

    assert _to_book_ids(conn, []) == []
    assert conn.queries == 0


# ---------------------------------------------------------------------------
# 실제 DB 가 필요한 테스트
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


@needs_db
def test_책_표에서_ISBN으로_book_id를_찾는다() -> None:
    async def _go() -> list[int]:
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.executemany(
                "INSERT INTO v_books (book_id, title, price, in_stock, isbn13)"
                " VALUES ($1, '테스트', 10000, true, $2)",
                [(9300001, _A), (9300002, _B), (9300003, None)],
            )
            return await isbn.to_book_ids(conn, [_B, _C, _A])
        finally:
            await tx.rollback()
            await conn.close()

    assert asyncio.run(_go()) == [9300002, 9300001]
