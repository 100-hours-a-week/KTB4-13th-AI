"""④ 취향 프로필이 있는 사용자의 목록 — 취향 벡터로 후보를 뽑아 채점한다(명세 ④).

13만 권(운영 58만 권)을 요청마다 전부 채점하면 느리다. 취향 벡터와 가까운 책만 벡터 색인으로
뽑아 그 안에서 점수를 매긴다. 탐색 폭과 필터는 ① 벡터 검색과 같게 둔다.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import asyncpg

from app.core import history, popularity
from app.core.pgvector import to_vector_literal
from app.feed import scoring
from app.feed.cursor import Page
from app.feed.schemas import FeedRequest
from app.search.filters import build_where
from app.search.schemas import SearchFilters
from app.search.vector import EF_SEARCH

# 채점할 후보 수. 첫 페이지(최대 50권)보다 넉넉해야 필터로 걸러지고도 남는다.
CANDIDATE_LIMIT = 500

_MAX_POPULARITY_SQL = (
    f"SELECT max({popularity.score_sql('p')}) FROM v_book_popularity p"
)

# 제외 규칙은 cold_start 목록(#106)과 같다. 산 책·담은 책·2.0점 이하 리뷰를 단 책.
_SQL = f"""
WITH nearest AS (
    SELECT e.book_id, e.embedding <=> $1::vector AS distance
    FROM book_embeddings e
    JOIN v_books b USING (book_id)
    WHERE NOT EXISTS (
            SELECT 1 FROM v_user_purchases x
            WHERE x.user_id = $2 AND x.book_id = b.book_id AND x.purchased_at <= $5)
      AND NOT EXISTS (
            SELECT 1 FROM v_user_library x
            WHERE x.user_id = $2 AND x.book_id = b.book_id AND x.added_at <= $5)
      AND NOT EXISTS (
            SELECT 1 FROM v_user_reviews x
            WHERE x.user_id = $2 AND x.book_id = b.book_id AND x.rating <= $3
              AND x.created_at <= $5)
      {{where}}
    ORDER BY distance
    LIMIT $4
)
SELECT b.book_id, b.title, b.author, b.price, b.cover_url, b.in_stock, b.category, b.pub_year,
       1 - n.distance AS similarity,
       coalesce({popularity.score_sql("p")}, 0) AS popularity
FROM nearest n
JOIN v_books b USING (book_id)
LEFT JOIN v_book_popularity p USING (book_id)
"""

# 점수는 정수라 같은 점수가 흔하다. 같으면 개인화를 끈 목록과 같게 인기 → 신간 → 번호 순이다.
_ORDER = {
    "match": lambda row: (
        -row["match_score"],
        -row["popularity"],
        -(row["pub_year"] or 0),
        row["book_id"],
    ),
    "newest": lambda row: (-(row["pub_year"] or 0), row["book_id"]),
    "price_asc": lambda row: (row["price"], row["book_id"]),
}


async def catalog_max_popularity(conn: asyncpg.Connection) -> float | None:
    """카탈로그에서 가장 높은 인기 점수. 인기 항을 0–1 로 바꾸는 기준이다(#109 임시값)."""
    return await conn.fetchval(_MAX_POPULARITY_SQL)


async def fetch(
    conn: asyncpg.Connection,
    req: FeedRequest,
    page: Page,
    centroid: list[float],
    tag_weights: dict[str, float],
) -> tuple[list[dict[str, Any]], bool]:
    """(이번 페이지 목록, 다음 페이지가 있는지). 점수 하한과 정렬을 적용한다.

    커서를 받은 시각 뒤에 생긴 이력은 제외 대상에서 빼고 본다(명세 ④).
    """
    filters = SearchFilters(
        category=req.category,
        pub_year_from=req.pub_year_from,
        pub_year_to=req.pub_year_to,
    )
    where, filter_params = build_where(filters, first_param=6)
    sql = _SQL.format(where=where)

    # SET LOCAL 은 트랜잭션 안에서만 먹는다(① 벡터 검색과 같은 방식).
    async with conn.transaction():
        await conn.execute(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}")
        await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
        rows = await conn.fetch(
            sql,
            to_vector_literal(centroid),
            req.user_id,
            history.DISLIKED_MAX_RATING,
            CANDIDATE_LIMIT,
            page.issued_at,
            *filter_params,
        )
        catalog_max = await catalog_max_popularity(conn)

    return rank(
        rows,
        req,
        page,
        lambda row: scoring.match_score(
            row["similarity"],
            tag_weights.get(row["category"]),
            row["popularity"],
            catalog_max,
        ),
    )


def rank(
    rows: Iterable[Mapping[str, Any]],
    req: FeedRequest,
    page: Page,
    score: Callable[[Mapping[str, Any]], int],
) -> tuple[list[dict[str, Any]], bool]:
    """후보를 채점해 점수 하한과 정렬을 적용하고 이번 페이지만 잘라 낸다.

    벡터를 못 쓸 때의 목록(rule-only)도 같은 규칙을 써야 해서 따로 뺐다.
    """
    items = []
    for row in rows:
        match_score = score(row)
        if req.match_score_min is not None and match_score < req.match_score_min:
            continue
        items.append({**dict(row), "match_score": match_score})

    items.sort(key=_ORDER[req.sort])
    window = items[page.offset : page.offset + req.size]
    has_more = len(items) > page.offset + req.size
    return [_response_item(row) for row in window], has_more


def _response_item(row: dict[str, Any]) -> dict[str, Any]:
    """응답에 나가는 칸만 남긴다(명세 ④). 채점에 쓴 유사도·분류는 내보내지 않는다."""
    return {
        "book_id": row["book_id"],
        "title": row["title"],
        "author": row["author"],
        "price": row["price"],
        "cover_url": row["cover_url"],
        "in_stock": row["in_stock"],
        "match_score": row["match_score"],
    }
