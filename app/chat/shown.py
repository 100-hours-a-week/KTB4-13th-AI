"""③ 마지막으로 보여 준 카드 — BE 가 싣는 ``context_cards`` 를 1단계와 대답 턴이 읽을 책 목록으로 바꾼다(#530).

1단계(조건 정리)는 그동안 어떤 책을 보여 줬는지 몰랐다. "2번 같은 거", "1번은 너무 어려워"를 풀 단서가 말풍선에 적힌
제목뿐이었다. BE 대화방 경로는 마지막으로 카드가 보인 묶음을 순번과 함께 보낸다(명세 ③ 입력 ⁸).

이 칸은 쇼핑 칸이라, 명세상 갈래가 ③이면 검사하지 않는다. 쇼핑 칸이 틀렸다는 이유로 추천이 실패하면 안 되기
때문이다. 그래서 모양이 틀린 항목은 400 으로 막지 않고 건너뛴다.
"""

import re
from dataclasses import dataclass
from typing import Any

from app.core import db

# 명세 ③ 입력: context_cards 는 최대 10개
MAX_SHOWN = 10
_ISBN13 = re.compile(r"^[0-9]{13}$")

_SQL = (
    "SELECT isbn13, book_id, title, author, description "
    "FROM v_books WHERE isbn13 = ANY($1::text[])"
)


@dataclass(frozen=True)
class ShownBook:
    rank: int
    book_id: int
    title: str
    author: str | None
    description: str | None


def parse(payload: dict[str, Any]) -> list[tuple[int, str]]:
    """``context_cards`` 에서 (순번, ISBN) 을 순번 차례로 뽑는다. 틀린 항목과 겹치는 순번·ISBN 은 건너뛴다."""
    cards = payload.get("context_cards")
    if not isinstance(cards, list):
        return []
    picked: dict[int, str] = {}
    seen: set[str] = set()
    for card in cards[:MAX_SHOWN]:
        if not isinstance(card, dict):
            continue
        rank, isbn = card.get("rank"), card.get("isbn")
        if not isinstance(rank, int) or isinstance(rank, bool) or rank < 1:
            continue
        if not isinstance(isbn, str) or not _ISBN13.match(isbn):
            continue
        if rank in picked or isbn in seen:
            continue
        picked[rank] = isbn
        seen.add(isbn)
    return sorted(picked.items())


async def fetch(cards: list[tuple[int, str]]) -> list[ShownBook]:
    """(순번, ISBN) 을 책 표에서 찾아 순번 차례로 돌려준다. 책 표에 없는 ISBN 은 빠진다."""
    if not cards:
        return []
    async with db.get_pool().acquire() as conn:
        rows = await conn.fetch(_SQL, [isbn for _, isbn in cards])
    by_isbn = {row["isbn13"]: row for row in rows}
    return [
        ShownBook(
            rank=rank,
            book_id=by_isbn[isbn]["book_id"],
            title=by_isbn[isbn]["title"],
            author=by_isbn[isbn]["author"],
            description=by_isbn[isbn]["description"],
        )
        for rank, isbn in cards
        if isbn in by_isbn
    ]


def listing(books: list[ShownBook], description_chars: int) -> str:
    """프롬프트에 넣을 줄 목록. "2번: 『제목』 - 저자 - 소개 앞부분". 없으면 "(없음)"."""
    if not books:
        return "(없음)"
    lines = []
    for book in books:
        about = " ".join((book.description or "").split())[:description_chars]
        line = f"{book.rank}번: 『{book.title}』"
        if book.author:
            line += f" - {book.author}"
        line += f" - {about}" if about else " - (소개 없음)"
        lines.append(line)
    return "\n".join(lines)
