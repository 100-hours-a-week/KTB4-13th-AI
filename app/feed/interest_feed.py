"""④ 관심사별 홈 추천 목록 뽑기 — ⑥ 이 프로필과 함께 저장할 200권을 만든다(#452, 설계 #395).

관심사 점수로 자리를 나누고, 관심사마다 검색어를 정해 시간 예산 안에서 뽑은 뒤(interest_fetch.py), 가진 책과
그 판본, 음수인 관심사의 책을 거르고 조건을 지키는 책을 앞에 두어 목록을 조립한다(interest_list.py).
"""

import asyncio
import json
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.core import categories, editions, products
from app.feed import interest_fetch, interest_list, interest_picks
from app.feed.interest_fetch import Job
from app.feed.interest_list import Placed
from app.feed.interest_picks import Pick
from app.profile import interests, labels

# 관심사마다 받은 자리의 두 배를 뽑는다. 가진 책·판본·다른 관심사와 겹친 책을 빼도 자리가 비지 않게 한다.
# 자리가 적은 관심사도 PICK_MIN 권은 뽑는다
PICK_FACTOR = 2
PICK_MIN = 20

# 서버 안에서 목록 뽑기는 한 번에 하나만 한다. 운영에서 10명을 동시에 뽑으니 한 사람이 12초까지 늘었다(#399).
# 서버는 프로세스 하나로 돈다(Dockerfile 의 uvicorn)
_BUILDING = asyncio.Lock()

# 세부 카테고리 → 속한 카테고리
_PARENT = {
    name: parent for parent, group in categories.SUBCATEGORIES.items() for name in group
}

# 뽑은 책의 조건 값(출간연도·가격·쪽수)과, 관심사를 가를 값(세부 분류 번호, 벡터). 벡터는 음수인 관심사가 있을
# 때만 읽는다
_FACTS_SQL = f"""
SELECT b.book_id, b.pub_year, {products.price_sql("b")} AS price, c.page_count, c.class_no,
       {{vector}} AS vector
FROM v_books b
LEFT JOIN book_classes c ON c.isbn13 = b.isbn13
{{join}}
WHERE b.book_id = ANY($1::int[])
"""
_WITH_VECTORS = {
    "vector": "e.embedding::text",
    "join": "LEFT JOIN book_embeddings e ON e.book_id = b.book_id",
}
_WITHOUT_VECTORS = {"vector": "NULL::text", "join": ""}
_OWNED_SQL = "SELECT title, author FROM v_books WHERE book_id = ANY($1::int[])"


@dataclass(frozen=True)
class Feed:
    """⑥ 이 저장할 목록. substituted 는 시간 예산을 넘겨 인기 있는 책으로 대신한 관심사다(밤에 다시 뽑음)."""

    books: list[Placed]
    substituted: tuple[str, ...]

    def stored(self) -> dict[str, Any]:
        """프로필에 저장하는 모양(taste_profile.feed). 대신했지만 채울 책이 없던 관심사도 남겨 밤에 다시 뽑는다."""
        return {
            "books": [
                {
                    "book_id": b.book_id,
                    "interest": b.interest,
                    "match_score": b.match_score,
                }
                for b in self.books
            ],
            "substituted": list(self.substituted),
        }


def jobs(
    seats: Mapping[str, int],
    book_vectors: Mapping[str, Sequence[float]],
    keyword_vectors: Mapping[str, Sequence[float]],
    name_vector: Callable[[str], Sequence[float] | None],
) -> list[Job]:
    """관심사마다 뽑을 일. 자리를 많이 받은 관심사부터다.

    검색어는 그 관심사의 책 벡터 평균이고, 없으면 이름 벡터다. 키워드 관심사는 핵심어의 이름 벡터다.
    """
    out = []
    for name, count in sorted(seats.items(), key=lambda item: -item[1]):
        limit = max(count * PICK_FACTOR, PICK_MIN)
        if name.startswith(interests.AUTHOR):
            out.append(Job(name, limit))
        elif name.startswith(interests.KEYWORD):
            out.append(Job(name, limit, keyword_vectors.get(name), by_name=True))
        elif name in book_vectors:
            out.append(Job(name, limit, book_vectors[name]))
        else:
            out.append(Job(name, limit, name_vector(name), by_name=True))
    return out


