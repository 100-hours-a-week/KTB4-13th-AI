"""④ 개인화를 끈 목록(cold_start) — 인기 점수 높은 순, 같으면 신간순(명세 ④).

인기 점수는 판매 수 순서라(app/core/popularity.py) 요청마다 카탈로그 전체를 정렬하지 않고 색인
순서대로 읽는다(#186). 판매 수가 있는 책(인기 행, 소수)을 판매 수 순으로 먼저, 나머지(0점)를
신간순 색인으로 이어 붙인다. 261만 권에서 전체 정렬은 한 쪽에 1초가 넘었고, 이렇게 읽으면 수 ms다.
"""

from typing import Any

import asyncpg

from app.core import history, products
from app.feed.cursor import Page
from app.feed.schemas import FeedRequest
from app.search.filters import build_where
from app.search.schemas import SearchFilters

# 목록 길이 상한. 개인화 목록(가까운 500권)과 같게 둔다. 쪽을 OFFSET 으로 넘겨서, 상한이 없으면
# 깊은 쪽일수록 앞의 책을 모두 다시 읽는다(261만 권에서 30만 번째 쪽 7초).
MAX_BOOKS = 500

# 이미 산 책, 나의 도서관에 담은 책, 2.0점 이하 리뷰를 단 책은 뺀다(명세 ④).
# 좋은 리뷰만 단 책(사지도 담지도 않음)은 명세의 제외 대상이 아니라 남긴다.
EXCLUDED_SQL = """
NOT EXISTS (
        SELECT 1 FROM v_user_purchases x
        WHERE x.user_id = $1 AND x.book_id = b.book_id AND x.purchased_at <= $3)
  AND NOT EXISTS (
        SELECT 1 FROM v_user_library x
        WHERE x.user_id = $1 AND x.book_id = b.book_id AND x.added_at <= $3)
  AND NOT EXISTS (
        SELECT 1 FROM v_user_reviews x
        WHERE x.user_id = $1 AND x.book_id = b.book_id AND x.rating <= $2
          AND x.created_at <= $3)
"""

# 가격·재고는 상품 표에서 읽는다. 책 표의 가격 칸은 복제가 채우지 않는다(#207).
_COLUMNS = (
    "b.book_id, b.title, b.author,"
    f" {products.price_sql('b')} AS price, b.cover_url, {products.in_stock_sql('b')} AS in_stock"
)


_OUTPUT = "u.book_id, u.title, u.author, u.price, u.cover_url, u.in_stock"


def popular_first_sql(
    where: str, extra: str = "", limit: str = "$4", columns: str = _OUTPUT
) -> str:
    """인기순(판매 수 → 신간 → 번호)으로 앞에서부터 읽는 쿼리. rule-only 후보도 같은 것을 쓴다.

    두 구간을 각각 색인 순서로 `limit` 권까지만 읽고, 그 결과만 바깥에서 한 번 더 정렬한다.
    바깥 정렬 없이 UNION ALL 순서에 기대면 PostgreSQL 이 병렬로 섞을 때 순서가 틀어지고, 두 구간
    전체를 바깥에서 정렬하면 267만 행을 정렬한다(8초). `extra` 는 두 구간에 함께 붙는 조건이고,
    `columns` 는 바깥으로 내보낼 칸이다(u.seg·u.sales·u.pub_year 가 순서를 정한다).
    """
    return f"""
SELECT {columns} FROM (
  (SELECT 0 AS seg, p.sales, b.pub_year, {_COLUMNS}
   FROM v_book_popularity p JOIN v_books b USING (book_id)
   WHERE p.sales > 0 AND {EXCLUDED_SQL} {where} {extra}
   ORDER BY p.sales DESC, b.pub_year DESC NULLS LAST, b.book_id
   LIMIT {limit})
  UNION ALL
  (SELECT 1, 0, b.pub_year, {_COLUMNS}
   FROM v_books b
   WHERE NOT EXISTS (SELECT 1 FROM v_book_popularity p WHERE p.book_id = b.book_id AND p.sales > 0)
     AND {EXCLUDED_SQL} {where} {extra}
   ORDER BY b.pub_year DESC NULLS LAST, b.book_id
   LIMIT {limit})
) u
ORDER BY u.seg, u.sales DESC, u.pub_year DESC NULLS LAST, u.book_id
"""


