"""구매·나의 도서관·리뷰 이력의 가중치 — 이력을 읽는 모든 곳이 같은 표를 쓰게 한 곳에 둔다.

⑥ 취향 프로필이 이력 전체로 계산하고, ③④ 는 프로필이 반영한 뒤(computed_at 이후)에 생긴 이력을
채점 때 "같은 가중치 표"로 더한다(명세 ⑥). 표가 갈라지면 프로필과 가산이 서로 다른 기준이 된다.

| 이력 | 가중치 |
| 구매 | 3 |
| 리뷰 4.0점 이상 | 2 |
| 나의 도서관 담기 | 1 |
| 리뷰 2.5점 이상 3.5점 이하 | 0 (쓰지 않음) |
| 리뷰 2.0점 이하 | −2 (취향 벡터에는 넣지 않고 카테고리 점수만 깎음) |

별점 경계는 우리 해석이다. 명세는 "4–5점 / 3점 / 1–2점" 으로 정수만 적었지만 실제 별점은 0.5 단위라
3.5점·2.5점이 비어 있어, 001 마이그레이션 v_user_reviews 주석대로 둘 다 중립으로 본다(이슈 #91).
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg

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
    # 이번에 읽은 이력 행들의 시각 중 최댓값. 이력이 없으면 None.
    # 계산 시각이 아니다 — 계산 시각으로 두면 복제가 늦게 도착한 이력이 프로필에도 ③④ 의 가산에도
    # 빠져 영구히 누락된다(명세 ⑥). 가중치 0 인 행도 넣는다. 빼면 ③④ 가 그 행을 "나중 이력"으로
    # 보고 다시 읽는데, 같은 책의 다른 행이 이미 반영돼 있으면 같은 몫을 두 번 더하게 된다.
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


_SQL = """
SELECT h.book_id, h.kind, h.rating, h.at, b.category
FROM (
    SELECT book_id, 'purchase' AS kind, NULL::numeric AS rating, purchased_at AS at
    FROM v_user_purchases WHERE user_id = $1
    UNION ALL
    SELECT book_id, 'library', NULL, added_at FROM v_user_library WHERE user_id = $1
    UNION ALL
    SELECT book_id, 'review', rating, created_at FROM v_user_reviews WHERE user_id = $1
) h
LEFT JOIN v_books b USING (book_id)
"""


async def read(conn: asyncpg.Connection, user_id: int) -> History:
    """user_id 의 복제된 이력 3종을 읽어 모은다."""
    rows = await conn.fetch(_SQL, user_id)
    return summarize([dict(r) for r in rows])
