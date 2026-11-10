"""book_id 목록을 응답에 실을 책 정보로 바꾼다."""

from typing import Any

import asyncpg

from app.core import products

# 가격·재고는 상품 표에서 읽는다. 책 표의 가격 칸은 복제가 채우지 않는다(#206).
_SQL = f"""
SELECT b.book_id, b.isbn13 AS isbn, b.title, b.author, b.publisher,
       {products.price_sql("b")} AS price, {products.in_stock_sql("b")} AS in_stock,
       b.cover_url
FROM unnest($1::int[]) WITH ORDINALITY AS ids(book_id, pos)
JOIN v_books b USING (book_id)
ORDER BY ids.pos
"""
# 응답에 나가는 칸(명세 ①). BE 와는 ISBN 으로만 책을 가리키고, book_id 는 AI 안에서만 쓴다(#308)
_RESPONSE = ("isbn", "title", "author", "publisher", "price", "in_stock", "cover_url")


async def fetch(conn: asyncpg.Connection, book_ids: list[int]) -> list[dict[str, Any]]:
    """받은 순서 그대로 돌려준다. 그사이 카탈로그에서 빠진 책은 조용히 빠진다.

    author·publisher·cover_url 은 값이 없는 책이 있어 null 로 나갈 수 있다(002 마이그레이션).
    상품이 없는 책은 price 가 null, in_stock 이 false 다.
    book_id 는 ③ 챗봇이 후보를 고를 때 쓴다. 응답에 실을 때는 response_item 으로 칸을 고른다.
    """
    if not book_ids:
        return []
    rows = await conn.fetch(_SQL, book_ids)
    return [dict(r) for r in rows]


def response_item(row: dict[str, Any]) -> dict[str, Any]:
    """응답에 나가는 칸만 남긴다. isbn 은 BE 가 책을 찾는 키다. ISBN 이 없는 책은 책 표에 없다(11/9 개발 0권)."""
    return {key: row[key] for key in _RESPONSE}
