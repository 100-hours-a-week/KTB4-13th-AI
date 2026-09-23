"""④ 개인화를 끈 목록(cold_start) — 인기 점수 높은 순, 같으면 신간순(명세 ④).

인기 행이 없는 책은 0점이라 뒤로 가고, 그 안에서는 출간연도 최신순이 된다. BE 인기 집계가 들어오기
전(#56)에는 사실상 신간순이다.
"""

from typing import Any

import asyncpg

from app.core import history, popularity
from app.feed.schemas import FeedRequest
from app.search.filters import build_where
from app.search.schemas import SearchFilters

_POPULARITY = popularity.score_sql("p")

# 마지막 b.book_id 는 값이 모두 같을 때 순서를 고정한다. 페이지를 넘길 때(#108) 순서가 흔들리면
# 한 권이 겹치거나 빠진다.
_ORDER_BY = {
    "match": f"{_POPULARITY} DESC, b.pub_year DESC NULLS LAST, b.book_id",
    "newest": f"b.pub_year DESC NULLS LAST, {_POPULARITY} DESC, b.book_id",
    "price_asc": "b.price ASC, b.book_id",
}

# 이미 산 책, 나의 도서관에 담은 책, 2.0점 이하 리뷰를 단 책은 뺀다(명세 ④).
# 좋은 리뷰만 단 책(사지도 담지도 않음)은 명세의 제외 대상이 아니라 남긴다.
_SQL = """
SELECT b.book_id, b.title, b.author, b.price, b.cover_url, b.in_stock
FROM v_books b
LEFT JOIN v_book_popularity p USING (book_id)
WHERE NOT EXISTS (
        SELECT 1 FROM v_user_purchases x WHERE x.user_id = $1 AND x.book_id = b.book_id)
  AND NOT EXISTS (
        SELECT 1 FROM v_user_library x WHERE x.user_id = $1 AND x.book_id = b.book_id)
  AND NOT EXISTS (
        SELECT 1 FROM v_user_reviews x
        WHERE x.user_id = $1 AND x.book_id = b.book_id AND x.rating <= $2)
  {where}
ORDER BY {order_by}
LIMIT $3
"""


async def fetch(conn: asyncpg.Connection, req: FeedRequest) -> list[dict[str, Any]]:
    """첫 페이지 size 권. 모두 cold_start 라 match_score 는 0 이다. 이어 붙이기는 #108."""
    # 필터는 ① 검색과 같은 조건식을 쓴다. 출간연도가 비어 있는 책은 연도 필터를 걸면 빠진다.
    filters = SearchFilters(
        category=req.category,
        pub_year_from=req.pub_year_from,
        pub_year_to=req.pub_year_to,
    )
    where, params = build_where(filters, first_param=4)
    sql = _SQL.format(where=where, order_by=_ORDER_BY[req.sort])
    rows = await conn.fetch(
        sql, req.user_id, history.DISLIKED_MAX_RATING, req.size, *params
    )
    return [{**dict(r), "match_score": 0} for r in rows]
