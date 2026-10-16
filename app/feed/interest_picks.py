"""④ 관심사별 목록 — 관심사마다 그 관심사의 도서관 분류 안에서 검색어와 가까운 책을 뽑는다(#449, 설계 #395).

검색어는 그 관심사의 책 벡터 평균이고, 책이 없으면 이름 벡터다. 뽑은 책 가운데 세부 분류 번호가 이 관심사로
이어지는 책(AI 책 분류 표)을 앞에 둔다. 말한 "요리"도 594 책부터 나온다.
작가 관심사는 저자 칸으로, 키워드 관심사는 핵심어의 이름 벡터로 뽑는다(#450). 시간 예산을 넘긴 관심사는
그 분류의 인기 있는 책으로 대신한다(#451).
"""

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.core import categories
from app.core.pgvector import to_vector_literal
from app.feed import cold_start, scoring
from app.feed.personalized import (
    EXACT_LIMIT,
    FALLBACK_LIMIT,
    FILTERED_MAX_SCAN_TUPLES,
    FILTERED_SCAN_MEM_MULTIPLIER,
)
from app.profile import interests

# 관심사 하나를 뽑을 때 벡터 색인을 얼마나 넓게 훑는지. 지금 홈 추천과 ① 검색은 1000 이지만, 목록 하나의
# 3초 예산은 운영에서 200 으로 잰 시간에 맞춰 정했다(#399). 운영에서 1000 이면 큰 분류 하나가 2.7–7초라 첫
# 관심사가 예산을 다 쓴다. 200 은 0.2–2.1초이고, 정확히 다 견준 결과와 평균 89% 같다(#478)
PICK_EF_SEARCH = 200

# 세부 카테고리 → 속한 카테고리. 어느 카테고리에도 속하지 않는 세부 카테고리는 None 이다
_PARENT = {
    name: parent for parent, group in categories.SUBCATEGORIES.items() for name in group
}

# 자리 번호: 검색어($1) → 뽑을 권수($2) → 도서관 분류($3, 좁힐 때만). 가까운 순으로 뽑은 뒤 책 정보와
# 세부 분류 번호를 붙인다. 벡터 색인은 가까운 순서를 대강만 지키므로(relaxed_order) 다시 줄 세운다.
_SQL = """
WITH nearest AS (
    SELECT e.book_id, {distance} AS distance
    FROM book_embeddings e
    JOIN v_books b USING (book_id)
    {where}
    ORDER BY distance
    LIMIT $2
)
SELECT n.book_id, b.isbn13, b.title, b.author, b.category, c.class_no, n.distance
FROM nearest n
JOIN v_books b USING (book_id)
LEFT JOIN book_classes c ON c.isbn13 = b.isbn13
ORDER BY n.distance, n.book_id
"""
_IN_CLASSES = "WHERE b.category = ANY($3::text[])"
_INDEX_DISTANCE = "e.embedding <=> $1::vector"
# 식을 바꿔 벡터 색인을 못 쓰게 한다. 분류 조건으로 책을 고른 뒤 거리를 전부 계산한다.
_EXACT_DISTANCE = "(e.embedding <=> $1::vector) + 0"
# 분류 안의 책 수는 세지 않고 실행 계획의 어림값을 받는다. 지금 홈 추천과 같은 이유다(personalized.py, #289)
_ESTIMATE_SQL = (
    "EXPLAIN (FORMAT JSON) SELECT 1 FROM v_books b WHERE b.category = ANY($1::text[])"
)


# 저자 칸을 이름 낱말로 가르는 구분. 표기가 "지은이: 한강", "한강 (지은이)", "J.K. 롤링 지음 ;강동혁 옮김"처럼
# 들쭉날쭉해서(app/routers/chat.py 의 판본 묶기와 같은 사정) 쉼표·쌍반점·콜론·괄호와 공백으로 가른다
_AUTHOR_SEPARATORS = r"[\s,;:()]+"
# 자리 번호: 작가 이름($1) → 읽을 권수($2). 글자 조각 색인(<%)으로 후보를 먼저 다 모은 뒤, 이름이 저자 칸의
# 낱말로 통째로 있는 책만 남긴다. 조각만 보면 "한강"에 한강준·한강수가, "김훈"에 김훈민이 섞인다. 후보를 먼저
# 모으지 않으면 PostgreSQL 이 출간연도 색인을 따라 한 권씩 견줘, 흔한 조각이 든 이름이 느렸다
# (평가 DB에서 J.K. 롤링 0.55초 → 0.015초)
_AUTHOR_SQL = f"""
WITH candidates AS MATERIALIZED (
    SELECT b.book_id, b.isbn13, b.title, b.author, b.category, b.pub_year
    FROM v_books b
    WHERE $1 <% b.author
)
SELECT book_id, isbn13, title, author, category
FROM candidates
WHERE strpos(' ' || regexp_replace(author, '{_AUTHOR_SEPARATORS}', ' ', 'g') || ' ',
             ' ' || $1 || ' ') > 0
ORDER BY pub_year DESC NULLS LAST, book_id
LIMIT $2
"""
# 판본을 거르기 전에 읽는 배수. 시리즈는 권마다 제목이 같아 판본으로 묶인다(평가 DB에서 J.K. 롤링 259권의
# 제목이 30개)
AUTHOR_READ_FACTOR = 5

