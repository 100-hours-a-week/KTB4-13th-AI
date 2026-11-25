"""Elasticsearch 판정(#754) 전용: 키워드·벡터 후보를 Elasticsearch 에서 받는다. 브랜치에만 있고 main 에 넣지 않는다.

환경 변수 SEARCH_ES 로 고른다. 비어 있으면 지금 검색과 똑같다.

- `keyword` — 후보 A. 키워드 후보만 Elasticsearch(nori, BM25). 벡터는 pgvector 그대로.
- `keyword,vector` — 후보 B. 벡터 후보도 Elasticsearch(같은 e5 벡터, 근사 최근접).

엔진만 바꾼다. 후보 수(키워드 200, 벡터 50), 필드 무게(제목 3·저자 2·소개 0.5), 필터, 합치기·제목 우선·정렬
(service.py)은 지금 코드 그대로다. 질의는 문서 기본값으로 고정하고 튜닝하지 않는다(V4 최종안 5절):

- 키워드는 multi_match 기본(best_fields, 낱말은 OR)에 오타 허용 `fuzziness: AUTO` 만 준다. 지금 검색의
  선(MIN_COVERAGE 0.6) 같은 장치는 없다.
- 벡터는 knn 기본(num_candidates = k 의 1.5배, BBQ 색인은 3배로 더 받아 원래 값으로 다시 매김)이다.

검색어는 지금 키워드 검색과 같게 NFKC 로 바꾸고, 글자·숫자가 하나도 없으면 0건이다(keyword.search_ids 와 같음).
점수가 같으면 book_id 가 작은 쪽이 먼저다(지금 키워드 검색과 같음).
"""

import os
from typing import Any

import asyncpg
import httpx

from app.core import categories
from app.search import keyword, vector
from app.search.schemas import SearchFilters

ES_URL = os.environ.get("ES_URL", "http://localhost:9200")
INDEX = "books"
TIMEOUT_SECONDS = 10
FIELDS = [
    f"title^{keyword.W_TITLE}",
    f"author^{keyword.W_AUTHOR}",
    f"description^{keyword.W_DESCRIPTION}",
]


def engines() -> set[str]:
    """Elasticsearch 로 바꾼 쪽(keyword, vector)."""
    return {
        part.strip()
        for part in os.environ.get("SEARCH_ES", "").split(",")
        if part.strip()
    }


def filter_clauses(filters: SearchFilters) -> list[dict[str, Any]]:
    """filters.build_where 와 같은 조건. 값이 없는 책은 범위 조건에서 빠진다(SQL 의 NULL 비교와 같다)."""
    clauses: list[dict[str, Any]] = []
    if filters.category is not None:
        catalog = categories.filter_categories(filters.category)
        if catalog is None:
            catalog = (filters.category,)
        if catalog:
            clauses.append({"terms": {"category": list(catalog)}})
    price = {
        op: value
        for op, value in (("gte", filters.price_min), ("lte", filters.price_max))
        if value is not None
    }
    if price:
        clauses.append({"range": {"price": price}})
    year = {
        op: value
        for op, value in (("gte", filters.pub_year_from), ("lte", filters.pub_year_to))
        if value is not None
    }
    if year:
        clauses.append({"range": {"pub_year": year}})
    return clauses


async def _search(body: dict[str, Any]) -> list[int]:
    async with httpx.AsyncClient(base_url=ES_URL, timeout=TIMEOUT_SECONDS) as client:
        response = await client.post(f"/{INDEX}/_search", json=body)
    response.raise_for_status()
    return [int(hit["_id"]) for hit in response.json()["hits"]["hits"]]


async def keyword_search_ids(
    conn: asyncpg.Connection,
    query: str,
    filters: SearchFilters,
) -> list[int]:
    """키워드 후보. SEARCH_ES 에 keyword 가 없으면 지금 키워드 검색(부르는 모양도 그대로)."""
    if "keyword" not in engines():
        return await keyword.search_ids(conn, query, filters)
    query = keyword.normalize_query(query)
    if not keyword.tokenize(query):
        return []
    match = {
        "multi_match": {"query": query.strip(), "fields": FIELDS, "fuzziness": "AUTO"}
    }
    return await _search(
        {
            "size": keyword.CANDIDATE_LIMIT,
            "_source": False,
            "query": {"bool": {"must": match, "filter": filter_clauses(filters)}},
            "sort": ["_score", {"book_id": "asc"}],
        }
    )


async def vector_search_ids(
    conn: asyncpg.Connection,
    query_vector: list[float],
    filters: SearchFilters,
) -> list[int]:
    """벡터 후보. SEARCH_ES 에 vector 가 없으면 지금 벡터 검색(부르는 모양도 그대로)."""
    if "vector" not in engines():
        return await vector.search_ids(conn, query_vector, filters)
    limit = vector.CANDIDATE_LIMIT
    knn: dict[str, Any] = {
        "field": "embedding",
        "query_vector": query_vector,
        "k": limit,
    }
    clauses = filter_clauses(filters)
    if clauses:
        knn["filter"] = clauses
    return await _search({"knn": knn, "size": limit, "_source": False})
