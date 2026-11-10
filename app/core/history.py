"""구매·나의 도서관·리뷰 이력의 가중치 — 이력을 읽는 모든 곳이 같은 표를 쓰게 한 곳에 둔다.

⑥ 취향 프로필이 BE 가 요청에 실어 보낸 이력 전체로 계산한다(#327).

| 이력 | 가중치 |
| 구매 | 3 |
| 리뷰 4.0점 이상 | 2 |
| 나의 도서관 담기 | 1 |
| 리뷰 2.5점 이상 3.5점 이하 | 0 (쓰지 않음) |
| 리뷰 2.0점 이하 | −2 (취향 벡터에는 넣지 않고 카테고리 점수만 깎음) |

복제한 사용자 표에서 읽던 길(read)은 복제를 멈추며 없앴다(#307). 요청의 목록을 행 모양으로 바꿔
summarize 에 넣는다.

별점 경계는 우리 해석이다. 명세는 "4–5점 / 3점 / 1–2점" 으로 정수만 적었지만 실제 별점은 0.5 단위라
3.5점·2.5점이 비어 있어, 001 마이그레이션의 리뷰 표 주석대로 둘 다 중립으로 본다(이슈 #91).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

from app.core import isbn

PURCHASE = 3
LIKED_REVIEW = 2
LIBRARY = 1
DISLIKED_REVIEW = -2

LIKED_MIN_RATING = Decimal("4.0")
DISLIKED_MAX_RATING = Decimal("2.0")


def review_weight(rating: Decimal) -> int:
    if rating >= LIKED_MIN_RATING:
        return LIKED_REVIEW
    if rating <= DISLIKED_MAX_RATING:
        return DISLIKED_REVIEW
    return 0


@dataclass
class History:
    # 책마다 가중치 하나. 여러 테이블·여러 행에 있어도 가장 큰 값 하나만 쓴다(명세: 더하지 않는다).
    weights: dict[int, int] = field(default_factory=dict)
    # 카테고리마다 그 카테고리 책들의 가중치 합. −2 도 여기서는 반영한다.
    category_scores: dict[str, int] = field(default_factory=dict)
    # 2.0점 이하 리뷰를 단 책. weights 와 따로 둔다 — 명세 ⑥ 각주는 이 책을 취향 벡터 재료에서 빼고
    # 추천에서도 제외하라는데, weights 는 큰 값 하나만 남겨 사고(3) 1점을 준 책이 3 으로 남는다.
    # 이 경우에도 싫다는 신호가 이긴다고 보고, 취향 벡터를 만드는 쪽(#92)이 이 목록으로 뺀다.
    disliked_book_ids: set[int] = field(default_factory=set)
    # 프로필이 반영한 이력의 시각. 요청으로 받으면 받은 시각이다(from_request). 이력이 없으면 None.
    computed_at: datetime | None = None

    def any(self) -> bool:
        return self.computed_at is not None


def summarize(rows: list[dict[str, Any]]) -> History:
    """이력 행(book_id, kind, rating, at, category)을 책별 가중치로 모은다. DB 없이 돈다."""
    history = History()
    categories: dict[int, str | None] = {}
    for row in rows:
        if row["kind"] == "purchase":
            weight = PURCHASE
        elif row["kind"] == "library":
            weight = LIBRARY
        else:
            weight = review_weight(row["rating"])
        book_id = row["book_id"]
        if weight == DISLIKED_REVIEW:
            history.disliked_book_ids.add(book_id)
        if book_id not in history.weights or weight > history.weights[book_id]:
            history.weights[book_id] = weight
        categories[book_id] = row["category"]
        if history.computed_at is None or row["at"] > history.computed_at:
            history.computed_at = row["at"]

    for book_id, weight in history.weights.items():
        # 카탈로그에 없거나 카테고리가 비어 있는 책은 카테고리 점수에서만 빠진다.
        category = categories[book_id]
        if weight and category:
            history.category_scores[category] = (
                history.category_scores.get(category, 0) + weight
            )
    return history


def reported_rows(
    book_ids: Mapping[str, int],
    categories: Mapping[int, str | None],
    purchased_isbns: Sequence[str],
    library_isbns: Sequence[str],
    reviews: Sequence[tuple[str, float]],
    at: datetime,
) -> list[dict[str, Any]]:
    """요청으로 받은 이력을 복제 표에서 읽은 행과 같은 모양으로 만든다. DB 없이 돈다.

    reviews 는 (ISBN, 별점) 이다. book_ids 에 없는 ISBN(책 표에 없는 책)의 이력은 빠진다.
    """
    reported = [
        *((i, "purchase", None) for i in purchased_isbns),
        *((i, "library", None) for i in library_isbns),
        # 별점 경계(4.0, 2.0)를 복제 표의 numeric 과 같은 타입으로 견준다.
        *((i, "review", Decimal(str(rating))) for i, rating in reviews),
    ]
    return [
        {
            "book_id": book_ids[i],
            "kind": kind,
            "rating": rating,
            "at": at,
            "category": categories.get(book_ids[i]),
        }
        for i, kind, rating in reported
        if i in book_ids
    ]


_CATEGORY_SQL = "SELECT book_id, category FROM v_books WHERE book_id = ANY($1::int[])"


async def from_request(
    conn: asyncpg.Connection,
    *,
    purchased_isbns: Sequence[str],
    library_isbns: Sequence[str],
    reviews: Sequence[tuple[str, float]],
    at: datetime,
) -> History:
    """BE 가 요청에 실어 보낸 이력을 모은다(#327).

    at 은 요청을 받은 시각이다. 요청에는 이력마다의 시각이 없고 BE 가 그때까지의 전체를 보내므로,
    이력이 비어 있어도 computed_at 을 at 으로 둔다.
    """
    isbns = [*purchased_isbns, *library_isbns, *(i for i, _ in reviews)]
    book_ids = await isbn.book_id_map(conn, isbns)
    categories: dict[int, str | None] = {}
    if book_ids:
        rows = await conn.fetch(_CATEGORY_SQL, list(book_ids.values()))
        categories = {r["book_id"]: r["category"] for r in rows}
    history = summarize(
        reported_rows(book_ids, categories, purchased_isbns, library_isbns, reviews, at)
    )
    history.computed_at = at
    return history
