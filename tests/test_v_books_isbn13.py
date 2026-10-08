"""책 표의 isbn13 칸이 지키는 조건(#309). 실제 PostgreSQL 이 있어야 돈다.

BE 와 책을 ISBN 으로 주고받게 되면(#308) 이 칸이 두 서버를 잇는 키다. 틀린 값이 들어가도
에러 없이 엉뚱한 책이 이어지므로, DB 가 넣는 순간에 막는지 확인한다.
"""

import asyncio
import os

import asyncpg
import pytest

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_INSERT = (
    "INSERT INTO v_books (book_id, title, price, in_stock, isbn13)"
    " VALUES ($1, '테스트', 10000, true, $2)"
)


def _insert(*rows: tuple[int, str | None]) -> None:
    """rows 를 차례로 넣어 보고 되돌린다. DB 가 거절하면 그 예외가 그대로 올라온다."""

    async def _go() -> None:
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id, isbn13 in rows:
                await conn.execute(_INSERT, book_id, isbn13)
        finally:
            await tx.rollback()
            await conn.close()

    asyncio.run(_go())


@needs_db
def test_13자리_숫자는_들어간다() -> None:
    _insert((9300001, "9788936434120"))


@needs_db
def test_아직_채우지_않은_책은_비워_둘_수_있다() -> None:
    # 값은 BE 에서 받아 나중에 채운다. 빈 책이 여럿이어도 겹친 것으로 보지 않는다.
    _insert((9300001, None), (9300002, None))


@needs_db
@pytest.mark.parametrize(
    "isbn13",
    [
        "978-89-364-3412-0",  # 하이픈
        "978893643412",  # 12자리
        "97889364341200",  # 14자리
        "897364341X",  # 10자리 ISBN
        "978893643412X",  # 글자가 섞임
        " 9788936434120",  # 공백
        "",
    ],
)
def test_13자리_숫자가_아니면_거절한다(isbn13: str) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        _insert((9300001, isbn13))


@needs_db
def test_같은_ISBN을_두_책에_넣을_수_없다() -> None:
    with pytest.raises(asyncpg.UniqueViolationError):
        _insert((9300001, "9788936434120"), (9300002, "9788936434120"))
