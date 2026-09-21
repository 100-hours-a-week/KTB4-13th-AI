"""book_id 목록을 응답에 실을 책 정보로 바꾼다."""

from typing import Any

import asyncpg

_SQL = """
SELECT b.book_id, b.title, b.author, b.publisher, b.price, b.in_stock, b.cover_url
FROM unnest($1::int[]) WITH ORDINALITY AS ids(book_id, pos)
JOIN v_books b USING (book_id)
ORDER BY ids.pos
"""


async def fetch(conn: asyncpg.Connection, book_ids: list[int]) -> list[dict[str, Any]]:
    """받은 순서 그대로 돌려준다. 그사이 카탈로그에서 빠진 책은 조용히 빠진다.

    author·publisher·cover_url 은 값이 없는 책이 있어 null 로 나갈 수 있다(002 마이그레이션).
    """
    if not book_ids:
        return []
    rows = await conn.fetch(_SQL, book_ids)
    return [dict(r) for r in rows]
