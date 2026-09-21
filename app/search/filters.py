"""화면 필터를 SQL 조건으로 바꾼다. 키워드 검색과 벡터 검색이 같은 조건을 써야 한다."""

from typing import Any

from app.search.schemas import SearchFilters


def build_where(
    filters: SearchFilters, first_param: int, alias: str = "b"
) -> tuple[str, list[Any]]:
    """(" AND ..." 조각, 값 목록) 을 돌려준다. 조건이 없으면 ("", []).

    값은 전부 $n 자리로 넘긴다. 글자를 SQL 에 직접 이어 붙이지 않는다.
    """
    conditions: list[tuple[str, Any]] = []
    if filters.category is not None:
        conditions.append((f"{alias}.category = ", filters.category))
    if filters.price_min is not None:
        conditions.append((f"{alias}.price >= ", filters.price_min))
    if filters.price_max is not None:
        conditions.append((f"{alias}.price <= ", filters.price_max))
    # 출간연도가 비어 있는 책은 연도 조건을 걸면 빠진다(NULL 비교는 참이 아니다).
    if filters.pub_year_from is not None:
        conditions.append((f"{alias}.pub_year >= ", filters.pub_year_from))
    if filters.pub_year_to is not None:
        conditions.append((f"{alias}.pub_year <= ", filters.pub_year_to))

    sql = ""
    params: list[Any] = []
    for i, (prefix, value) in enumerate(conditions):
        sql += f" AND {prefix}${first_param + i}"
        params.append(value)
    if filters.in_stock_only:
        sql += f" AND {alias}.in_stock"
    return sql, params
