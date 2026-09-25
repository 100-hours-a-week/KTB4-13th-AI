"""④ 취향 프로필이 있는 사용자의 목록 — 취향 벡터로 후보를 뽑아 채점한다(명세 ④).

13만 권(운영 58만 권)을 요청마다 전부 채점하면 느리다. 취향 벡터와 가까운 책만 벡터 색인으로
뽑아 그 안에서 점수를 매긴다. 탐색 폭과 필터는 ① 벡터 검색과 같게 둔다.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any

import asyncpg

from app.core import history, popularity, products
from app.core.pgvector import to_vector_literal
from app.feed import scoring
from app.feed.cursor import Page
from app.feed.schemas import FeedRequest
from app.search.filters import build_where
from app.search.schemas import SearchFilters
from app.search.vector import EF_SEARCH

# 채점할 후보 수. 첫 페이지(최대 50권)보다 넉넉해야 필터로 걸러지고도 남는다.
CANDIDATE_LIMIT = 500

# 카탈로그 최고 인기 점수. 인기 점수가 판매 수라(popularity.py) 판매 수 색인으로 바로 읽는다.
# 식(coalesce)으로 감싸면 색인을 못 타 인기 표 전체를 훑는다.
_MAX_POPULARITY_SQL = "SELECT max(sales) FROM v_book_popularity"

# 제외 규칙은 cold_start 목록(#106)과 같다. 산 책·담은 책·2.0점 이하 리뷰를 단 책.
_SQL = f"""
WITH nearest AS (
    SELECT e.book_id, {{distance}} AS distance
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
SELECT b.book_id, b.title, b.author, {products.price_sql("b")} AS price, b.cover_url,
       {products.in_stock_sql("b")} AS in_stock, b.category, b.pub_year,
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
    # 상품이 없어 가격이 없는 책은 맨 뒤다(① 가격순과 같음).
    "price_asc": lambda row: (row["price"] is None, row["price"] or 0, row["book_id"]),
}


async def catalog_max_popularity(conn: asyncpg.Connection) -> float | None:
    """카탈로그에서 가장 높은 인기 점수. 인기 항을 0–1 로 바꾸는 기준이다(#109 임시값)."""
    return await conn.fetchval(_MAX_POPULARITY_SQL)


# 필터를 건 더보기에서 가까운 책을 어떻게 뽑을지(#196). 벡터 색인은 가까운 순으로 훑으며 필터에 맞는
# 책을 모으는데, 취향과 먼 분류면 한참 훑어도 몇 권 못 찾고 멈춘다. 필터에 걸리는 책이 적으면 그 책들과
# 거리를 전부 계산하는 편이 빠르고 정확하다(261만 권 기준 5만 권 약 80ms, 19만 권 약 340ms).
EXACT_LIMIT = 50_000
# 벡터 색인으로 뽑았는데 CANDIDATE_LIMIT 에 모자라면, 걸리는 책이 이만큼 이하일 때만 정확 계산으로 다시 뽑는다.
FALLBACK_LIMIT = 200_000
# 필터가 있을 때 벡터 색인이 더 멀리 훑게 한다. 기본값(메모리 1배, 2만 건)이면 로컬 13만 권에서 경제학
# 필터가 443권, 법학 필터가 18권에서 멈췄다. 4배·10만 건이면 둘 다 500권을 채웠다.
FILTERED_SCAN_MEM_MULTIPLIER = 4
FILTERED_MAX_SCAN_TUPLES = 100_000

_INDEX_DISTANCE = "e.embedding <=> $1::vector"
# 식을 바꿔 벡터 색인을 못 쓰게 한다. 필터 조건의 색인으로 책을 고른 뒤 거리를 전부 계산한다.
_EXACT_DISTANCE = "(e.embedding <=> $1::vector) + 0"

_COUNT_SQL = """
SELECT count(*) FROM (SELECT 1 FROM v_books b WHERE true {where} LIMIT $1) s
"""


async def _nearest(
    conn: asyncpg.Connection,
    req: FeedRequest,
    page: Page,
    centroid: list[float],
    filters: SearchFilters,
) -> list[asyncpg.Record]:
    """취향 벡터와 가까운 책 CANDIDATE_LIMIT 권. 필터가 있으면 걸리는 책 수로 뽑는 방법을 정한다.

    같은 요청(같은 필터)이면 어느 DB 연결에서 돌아도 같은 방법·같은 계획으로 뽑는다. 쪽마다 다시 뽑기
    때문에, 쪽마다 방법이 달라지면 가까운 500권이 달라져 책이 중복되거나 빠진다(#196).
    """
    where, filter_params = build_where(filters, first_param=6)
    filtered = bool(where)
    count = 0
    if filtered:
        count_where, count_params = build_where(filters, first_param=2)
        count = await conn.fetchval(
            _COUNT_SQL.format(where=count_where), FALLBACK_LIMIT + 1, *count_params
        )

    async def _run(distance: str) -> list[asyncpg.Record]:
        # SET LOCAL 은 트랜잭션 안에서만 먹는다(① 벡터 검색과 같은 방식).
        async with conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}")
            await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
            # 같은 쿼리를 여러 번 돌리면 PostgreSQL 이 값과 상관없는 계획으로 바꾸는데, 그 시점이 연결마다
            # 달라 쪽마다 뽑는 방법이 갈렸다. 매번 이번 값으로 계획을 세우게 한다.
            await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
            if filtered:
                await conn.execute(
                    "SET LOCAL hnsw.scan_mem_multiplier = "
                    f"{FILTERED_SCAN_MEM_MULTIPLIER}"
                )
                await conn.execute(
                    f"SET LOCAL hnsw.max_scan_tuples = {FILTERED_MAX_SCAN_TUPLES}"
                )
            return await conn.fetch(
                _SQL.format(distance=distance, where=where),
                to_vector_literal(centroid),
                req.user_id,
                history.DISLIKED_MAX_RATING,
                CANDIDATE_LIMIT,
                page.issued_at,
                *filter_params,
            )

    if filtered and count <= EXACT_LIMIT:
        return await _run(_EXACT_DISTANCE)
    rows = await _run(_INDEX_DISTANCE)
    if filtered and len(rows) < CANDIDATE_LIMIT and count <= FALLBACK_LIMIT:
        # 멀리 훑어도 모자라면 필터 안에서 정확히 다시 뽑는다. 명세상 필터로 줄어드는 건 0건일 때뿐이다.
        rows = await _run(_EXACT_DISTANCE)
    return rows


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
    rows = await _nearest(conn, req, page, centroid, filters)
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
