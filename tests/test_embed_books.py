"""책 벡터 채우기 작업 테스트.

대부분 DB 와 모델 없이 돈다 — 연결과 embed 를 가짜로 바꿔 끼운다. 실행 계획 테스트만 실제 DB 가 필요하다.
"""

import asyncio
import os

import asyncpg
import pytest

from app.core.pgvector import to_vector_literal
from app.jobs import embed_books


class FakeConn:
    """책 조회는 book_id > last_id 인 책을 앞에서부터, 벡터 조회는 이미 채운 책을 돌려준다.

    executemany 는 받은 걸 적어 둔다.
    """

    def __init__(self, books: list[dict], done: set[int] = frozenset()) -> None:
        self.books = books
        self.done = done
        self.saved: list[tuple] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        if "FROM v_books" in sql:
            last_id, limit = args
            return [b for b in self.books if b["book_id"] > last_id][:limit]
        ids, _model = args
        return [{"book_id": i} for i in ids if i in self.done]

    async def executemany(self, _sql: str, args: list[tuple]) -> None:
        self.saved.extend(args)


def _book(book_id: int) -> dict:
    return {
        "book_id": book_id,
        "title": f"책{book_id}",
        "author": "저자",
        "description": "소개",
    }


def _fake_embed(dim: int = 384, drop: int = 0):
    async def _embed(texts: list[str], purpose: str):
        assert purpose == "document"
        return [[0.5] * dim for _ in texts[: len(texts) - drop]], dim, "test-model"

    return _embed


def test_제목_저자_소개글_순으로_붙인다() -> None:
    assert embed_books.build_document("여행의 이유", "김영하", "소개글") == (
        "여행의 이유\n김영하\n소개글"
    )


def test_저자가_없으면_빼고_붙인다() -> None:
    assert embed_books.build_document("제목", None, "소개글") == "제목\n소개글"


def test_벡터를_pgvector_글자_표기로_바꾼다() -> None:
    assert to_vector_literal([0.5, -1.0]) == "[0.5,-1.0]"


def test_묶음_크기로_나눠_전부_저장한다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_books.embedding, "embed", _fake_embed())
    conn = FakeConn([_book(i) for i in range(1, 6)])

    total = asyncio.run(embed_books.run(conn, batch_size=2))

    assert total == 5
    assert [row[0] for row in conn.saved] == [1, 2, 3, 4, 5]
    assert conn.saved[0][2:] == (384, "test-model")


def test_채울_책이_없으면_0권이다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_books.embedding, "embed", _fake_embed())

    assert asyncio.run(embed_books.run(FakeConn([]))) == 0


def test_벡터_길이가_설정과_다르면_저장하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embed_books.embedding, "embed", _fake_embed(dim=1024))
    conn = FakeConn([_book(1)])

    with pytest.raises(RuntimeError, match="벡터 길이"):
        asyncio.run(embed_books.run(conn))
    assert conn.saved == []


def test_벡터_개수가_책_권수와_다르면_저장하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(embed_books.embedding, "embed", _fake_embed(drop=1))
    conn = FakeConn([_book(1), _book(2)])

    with pytest.raises(RuntimeError, match="벡터 개수"):
        asyncio.run(embed_books.run(conn))
    assert conn.saved == []


def test_이미_지금_모델로_만든_책은_건너뛴다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(embed_books.embedding, "embed", _fake_embed())
    conn = FakeConn([_book(i) for i in range(1, 6)], done={2, 4})

    assert asyncio.run(embed_books.run(conn)) == 3
    assert [row[0] for row in conn.saved] == [1, 3, 5]


def test_훑은_구간이_모두_채워져_있어도_다음_구간으로_넘어간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 채울 책이 없는 구간을 끝으로 보면, 앞쪽이 다 채워진 채로 끊겼다 다시 돌릴 때 뒤를 못 채운다.
    monkeypatch.setattr(embed_books.embedding, "embed", _fake_embed())
    conn = FakeConn([_book(i) for i in range(1, 6)], done={1, 2})

    total = asyncio.run(embed_books.run(conn, batch_size=2, scan_size=2))

    assert total == 3
    assert [row[0] for row in conn.saved] == [3, 4, 5]


_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")


@pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)
@pytest.mark.parametrize(
    ("sql", "args"),
    [
        (embed_books._SCAN, (0, embed_books.SCAN_SIZE)),
        (embed_books._DONE, (list(range(1, 2001)), "intfloat/multilingual-e5-small")),
    ],
)
def test_표_전체를_읽지_않는다(sql: str, args: tuple) -> None:
    # 한 쿼리로 "벡터가 없는 책"을 고르면 묶음마다 두 표를 통째로 읽었다(#194).
    async def _go() -> str:
        conn = await asyncpg.connect(_DB_URL)
        try:
            rows = await conn.fetch("EXPLAIN " + sql, *args)
            return "\n".join(r[0] for r in rows)
        finally:
            await conn.close()

    plan = asyncio.run(_go())

    assert "Seq Scan" not in plan
