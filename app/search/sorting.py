"""관련도순이 아닌 정렬(최신·가격·인기). 이미 추려진 후보 안에서만 줄을 세운다."""

import asyncpg

from app.core import popularity

# 값이 같으면 관련도 등수(pos)가 앞선 책이 먼저다. pub_year 가 없는 책은 최신순에서 맨 뒤로 간다.
_ORDER_BY = {
    "newest": "b.pub_year DESC NULLS LAST, ids.pos",
    "price_asc": "b.price ASC, ids.pos",
    "price_desc": "b.price DESC, ids.pos",
    "popular": f"{popularity.score_sql('p')} DESC, ids.pos",
}

_SQL = """
SELECT b.book_id
FROM unnest($1::int[]) WITH ORDINALITY AS ids(book_id, pos)
JOIN v_books b USING (book_id)
LEFT JOIN v_book_popularity p USING (book_id)
ORDER BY {order_by}
"""


async def sort_ids(
    conn: asyncpg.Connection, book_ids: list[int], sort: str
) -> list[int]:
    """관련도 순서로 받은 book_id 를 sort 기준으로 다시 줄 세운다."""
    if not book_ids:
        return []
    rows = await conn.fetch(_SQL.format(order_by=_ORDER_BY[sort]), book_ids)
    return [r["book_id"] for r in rows]
