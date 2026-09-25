"""화면 필터를 SQL 조건으로 바꾼다. 키워드 검색과 벡터 검색이 같은 조건을 써야 한다."""

from typing import Any

from app.core import categories, products
from app.search.schemas import SearchFilters


def build_where(
    filters: SearchFilters, first_param: int, alias: str = "b"
) -> tuple[str, list[Any]]:
    """(" AND ..." 조각, 값 목록) 을 돌려준다. 조건이 없으면 ("", []).

    값은 전부 $n 자리로 넘긴다. 글자를 SQL 에 직접 이어 붙이지 않는다. 조건 식의 {} 자리에 $n 이 들어간다.
    """
    conditions: list[tuple[str, Any]] = []
    if filters.category is not None:
        # 온보딩 값("소설")이면 대응표의 핵심 분류들로 푼다(#219). 카탈로그 분류명("한국문학")은 지금처럼
        # 정확히 일치하는 것만 거른다. ③ 챗봇은 LLM 이 준 분류명을 그대로 넘길 수 있다.
        catalog = categories.filter_categories(filters.category) or (filters.category,)
        if len(catalog) == 1:
            # 분류 하나는 = 로 건다. ANY 로 걸면 PostgreSQL 이 분류 + 신간순 색인을 못 골라 느려진다
            # (261만 권 여행 10쪽 61ms, #219).
            conditions.append((f"{alias}.category = {{}}", catalog[0]))
        else:
            conditions.append((f"{alias}.category = ANY({{}}::text[])", list(catalog)))
    # 가격·재고는 상품 표에서 읽는다(#206). 상품이 없는 책은 가격 조건을 걸면 빠지고 품절로 친다.
    if filters.price_min is not None:
        conditions.append((f"{products.price_sql(alias)} >= {{}}", filters.price_min))
    if filters.price_max is not None:
        conditions.append((f"{products.price_sql(alias)} <= {{}}", filters.price_max))
    # 출간연도가 비어 있는 책은 연도 조건을 걸면 빠진다(NULL 비교는 참이 아니다).
    if filters.pub_year_from is not None:
        conditions.append((f"{alias}.pub_year >= {{}}", filters.pub_year_from))
    if filters.pub_year_to is not None:
        conditions.append((f"{alias}.pub_year <= {{}}", filters.pub_year_to))

    sql = ""
    params: list[Any] = []
    for i, (condition, value) in enumerate(conditions):
        sql += " AND " + condition.format(f"${first_param + i}")
        params.append(value)
    if filters.in_stock_only:
        sql += f" AND {products.in_stock_sql(alias)}"
    return sql, params
