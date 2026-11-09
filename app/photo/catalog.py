"""사진에서 찾은 책을 책 표에서 읽는다(#615). 표지 규칙과 책장 규칙이 같이 쓴다."""

from dataclasses import dataclass

import asyncpg

from app.core import products


@dataclass(frozen=True)
class Book:
    book_id: int
    isbn: str | None
    title: str
    author: str | None
    publisher: str | None
    pub_year: int | None
    price: int | None
    in_stock: bool
    cover_url: str | None


COLUMNS = f"""
SELECT b.book_id, b.isbn13 AS isbn, b.title, b.author, b.publisher, b.pub_year,
       {products.price_sql("b")} AS price, {products.in_stock_sql("b")} AS in_stock,
       b.cover_url
FROM v_books b
"""
BY_IDS_SQL = COLUMNS + "WHERE b.book_id = ANY($1::int[])"
BY_ISBN_SQL = COLUMNS + "WHERE b.isbn13 = $1"


def is_valid_isbn13(isbn: str) -> bool:
    """13자리 숫자이고 마지막 검증 숫자가 맞는가. 모델이 숫자 하나를 잘못 읽으면 대부분 여기서 걸린다."""
    if len(isbn) != 13 or not isbn.isdigit():
        return False
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(isbn[:12]))
    return (10 - total % 10) % 10 == int(isbn[12])


def book(row: asyncpg.Record) -> Book:
    return Book(**dict(row))


async def by_isbn(conn: asyncpg.Connection, isbn: str | None) -> Book | None:
    """검증 숫자가 맞는 ISBN 이면 그 책. 판본까지 정확하다. 틀린 ISBN 은 책 표를 읽지 않는다."""
    if not isbn or not is_valid_isbn13(isbn):
        return None
    row = await conn.fetchrow(BY_ISBN_SQL, isbn)
    return book(row) if row is not None else None
