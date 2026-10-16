"""④ 홈과 더 보기 — 저장된 관심사별 목록에서 한 쪽을 읽는다(#454, #455, 설계 #395). 요청 때 벡터를 조회하지 않는다.

⑥ 이 프로필과 함께 뽑아 둔 200권(app/feed/interest_feed.py)에서, 지금 목록과 같은 제외 조건으로 가진 책을
빼고, 요청에 가진 책이 실려 왔으면 그 다른 판본도 뺀 뒤 커서 위치부터 읽는다. 더 보기는 지금 목록과 같은
뜻으로 거르고 정렬대로 다시 줄 세운다. 스크롤하는 중에 목록이 다시 뽑혔으면 커서에 실은 판 번호로 앞 판을
이어 읽는다.
"""

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.core import categories, editions, products
from app.feed import exclusion
from app.feed.cursor import Page
from app.feed.schemas import FeedRequest

_STORED_SQL = """
SELECT feed::text AS feed, feed_version, feed_prev::text AS feed_prev
FROM taste_profile
WHERE user_id = $1
"""

# 자리 번호: 목록의 책 번호($1) → 뺄 책의 값들. 책 정보는 지금 목록과 같은 칸을 읽는다
_ROWS_SQL = f"""
SELECT b.book_id, b.isbn13 AS isbn, b.title, b.author,
       {products.price_sql("b")} AS price, b.cover_url,
       {products.in_stock_sql("b")} AS in_stock, b.category, b.pub_year
FROM v_books b
WHERE b.book_id = ANY($1::int[]) AND {{excluded}}
"""
# 응답에 나가는 칸(명세 ④)
_RESPONSE = (
    "book_id",
    "isbn",
    "title",
    "author",
    "price",
    "cover_url",
    "in_stock",
    "match_score",
)
# 더 보기의 정렬. 같으면 저장된 순서다. 가격이 없는 책은 가격순에서 맨 뒤다(지금 목록과 같음)
_ORDER = {
    "match": lambda item: -item["match_score"],
    "newest": lambda item: -(item["pub_year"] or 0),
    "price_asc": lambda item: (item["price"] is None, item["price"] or 0),
}
_OWNED_SQL = "SELECT title, author FROM v_books WHERE isbn13 = ANY($1::text[])"


@dataclass(frozen=True)
class Stored:
    """저장된 목록. books 는 지금 판, prev 는 바로 앞 판이다(책마다 book_id·interest·match_score)."""

    books: list[dict[str, Any]]
    version: int
    prev: list[dict[str, Any]] | None

    def at(self, version: int | None) -> tuple[list[dict[str, Any]], int]:
        """커서가 읽던 판. 그새 한 번 다시 뽑혔으면 앞 판이다.

        두 번 넘게 다시 뽑혔으면 지금 판으로 이어 읽는다. 프로필이 바뀌어도 끊지 않는 지금 목록과 같다
        (app/feed/cursor.py).
        """
        if (
            version is not None
            and version == self.version - 1
            and self.prev is not None
        ):
            return self.prev, version
        return self.books, self.version


async def stored(conn: asyncpg.Connection, user_id: int) -> Stored | None:
    """저장된 목록. 아직 뽑지 않았으면 None 이다."""
    row = await conn.fetchrow(_STORED_SQL, user_id)
    if row is None or row["feed"] is None:
        return None
    prev = json.loads(row["feed_prev"]) if row["feed_prev"] else None
    return Stored(
        books=json.loads(row["feed"])["books"],
        version=row["feed_version"],
        prev=prev["books"] if prev else None,
    )


async def books(
    conn: asyncpg.Connection,
    req: FeedRequest,
    saved: list[dict[str, Any]],
    excluded: exclusion.Exclusion,
) -> list[dict[str, Any]]:
    """읽을 책 전부. 가진 책과 그 판본을 빼고, 더 보기면 걸러 정렬대로 줄 세운다. 매칭 점수는 저장된 값이다."""
    rows = await conn.fetch(
        _ROWS_SQL.format(excluded=excluded.sql(2)),
        [book["book_id"] for book in saved],
        *excluded.params,
    )
    by_id = {row["book_id"]: row for row in rows}
    owned = await _owned_editions(conn, req.owned_isbns() or [])
    kept = []
    for book in saved:
        row = by_id.get(book["book_id"])
        if row is None or editions.edition_key(row["title"], row["author"]) in owned:
            continue
        kept.append({**dict(row), "match_score": book["match_score"]})
    return arrange(req, kept)


def arrange(req: FeedRequest, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """더 보기면 카테고리·출간연도·점수 하한으로 거르고 정렬대로 줄 세운다. 홈은 저장된 순서 그대로다."""
    if req.surface != "recommend_more":
        return items
    return sorted((item for item in items if _passes(req, item)), key=_ORDER[req.sort])


def _passes(req: FeedRequest, item: dict[str, Any]) -> bool:
    """지금 목록의 거르기와 같은 뜻이다(app/search/filters.py). 출간연도를 모르는 책은 연도로 거르면 빠진다."""
    if req.category is not None:
        allowed = categories.filter_categories(req.category)
        if allowed is None:
            allowed = (req.category,)
        # 빈 튜플은 어린이처럼 분류로 가를 수 없는 값이라 거르지 않는다
        if allowed and item["category"] not in allowed:
            return False
    year = item["pub_year"]
    if req.pub_year_from is not None and (year is None or year < req.pub_year_from):
        return False
    if req.pub_year_to is not None and (year is None or year > req.pub_year_to):
        return False
    return req.match_score_min is None or item["match_score"] >= req.match_score_min


def page_of(
    items: list[dict[str, Any]], req: FeedRequest, page: Page
) -> tuple[list[dict[str, Any]], bool]:
    """(이번 쪽, 다음 쪽이 있는지). 응답에 나가는 칸만 남긴다."""
    window = items[page.offset : page.offset + req.size]
    shown = [{key: item[key] for key in _RESPONSE} for item in window]
    return shown, len(items) > page.offset + req.size


async def _owned_editions(
    conn: asyncpg.Connection, isbns: list[str]
) -> set[tuple[str, str]]:
    """요청에 실려 온 가진 책들의 판본 키. 그 책의 다른 판본도 뺀다."""
    if not isbns:
        return set()
    rows = await conn.fetch(_OWNED_SQL, isbns)
    keys = (editions.edition_key(row["title"], row["author"]) for row in rows)
    return {key for key in keys if key}