# 자리 번호: 핵심어의 이름 벡터($1) → 뽑을 권수($2) → 소문자로 바꾼 핵심어($3). 가까운 책을 뽑은 뒤 제목이나
# 소개글에 그 말이 있는지 붙인다
_KEYWORD_SQL = """
WITH nearest AS (
    SELECT e.book_id, e.embedding <=> $1::vector AS distance
    FROM book_embeddings e
    ORDER BY distance
    LIMIT $2
)
SELECT n.book_id, b.isbn13, b.title, b.author, b.category, n.distance,
       strpos(lower(b.title), $3) > 0
           OR strpos(lower(coalesce(b.description, '')), $3) > 0 AS mentioned
FROM nearest n
JOIN v_books b USING (book_id)
ORDER BY n.distance, n.book_id
"""

# 자리 번호: 핵심어의 이름 벡터($1) → 소문자로 바꾼 핵심어($2) → 뽑을 권수($3). 제목에 그 말이 든 책을 글자
# 조각 색인(<%)으로 찾아 가까운 순으로 뽑는다. 거리 식을 바꿔(+ 0) 벡터 색인을 못 쓰게 한다. 벡터 색인을 따라가며
# 제목으로 거르면 거의 다 걸러져 몇 권 남지 않는다
_TITLE_SQL = """
SELECT e.book_id, b.isbn13, b.title, b.author, b.category,
       (e.embedding <=> $1::vector) + 0 AS distance
FROM v_books b
JOIN book_embeddings e USING (book_id)
WHERE $2 <% b.title AND strpos(lower(b.title), $2) > 0
ORDER BY distance, e.book_id
LIMIT $3
"""

# 자리 번호: 검색어($1) → 도서관 분류($2) → 뽑을 권수($3). 분류마다 개인화를 끈 목록과 같은 순서(판매 수 →
# 신간 → 번호)로 색인 앞부분만 읽어 모은 뒤, 책 벡터를 책 번호로 읽어 검색어와의 거리를 붙인다
_POPULAR_IN_CLASS = cold_start.popular_first_sql(
    "",
    "true",
    extra="AND b.category = w.category",
    limit="$3",
    columns="u.book_id, u.seg, u.sales, u.pub_year",
)
_POPULAR_SQL = f"""
WITH popular AS (
    SELECT c.book_id, c.seg, c.sales, c.pub_year
    FROM unnest($2::text[]) AS w(category)
    CROSS JOIN LATERAL (
        {_POPULAR_IN_CLASS}
        LIMIT $3
    ) c
    ORDER BY c.seg, c.sales DESC, c.pub_year DESC NULLS LAST, c.book_id
    LIMIT $3
)
SELECT p.book_id, b.isbn13, b.title, b.author, b.category,
       e.embedding <=> $1::vector AS distance
FROM popular p
JOIN v_books b USING (book_id)
LEFT JOIN book_embeddings e USING (book_id)
ORDER BY p.seg, p.sales DESC, p.pub_year DESC NULLS LAST, p.book_id
"""


@dataclass(frozen=True)
class Pick:
    """관심사에서 뽑은 책 한 권. part 는 매칭 점수의 유사도 항(0–1)이다(scoring.py)."""

    book_id: int
    isbn13: str | None
    title: str
    author: str | None
    category: str | None
    part: float


def _as_pick(row: Mapping[str, Any], part: float) -> Pick:
    return Pick(
        book_id=row["book_id"],
        isbn13=row["isbn13"],
        title=row["title"],
        author=row["author"],
        category=row["category"],
        part=part,
    )


