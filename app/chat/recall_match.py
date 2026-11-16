"""떠올린 제목(B)을 책 표의 책에 붙인다(#737).

먼저 사진 표지 규칙(app/photo/match_cover.decide)을 그대로 쓴다. 그 규칙은 사진 속 책을 잘못 확정하지 않으려고 애매하면
후보로 넘겨 사람이 고르게 한다. 떠올린 제목에는 고를 사람이 없어서 후보로 넘기면 곧 못 찾은 것이 됐다. 11/12 평가 DB에서
서술형 40문항 첫 턴을 gpt-4o 로 돌리자 떠올린 제목 179개 중 44개만 붙었다. 놓친 진짜 책 중 아래 셋은 규칙으로 가를 수 있다.

1. 제목·저자가 다 맞는 책이 여러 묶음으로 갈라져 있다. 『어린 왕자』는 출판사마다, 『위대한 개츠비』는 첫 저자 표기
   ("F. 스콧 피츠제럴드"·"스콧 피츠제럴드")마다 판본 묶음이 갈린다. 로마자 제목(『1Q84』)은 원서와 번역서를 못 가른다며
   확정하지 않는다.
2. 이름 표기가 다르다. 애거사↔애거서 크리스티, 존 로널드 루엘 톨킨↔J.R.R. 톨킨.
3. 제목 꼴이 다르다. 책 표 제목 앞에 괄호 말이 붙었거나(『(그림으로 보는) 시간의 역사』), 모델이 제목 앞에 저자 이름을
   붙였다(『파올로 코엘료의 연금술사』).

셋 다 제목과 저자가 둘 다 책 표의 한 책과 맞아야 붙인다. 카드에 나오는 것은 늘 책 표의 책이라 모델이 지어낸 책 자체는 나올
수 없다. 막아야 할 것은 지어낸 제목이 이름이 같은 다른 진짜 책에 붙는 것이다(지어낸 『우주 탐험』 스티븐 호킹 ↔ 책 표의
『우주 탐험』 케이티 데이니스). 그래서 이름은 제목이 같을 때만, 여러 낱말 이름의 마지막 낱말이 같을 때만 느슨하게 본다. 한
낱말 이름(김영하·한강)은 지금처럼 통째로 같아야 한다 — 김영하와 김영아는 다른 사람이다.
"""

import re
from collections.abc import Sequence
from dataclasses import replace

import asyncpg

from app.core import editions
from app.photo import match_cover
from app.photo.catalog import Book
from app.photo.read import BookText

# 책 표 제목 앞의 괄호 말. 책장 규칙(app/photo/match_shelf.py)이 떼는 것과 같은 모양이다
_LEADING_PAREN = re.compile(r"^\s*\([^)]*\)\s*")
# 저자 칸 이름 안의 괄호 말("애거서 크리스티 (Agatha Christie)")
_PAREN = re.compile(r"\([^)]*\)")
_NON_WORD = re.compile(r"[\W_]+")
# 마지막 낱말로 같은 사람을 볼 때 그 낱말의 최소 글자 수. "B." 같은 머리글자나 "던" 한 글자로는 가르지 않는다
MIN_LAST_NAME_CHARS = 2
# 사람 이름이 아닌 마지막 낱말. 단체 이름은 마지막 낱말이 같아도 같은 곳이 아니다 — 11/12 실측에서 모델이 댄 "대교출판
# 편집부"가 같은 제목의 "키움북스 편집부" 책에 붙었다
_NOT_A_PERSON = re.compile(r"(편집부|편집팀|편집실|위원회|연구회|연구소|출판사|기획)$")


def _cover(title: str, author: str) -> BookText:
    return BookText(is_book_cover=True, title=title, authors=[author])


def _strict(text: BookText, books: Sequence[Book]) -> list[Book]:
    """표지 규칙이 한 묶음으로 확정한 책. 고른 판본이 맨 앞이고, 같은 묶음에서 저자가 맞는 다른 판본이 뒤에 온다."""
    found = match_cover.decide(text, books)
    if found.kind != "found" or not found.books[0].author_hit:
        return []
    picked = found.books[0].book
    others = [
        s.book
        for s in found.editions
        if s.author_hit and s.book.book_id != picked.book_id
    ]
    return [picked, *others]