async def build(
    conn: asyncpg.Connection,
    scores: Mapping[str, float],
    book_vectors: Mapping[str, Sequence[float]],
    keyword_vectors: Mapping[str, Sequence[float]],
    conditions: Sequence[str],
    owned_book_ids: Collection[int],
    *,
    name_vector: Callable[[str], Sequence[float] | None] = labels.name_vector,
    total: int = interest_list.TOTAL_SEATS,
) -> Feed:
    """관심사 점수로 홈 추천 목록을 뽑는다. 서버 안에서 한 번에 하나만 뽑고, 뒤에 온 것은 기다린다.

    scores 는 관심사마다 합친 점수, book_vectors 는 관심사마다 책 벡터 평균, keyword_vectors 는 키워드 관심사마다
    핵심어의 이름 벡터(그 취향 기억 줄의 벡터), conditions 는 프로필의 조건 목록이다. owned_book_ids 는 지금
    목록에서 빼는 책과 같다(산 책, 나의 도서관 책, 2점 이하 리뷰를 단 책. ⑥ 이 읽은 이력의 책).
    """
    async with _BUILDING:
        return await _build(
            conn,
            scores,
            book_vectors,
            keyword_vectors,
            conditions,
            owned_book_ids,
            name_vector,
            total,
        )


async def _build(
    conn: asyncpg.Connection,
    scores: Mapping[str, float],
    book_vectors: Mapping[str, Sequence[float]],
    keyword_vectors: Mapping[str, Sequence[float]],
    conditions: Sequence[str],
    owned_book_ids: Collection[int],
    name_vector: Callable[[str], Sequence[float] | None],
    total: int,
) -> Feed:
    seats = interest_list.allocate(scores, total)
    fetched = await interest_fetch.fetch(
        conn, jobs(seats, book_vectors, keyword_vectors, name_vector)
    )
    negatives = {name for name, score in scores.items() if score < 0}
    picked = sorted({pick.book_id for got in fetched.picks.values() for pick in got})
    facts = await _facts(conn, picked, with_vectors=bool(negatives))
    owned = await _owned_editions(conn, owned_book_ids)
    conds = [c for c in map(interest_list.parse_condition, conditions) if c]
    books = interest_list.assemble(
        fetched.picks,
        seats,
        keep=_keeper(set(owned_book_ids), owned, negatives, facts, name_vector),
        meets=lambda pick: interest_list.meets_all(conds, facts.get(pick.book_id, {})),
        total=total,
    )
    return Feed(books, fetched.substituted)


async def _facts(
    conn: asyncpg.Connection, book_ids: list[int], *, with_vectors: bool
) -> dict[int, dict[str, Any]]:
    """책마다 조건 값(pages·price·year)과 세부 분류 번호, 벡터(with_vectors 일 때만)."""
    if not book_ids:
        return {}
    sql = _FACTS_SQL.format(**(_WITH_VECTORS if with_vectors else _WITHOUT_VECTORS))
    rows = await conn.fetch(sql, book_ids)
    return {
        row["book_id"]: {
            "pages": row["page_count"],
            "price": row["price"],
            "year": row["pub_year"],
            "class_no": row["class_no"],
            "vector": json.loads(row["vector"]) if row["vector"] else None,
        }
        for row in rows
    }


async def _owned_editions(
    conn: asyncpg.Connection, book_ids: Collection[int]
) -> set[tuple[str, str]]:
    """가진 책들의 판본 키. 그 책의 다른 판본도 목록에서 뺀다."""
    if not book_ids:
        return set()
    rows = await conn.fetch(_OWNED_SQL, sorted(book_ids))
    keys = (editions.edition_key(row["title"], row["author"]) for row in rows)
    return {key for key in keys if key}


def _keeper(
    owned_book_ids: set[int],
    owned_editions: set[tuple[str, str]],
    negatives: set[str],
    facts: Mapping[int, Mapping[str, Any]],
    name_vector: Callable[[str], Sequence[float] | None],
) -> Callable[[Pick], bool]:
    """목록에 둘 책인지. 가진 책과 그 판본, 음수인 관심사의 책을 뺀다.

    음수인 관심사의 책은 산 책을 관심사로 가를 때와 같이 가른다(interests.book_interest). 카테고리가 음수면 그
    밑의 세부 카테고리와 그 카테고리의 도서관 분류에 드는 책도 빼고(#395 의 (안)), 작가가 음수면 그 작가의
    책을 뺀다. 키워드와 분위기는 책으로 가를 수 없어 빼지 않는다.
    """
    avoided_classes = {
        cls
        for name in negatives
        if name in categories.ONBOARDING_TO_CATALOG
        for cls in categories.ONBOARDING_TO_CATALOG[name].core
    }
    avoided_authors = [
        name.removeprefix(interests.AUTHOR)
        for name in negatives
        if name.startswith(interests.AUTHOR)
    ]

    def keep(pick: Pick) -> bool:
        if pick.book_id in owned_book_ids:
            return False
        if editions.edition_key(pick.title, pick.author) in owned_editions:
            return False
        if not negatives:
            return True
        if pick.category in avoided_classes:
            return False
        if any(interest_picks.has_author(pick.author, a) for a in avoided_authors):
            return False
        fact = facts.get(pick.book_id, {})
        interest = interests.book_interest(
            fact.get("class_no"), pick.category, fact.get("vector"), name_vector
        )
        return (
            interest not in negatives and _PARENT.get(interest or "") not in negatives
        )

    return keep