def classes_of(name: str) -> tuple[str, ...] | None:
    """관심사를 뽑을 도서관 분류. 분류로 좁힐 수 없으면 None 이고, 그때는 벡터로만 뽑는다.

    카테고리는 그 카테고리의 분류다. 세부 카테고리는 지금의 세부 카테고리 → 도서관 분류 대응표를 쓰고,
    없으면 속한 카테고리의 분류를 쓴다(추리/스릴러는 소설의 분류 안에서). 어린이와 그 밑의 세부
    카테고리, 어느 카테고리에도 속하지 않는 세부 카테고리(의학 등)는 None 이다.
    """
    if name in categories.TAG_TO_CATALOG:
        return categories.TAG_TO_CATALOG[name] or None
    match = categories.ONBOARDING_TO_CATALOG.get(
        name if name in categories.ONBOARDING_TO_CATALOG else _PARENT.get(name)
    )
    return match.core if match else None


def numbered_first[T](
    name: str, items: Sequence[T], class_no: Callable[[T], str | None]
) -> list[T]:
    """세부 분류 번호가 이 관심사로 이어지는 책을 앞으로. 앞뒤 안의 순서는 그대로다."""
    return sorted(
        items, key=lambda item: categories.class_interest(class_no(item)) != name
    )


async def pick(
    conn: asyncpg.Connection,
    name: str,
    vector: Sequence[float],
    *,
    by_name: bool,
    limit: int,
    left: Callable[[], float],
) -> list[Pick]:
    """관심사 하나에서 검색어와 가까운 책 limit 권. 번호가 이 관심사로 이어지는 책을 앞에 둔다.

    vector 는 그 관심사의 책 벡터 평균(길이를 줄이지 않은 것)이거나 이름 벡터(by_name)다. 분류 안의 책이
    적으면 거리를 전부 계산하고, 많으면 벡터 색인으로 뽑는다. 고르는 기준은 지금 홈 추천과 같고
    (personalized.py), 색인을 훑는 폭만 PICK_EF_SEARCH 로 좁다. 질의마다 left() 초만 기다리고, 시간을 다
    쓰면 TimeoutError 다.
    """
    classes = classes_of(name)
    count = 0
    if classes:
        plan = await conn.fetchval(_ESTIMATE_SQL, list(classes), timeout=left())
        count = int(json.loads(plan)[0]["Plan"]["Plan Rows"])

    async def run(distance: str) -> list[asyncpg.Record]:
        # SET LOCAL 은 트랜잭션 안에서만 먹는다(훑는 폭 말고는 지금 홈 추천과 같은 설정)
        async with conn.transaction():
            await conn.execute(f"SET LOCAL hnsw.ef_search = {PICK_EF_SEARCH}")
            await conn.execute("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
            await conn.execute("SET LOCAL plan_cache_mode = force_custom_plan")
            if classes:
                await conn.execute(
                    "SET LOCAL hnsw.scan_mem_multiplier = "
                    f"{FILTERED_SCAN_MEM_MULTIPLIER}"
                )
                await conn.execute(
                    f"SET LOCAL hnsw.max_scan_tuples = {FILTERED_MAX_SCAN_TUPLES}"
                )
            return await conn.fetch(
                _SQL.format(distance=distance, where=_IN_CLASSES if classes else ""),
                to_vector_literal(list(vector)),
                limit,
                *([list(classes)] if classes else []),
                timeout=left(),
            )

    if classes and count <= EXACT_LIMIT:
        rows = await run(_EXACT_DISTANCE)
    else:
        rows = await run(_INDEX_DISTANCE)
        if classes and len(rows) < limit and count <= FALLBACK_LIMIT:
            rows = await run(_EXACT_DISTANCE)

    # 책 벡터가 길이 1 이라 코사인에 검색어의 길이를 곱하면 내적이다. 책 벡터 평균이면 그 책들과의 유사도 평균
    length = math.sqrt(sum(x * x for x in vector))
    return [
        _as_pick(
            row,
            scoring.interest_similarity_part(
                (1 - row["distance"]) * length, by_name=by_name
            ),
        )
        for row in numbered_first(name, rows, lambda row: row["class_no"])
    ]


def title_key(title: str | None) -> str:
    """띄어쓰기와 문장부호만 다른 제목을 같은 것으로 본다. 같은 책의 판본을 가를 때 쓴다."""
    return "".join(ch for ch in (title or "").casefold() if ch.isalnum())


async def pick_author(
    conn: asyncpg.Connection,
    name: str,
    *,
    limit: int,
    left: Callable[[], float],
) -> list[Pick]:
    """작가 관심사(작가:김영하)의 책 limit 권. 저자 칸에 그 이름이 통째로 든 책을 새 책부터 뽑는다.

    같은 제목의 판본은 하나만 둔다. 유사도 항은 모두 1 이다(작가 이름의 벡터는 책과 맞을 이유가 없음,
    #395). 질의는 left() 초만 기다린다.
    """
    author = " ".join(re.split(_AUTHOR_SEPARATORS, name.removeprefix(interests.AUTHOR)))
    author = author.strip()
    if not author:
        return []
    rows = await conn.fetch(
        _AUTHOR_SQL, author, limit * AUTHOR_READ_FACTOR, timeout=left()
    )
    part = scoring.interest_similarity_part(None, by_name=False, author_matched=True)
    seen: set[str] = set()
    picks: list[Pick] = []
    for row in rows:
        key = title_key(row["title"])
        if key in seen:
            continue
        seen.add(key)
        picks.append(_as_pick(row, part))
    return picks[:limit]


async def pick_keyword(
    conn: asyncpg.Connection,
    name: str,
    vector: Sequence[float],
    *,
    limit: int,
    left: Callable[[], float],
) -> list[Pick]:
    """키워드 관심사(키워드:고양이)의 책 limit 권. 핵심어의 이름 벡터와 가까운 책을 뽑고, 제목이나 소개글에
    그 말이 든 책을 앞에 둔다.

    #395 의 (안)은 그 말이 든 책을 카탈로그 전체에서도 찾는 것이었다. 소개글에는 색인이 없어 운영에서 전체를
    훑으면 40초가 넘어, 가까운 책 안에서만 가린다. 다만 드문 말(체스, 셜록 홈즈)은 가까운 책에 그 말이 든 책이
    적어서, 그 말이 든 책이 limit 권보다 적으면 제목 색인으로 더 찾는다(#480). 흔한 말은 가까운 책이 이미 모두
    그 말을 담고 있고, 제목 색인 조회가 오히려 비싸서 찾지 않는다. 질의는 left() 초만 기다린다.
    """
    keyword = name.removeprefix(interests.KEYWORD).strip().lower()
    literal = to_vector_literal(list(vector))
    async with conn.transaction():
        # 이 조회는 반복해 훑지 않아 훑는 폭보다 많은 책을 돌려주지 못한다. 뽑을 권수만큼은 훑는다
        await conn.execute(f"SET LOCAL hnsw.ef_search = {max(PICK_EF_SEARCH, limit)}")
        rows = await conn.fetch(_KEYWORD_SQL, literal, limit, keyword, timeout=left())
    mentioned = [row for row in rows if row["mentioned"]]
    others = [row for row in rows if not row["mentioned"]]
    if len(mentioned) < limit:
        seen = {row["book_id"] for row in mentioned}
        titled = await _titled(conn, literal, keyword, limit, left)
        mentioned = sorted(
            [*mentioned, *(row for row in titled if row["book_id"] not in seen)],
            key=lambda row: (row["distance"], row["book_id"]),
        )
    return [
        _as_pick(
            row, scoring.interest_similarity_part(1 - row["distance"], by_name=True)
        )
        for row in [*mentioned, *others][:limit]
    ]


async def _titled(
    conn: asyncpg.Connection,
    literal: str,
    keyword: str,
    limit: int,
    left: Callable[[], float],
) -> list[asyncpg.Record]:
    """제목에 그 말이 든 책(가까운 순). 시간 예산을 넘기면 빈 목록이고, 그때는 가까운 책만 쓴다.

    저장점으로 감싸, 시간을 넘긴 조회가 관심사를 뽑는 트랜잭션을 깨지 않게 한다.
    """
    try:
        async with conn.transaction():
            return await conn.fetch(_TITLE_SQL, literal, keyword, limit, timeout=left())
    except TimeoutError:
        return []


async def pick_popular(
    conn: asyncpg.Connection,
    name: str,
    vector: Sequence[float],
    *,
    by_name: bool,
    limit: int,
    timeout: float,
) -> list[Pick]:
    """시간 예산을 넘긴 관심사의 대신할 책 limit 권. 그 관심사의 도서관 분류에서 인기 있는 책을 뽑는다.

    지금 장애 때 쓰는 규칙 점수 목록과 같은 색인이라 몇 ms 다(#395). 인기도가 비어 있는 동안은 신간순이다.
    유사도 항은 책 벡터를 책 번호로 읽어 pick 과 같은 식으로 매기고, 벡터가 없는 책은 0 이다. 분류로 좁힐
    수 없는 관심사는 빈 목록이고, 그 자리는 다른 관심사가 채운다.
    """
    classes = classes_of(name)
    if not classes:
        return []
    rows = await cold_start.run_ordered(
        conn,
        _POPULAR_SQL,
        to_vector_literal(list(vector)),
        list(classes),
        limit,
        timeout=timeout,
    )
    length = math.sqrt(sum(x * x for x in vector))
    return [
        _as_pick(
            row,
            scoring.interest_similarity_part(
                None if row["distance"] is None else (1 - row["distance"]) * length,
                by_name=by_name,
            ),
        )
        for row in rows
    ]