# 최신순·가격순은 인기와 상관없어 한 쿼리로 색인 순서대로 읽는다.
# 최신순은 책 표의 색인(#188)이다. 동점은 번호순이다. 출간일이 연도 단위라 한 연도에 수십만 권이
# 몰리는데, 그 안을 인기순으로 세우면 매번 그 연도 전체를 읽어야 한다(261만 권 320ms, #186).
# 가격순은 상품 표를 가격 색인(#205) 순서로 읽으며 책을 붙인다. 가격·재고도 읽는 그 상품의 값을 써서
# 순서와 보이는 가격이 어긋나지 않게 한다. 상품이 없는 책은 가격이 없어 가격순 목록에 나오지 않는다.
# 책 하나에 상품이 여럿이면 여기서만 같은 책이 두 번 나올 수 있다(지금 데이터는 1:1, #208).
ORDERED_SQL = {
    "newest": f"""
SELECT {_COLUMNS}
FROM v_books b
WHERE {EXCLUDED_SQL} {{where}}
ORDER BY b.pub_year DESC NULLS LAST, b.book_id
""",
    "price_asc": f"""
SELECT b.book_id, b.title, b.author, pr.discounted_price::int AS price, b.cover_url,
       pr.stock_quantity > 0 AS in_stock
FROM v_products pr
JOIN v_books b ON b.book_id = pr.book_id
WHERE {EXCLUDED_SQL} {{where}}
ORDER BY pr.discounted_price, pr.book_id
""",
}


async def run_ordered(conn: asyncpg.Connection, sql: str, *args: Any) -> list:
    """색인 순서대로 읽는 쿼리를 계획이 흔들리지 않게 돌린다.

    두 구간을 이어 붙이는 쿼리에 PostgreSQL 이 병렬 실행이나 전체 해시 조인을 고르면, 261만 권에서
    1.5초가 걸린다. 이 쿼리는 앞에서부터 조금만 읽고 끝나는 모양이라 둘 다 쓸 일이 없다.
    SET LOCAL 은 트랜잭션 안에서만 먹는다(① 검색과 같은 방식).
    """
    async with conn.transaction():
        await conn.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        await conn.execute("SET LOCAL enable_hashjoin = off")
        await conn.execute("SET LOCAL enable_mergejoin = off")
        return await conn.fetch(sql, *args)


async def fetch(
    conn: asyncpg.Connection, req: FeedRequest, page: Page
) -> tuple[list[dict[str, Any]], bool]:
    """(이번 페이지 목록, 다음 페이지가 있는지). 모두 cold_start 라 match_score 는 0 이다.

    커서를 받은 시각 뒤에 생긴 이력은 제외 대상에서 빼고 본다. 카드를 보고 돌아와 산 책이
    다음 페이지에서 사라지면 목록이 한 칸씩 밀린다(명세 ④).
    """
    size = min(req.size, MAX_BOOKS - page.offset)
    if size <= 0:
        return [], False
    # 필터는 ① 검색과 같은 조건식을 쓴다. 출간연도가 비어 있는 책은 연도 필터를 걸면 빠진다.
    filters = SearchFilters(
        category=req.category,
        pub_year_from=req.pub_year_from,
        pub_year_to=req.pub_year_to,
    )
    where, params = build_where(filters, first_param=6)
    if req.sort == "match":
        sql = popular_first_sql(where, limit="$4::int + $5::int") + "LIMIT $4 OFFSET $5"
    else:
        sql = ORDERED_SQL[req.sort].format(where=where) + "LIMIT $4 OFFSET $5"
    # 한 권 더 받아 본다. 더 있으면 다음 페이지 커서를 준다.
    rows = await run_ordered(
        conn,
        sql,
        req.user_id,
        history.DISLIKED_MAX_RATING,
        page.issued_at,
        size + 1,
        page.offset,
        *params,
    )
    has_more = len(rows) > size and page.offset + size < MAX_BOOKS
    items = [{**dict(r), "match_score": 0} for r in rows[:size]]
    return items, has_more
