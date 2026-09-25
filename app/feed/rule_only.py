"""④ 취향 벡터를 못 쓸 때의 목록(rule-only) — 유사도를 빼고 카테고리·인기 점수로만 채점한다(명세 ④).

벡터가 없으니 가까운 책을 뽑을 수 없다. 대신 카테고리 점수가 양수인 분류의 책(점수 높은 분류부터)과
인기·신간순 책을 각각 CANDIDATE_LIMIT 권까지 모아 그 안에서 채점한다. 개인화 목록이 가까운 책 안에서만
고르는 것과 같은 방식이라, 점수 하한·정렬·페이지 자르기도 그대로 쓴다.

유사도 항을 뺀 나머지 비중을 1 로 키우지 않는다. 그래서 점수는 40 을 넘지 않는다(명세: 축소 응답도 같은 잣대).
"""

from typing import Any

import asyncpg

from app.core import history, popularity
from app.feed import cold_start, personalized, scoring
from app.feed.cursor import Page
from app.feed.schemas import FeedRequest
from app.search.filters import build_where
from app.search.schemas import SearchFilters

_POPULARITY = popularity.score_sql("p")

# 순서를 정하는 칸까지 내보낸다. 좋아하는 분류가 여럿이면 분류 점수 → 인기 → 신간 → 번호로 모은다.
_ORDER_COLUMNS = "u.book_id, u.seg, u.sales, u.pub_year"

# 후보는 개인화를 끈 목록과 같은 순서(판매 수 → 신간 → 번호)로 색인에서 앞부분만 읽는다(#189).
# 좋아하는 분류마다 그 분류 안에서 같은 순서로 CANDIDATE_LIMIT 권씩 읽고, 분류 점수가 높은 것부터 모은다.
_SQL = f"""
WITH liked AS (
    SELECT c.book_id
    FROM unnest($5::text[], $6::float8[]) AS w(category, points)
    CROSS JOIN LATERAL (
        {cold_start.popular_first_sql("{where}", extra="AND b.category = w.category", columns=_ORDER_COLUMNS)}
        LIMIT $4
    ) c
    ORDER BY w.points DESC, c.seg, c.sales DESC, c.pub_year DESC NULLS LAST, c.book_id
    LIMIT $4
), popular AS (
    {cold_start.popular_first_sql("{where}", columns="u.book_id")}
    LIMIT $4
)
SELECT b.book_id, b.title, b.author, b.price, b.cover_url, b.in_stock, b.category, b.pub_year,
       {_POPULARITY} AS popularity
FROM v_books b
LEFT JOIN v_book_popularity p USING (book_id)
WHERE b.book_id IN (SELECT book_id FROM liked UNION SELECT book_id FROM popular)
"""


async def fetch(
    conn: asyncpg.Connection,
    req: FeedRequest,
    page: Page,
    tag_weights: dict[str, float],
) -> tuple[list[dict[str, Any]], bool]:
    """(이번 페이지 목록, 다음 페이지가 있는지). 점수 하한과 정렬을 적용한다."""
    filters = SearchFilters(
        category=req.category,
        pub_year_from=req.pub_year_from,
        pub_year_to=req.pub_year_to,
    )
    where, filter_params = build_where(filters, first_param=7)
    liked = {category: points for category, points in tag_weights.items() if points > 0}
    rows = await cold_start.run_ordered(
        conn,
        _SQL.format(where=where),
        req.user_id,
        history.DISLIKED_MAX_RATING,
        page.issued_at,
        personalized.CANDIDATE_LIMIT,
        list(liked),
        [float(points) for points in liked.values()],
        *filter_params,
    )
    catalog_max = await personalized.catalog_max_popularity(conn)
    return personalized.rank(
        rows,
        req,
        page,
        lambda row: scoring.match_score(
            None,
            tag_weights.get(row["category"]),
            row["popularity"],
            catalog_max,
            with_similarity=False,
        ),
    )