def _title_hit(text: BookText, book: Book) -> bool:
    """제목이 같은가. 표지 규칙의 제목 판정(부제 떼기·C#·C++ 표시)에, 책 표 제목 앞 괄호 말을 뗀 꼴을 더한다."""
    if match_cover.score(text, book).title_hit:
        return True
    plain = _LEADING_PAREN.sub("", book.title).strip()
    return (
        bool(plain)
        and plain != book.title.strip()
        and (match_cover.score(text, replace(book, title=plain)).title_hit)
    )


def _last_word(name: str) -> str | None:
    """여러 낱말 사람 이름의 마지막 낱말. 한 낱말 이름이거나 단체 이름이면 None — 그런 이름은 통째로 같아야 같다."""
    words = _PAREN.sub(" ", editions.first_author(name)).split()
    if len(words) < 2:
        return None
    last = _NON_WORD.sub("", words[-1].casefold())
    if len(last) < MIN_LAST_NAME_CHARS or _NOT_A_PERSON.search(last):
        return None
    return last


def same_last_name(author: str, catalog_author: str | None) -> bool:
    """떠올린 저자와 책 표 저자 칸의 한 사람이 둘 다 여러 낱말 이름이고 마지막 낱말이 같은가."""
    mine = _last_word(author)
    if mine is None:
        return False
    people = re.split(r"[;,]", catalog_author or "")
    return any(_last_word(person) == mine for person in people)


def _edition_order(exact_author: bool, book: Book) -> tuple:
    """붙일 판본의 순서. 저자 이름이 그대로 맞는 것, 살 수 있는 것, 표지 그림이 있는 것, 새 판 순이다(표지 규칙과 같다)."""
    return (
        exact_author,
        book.in_stock,
        book.cover_url is not None,
        book.pub_year or 0,
        book.book_id,
    )


def _loose(text: BookText, books: Sequence[Book]) -> list[Book]:
    """제목이 같고 저자가 같은(또는 마지막 낱말이 같은) 책 전부. 묶음이 여럿이어도, 로마자 제목이어도 받는다."""
    author = text.authors[0]
    hits = []
    for book in books:
        if not _title_hit(text, book):
            continue
        exact = match_cover.score(text, book).author_hit
        if exact or same_last_name(author, book.author):
            hits.append((exact, book))
    hits.sort(key=lambda hit: _edition_order(*hit), reverse=True)
    return [book for _, book in hits]


def decide(title: str, author: str, books: Sequence[Book]) -> list[Book]:
    """검색 후보 중 떠올린 제목·저자에 붙일 책. 고른 판본이 맨 앞이다. 못 찾으면 빈 목록. DB 를 읽지 않는다."""
    text = _cover(title, author)
    return _strict(text, books) or _loose(text, books)


def title_without_author(title: str, author: str) -> str | None:
    """모델이 제목 앞에 저자 이름을 붙였으면("파올로 코엘료의 연금술사") 뗀 제목. 아니면 None."""
    for name in {author.strip(), author.replace(" ", "")}:
        head = f"{name}의"
        rest = title[len(head) :].strip() if title.startswith(head) else ""
        if name and rest:
            return rest
    return None


async def match(conn: asyncpg.Connection, title: str, author: str) -> list[Book]:
    """떠올린 제목·저자로 책 표의 책을 찾는다. 고른 판본이 맨 앞이고 같은 책의 다른 판본이 뒤에 온다.

    제목 그대로 먼저 찾는다. 『클라우스 슈밥의 제4차 산업혁명』처럼 저자 이름이 든 진짜 제목이 있어서다. 그걸로 못 찾았고
    제목이 저자 이름으로 시작하면 떼고 한 번 더 찾는다.
    """
    found = decide(title, author, await match_cover.search(conn, title))
    if found:
        return found
    shorter = title_without_author(title, author)
    if shorter is None:
        return []
    return decide(shorter, author, await match_cover.search(conn, shorter))
