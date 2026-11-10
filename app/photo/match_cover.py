"""표지에서 읽은 글자로 카탈로그의 책을 찾는다(#572, #615).

글자만으로는 한 권을 못 고르는 경우가 많다. 추천 대상 46.8만 권 중 같은 제목 책이 더 있는 책이 40.1%, 제목과
저자까지 같은 책이 30.8%다(11/02 평가 DB). 그래서 순서를 둔다.

1. ISBN 이 보이고 검증 숫자가 맞으면 그 책이다. 판본까지 정확하다.
2. 아니면 제목으로 ① 키워드 검색을 하고, 후보마다 제목·저자·출판사가 표지와 같은지 본다.
3. 같은 책의 다른 판본은 한 묶음으로 본다(app/core/editions.py). 묶음이 하나로 좁혀지면 찾은 것이고, 그 안에서
   판본을 고른다. 여럿이면 확정하지 않고 후보(candidates)로 남긴다. 나의 도서관은 후보를 보여 주지 않아
   scan 이 못 찾음으로 바꾼다.

틀린 책을 확정하는 것이 가장 나쁘다. 사용자는 확정된 책을 그대로 믿고 담는다. 그래서 애매하면 확정하지 않는다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal

import asyncpg

from app.core import editions
from app.photo import catalog, volume
from app.photo.catalog import Book
from app.photo.read import BookText
from app.search import keyword
from app.search.schemas import SearchFilters

# 표지 제목으로 찾을 후보 수. 『어린 왕자』처럼 판본이 수백 개인 책도 있어 넉넉히 받는다.
SEARCH_LIMIT = keyword.CANDIDATE_LIMIT
MAX_CANDIDATES = 3
# 표지와 같은 칸의 점수. 후보의 순서에 쓴다. 제목이 같아야 후보가 되므로 최저 60이다.
TITLE_POINTS = 60
AUTHOR_POINTS = 30
PUBLISHER_POINTS = 10
# 저자 이름 끝이 같으면 같은 사람으로 본다("앙투안 드 생텍쥐페리"와 "생텍쥐페리"). 두 글자 이름은 우연히 끝이
# 같은 다른 사람이 많아 완전히 같을 때만 같다고 본다.
MIN_AUTHOR_SUFFIX = 3
# 출판사는 "(주)현암사"와 "현암사"처럼 한쪽이 다른 쪽에 들어 있으면 같다. 한 글자는 우연히 들어가기 쉽다.
MIN_PUBLISHER_CHARS = 2
# 카탈로그 제목에서 부제를 떼는 자리
_SUBTITLE_SEPARATORS = (":", " - ", "=")
# 제목이 거의 같다고 보는 선. 띄어쓰기·문장부호를 뺀 제목 키끼리 견준다. 짧은 제목은 한 글자 차이가 크게 갈라
# 다른 책이 되기 쉬워("마음"↔"마을") 길이 아래로는 보지 않는다
NEAR_TITLE_RATIO = 0.9
MIN_NEAR_TITLE_CHARS = 8
_LATIN = re.compile(r"[A-Za-z]")
_HANGUL = re.compile(r"[가-힣]")


@dataclass(frozen=True)
class Scored:
    """후보 한 권과, 표지와 같은 칸."""

    book: Book
    title_hit: bool
    author_hit: bool
    publisher_hit: bool
    # 저자 이름이 얼마나 비슷한가(0–1). 확정에는 쓰지 않고 후보의 순서에만 쓴다. 작은 글씨를 한 글자 틀리게
    # 읽은 이름("앤 드루안"↔"앤 드루얀")이 정답을 후보 앞으로 올리게 한다. 확정에 쓰면 "김영하"↔"김영아"처럼
    # 한 글자 다른 다른 사람을 같은 사람으로 본다.
    author_closeness: float = 0.0
    # 제목이 같지는 않지만 거의 같다(한두 글자 틀리게 읽음: "자바 코드의"↔"자바 코드를"). 확정에는 쓰지 않고, 제목이
    # 같은 책이 하나도 없을 때 후보로만 남긴다. 확정에 쓰면 "…방법"과 "…방법 2"처럼 한 글자 다른 다른 책을 고른다
    title_near: bool = False

    @property
    def score(self) -> int:
        return (
            TITLE_POINTS * self.title_hit
            + AUTHOR_POINTS * self.author_hit
            + PUBLISHER_POINTS * self.publisher_hit
        )


@dataclass(frozen=True)
class Match:
    """찾은 결과. found 면 books 가 한 권, candidates 면 서로 다른 책 최대 3권, none 이면 비었다."""

    kind: Literal["found", "candidates", "none"]
    books: list[Scored]
    # 찾은 길. 평가와 로그에서 ISBN 으로 찾은 것과 글자로 찾은 것을 가른다
    via: Literal["isbn", "text"] | None = None


NOTHING = Match("none", [])


def _cover_title_keys(text: BookText) -> set[str]:
    """표지 제목(부제를 붙인 것도)과 권 번호를 합친 키들. 카탈로그 제목 키와 같으면 제목이 같다."""
    titles = [text.title]
    if text.subtitle:
        titles.append(f"{text.title} {text.subtitle}")
    volumes = volume.keys(text.volume)
    keys: set[str] = set()
    for title in titles:
        key = editions.title_key(title)
        if not key:
            continue
        if not volumes:
            keys.add(key)
            continue
        for v in volumes:
            keys.update({key + v, key + v + "권"})
        if volumes & volume.FIRST_VOLUMES:
            keys.add(key)
    return keys


def _catalog_title_keys(title: str) -> set[str]:
    """카탈로그 제목의 키. 제목에 부제가 붙어 있으면 부제를 뗀 앞부분도 본다."""
    keys = {editions.title_key(title)}
    for separator in _SUBTITLE_SEPARATORS:
        if separator in title:
            keys.add(editions.title_key(title.split(separator, 1)[0]))
    keys.discard("")
    return keys


def _author_keys(author: str | None) -> set[str]:
    """저자 칸의 사람마다 키. "지음"·"옮김" 같은 역할 말을 뗀다(editions.author_key 와 같은 규칙)."""
    parts = (author or "").replace(";", ",").split(",")
    return {key for part in parts if (key := editions.author_key(part))}


def _same_person(a: str, b: str) -> bool:
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    return len(shorter) >= MIN_AUTHOR_SUFFIX and longer.endswith(shorter)


def _author_pairs(
    cover_authors: Sequence[str], catalog_author: str | None
) -> list[tuple[str, str]]:
    catalog_keys = _author_keys(catalog_author)
    cover = {key for name in cover_authors if (key := editions.author_key(name))}
    return [(a, b) for a in cover for b in catalog_keys]


def _author_hit(cover_authors: Sequence[str], catalog_author: str | None) -> bool:
    return any(
        _same_person(a, b) for a, b in _author_pairs(cover_authors, catalog_author)
    )


def _author_closeness(
    cover_authors: Sequence[str], catalog_author: str | None
) -> float:
    pairs = _author_pairs(cover_authors, catalog_author)
    return max((SequenceMatcher(None, a, b).ratio() for a, b in pairs), default=0.0)


def _foreign_title(title: str) -> bool:
    """로마자가 있고 한글이 없는 제목. 한국 번역서도 표지에는 원서 제목만 크게 쓴 것이 많다(예: DEMIAN)."""
    return bool(_LATIN.search(title)) and not _HANGUL.search(title)


def _publisher_hit(cover: str | None, catalog_publisher: str | None) -> bool:
    a, b = editions.title_key(cover), editions.title_key(catalog_publisher)
    if len(a) < MIN_PUBLISHER_CHARS or len(b) < MIN_PUBLISHER_CHARS:
        return False
    return a in b or b in a


def _near(cover_keys: set[str], catalog_keys: set[str]) -> bool:
    return any(
        len(a) >= MIN_NEAR_TITLE_CHARS
        and SequenceMatcher(None, a, b).ratio() >= NEAR_TITLE_RATIO
        for a in cover_keys
        for b in catalog_keys
    )


def score(text: BookText, book: Book) -> Scored:
    cover_keys, catalog_keys = _cover_title_keys(text), _catalog_title_keys(book.title)
    title_hit = bool(cover_keys & catalog_keys)
    return Scored(
        book=book,
        title_hit=title_hit,
        author_hit=_author_hit(text.authors, book.author),
        publisher_hit=_publisher_hit(text.publisher, book.publisher),
        author_closeness=_author_closeness(text.authors, book.author),
        title_near=not title_hit and _near(cover_keys, catalog_keys),
    )


def _edition_order(s: Scored) -> tuple:
    """한 묶음 안에서 고를 판본의 순서. 표지와 더 많이 맞는 것, 살 수 있는 것, 표지 그림이 있는 것, 새 판 순이다."""
    b = s.book
    return (
        s.score,
        b.in_stock,
        b.cover_url is not None,
        b.pub_year or 0,
        b.book_id,
    )


def _pick(group: list[Scored]) -> Scored:
    return max(group, key=_edition_order)


def _groups(scored: list[Scored]) -> list[list[Scored]]:
    """같은 책의 판본끼리 묶는다. 검색 순위대로 처음 나온 순서를 지킨다. 제목·저자가 비어 키가 없으면 한 권씩 둔다."""
    groups: dict[object, list[Scored]] = {}
    for s in scored:
        key = editions.edition_key(s.book.title, s.book.author) or s.book.book_id
        groups.setdefault(key, []).append(s)
    return list(groups.values())


def _candidates(groups: list[list[Scored]]) -> Match:
    """묶음마다 한 판본씩, 표지와 많이 맞는 순으로 최대 3권. 같으면 검색 순위를 지킨다(정렬은 순서를 지킨다)."""
    picks = sorted(
        (_pick(g) for g in groups),
        key=lambda s: (
            s.score,
            s.author_closeness,
            s.book.in_stock,
            s.book.cover_url is not None,
        ),
        reverse=True,
    )
    return Match("candidates", picks[:MAX_CANDIDATES], "text")


def _one_by_publisher(groups: list[list[Scored]]) -> list[Scored] | None:
    """출판사까지 맞는 묶음이 하나뿐이면 그 묶음."""
    matched = [g for g in groups if any(s.publisher_hit for s in g)]
    return matched[0] if len(matched) == 1 else None


def decide(text: BookText, books: Sequence[Book]) -> Match:
    """검색 후보 중에서 표지의 책을 고른다. DB 를 읽지 않아 규칙만 따로 시험할 수 있다."""
    if not text.is_book_cover or not text.title:
        return NOTHING
    scored = [score(text, book) for book in books]
    titled = [s for s in scored if s.title_hit]
    if not titled:
        near = [s for s in scored if s.title_near]
        # 제목을 한두 글자 틀리게 읽었을 수 있다. 맞는 책이 있어도 확정하지 않는다
        return _candidates(_groups(near)) if near else NOTHING
    groups = _groups(titled)

    if _foreign_title(text.title):
        # 표지에 로마자 제목만 있으면 원서인지 한국 번역서인지 글자로는 모른다. 제목·저자가 같은 원서를 찾아도
        # 사용자의 책은 번역서일 수 있어 확정하지 않는다(문학동네 『데미안』 표지는 "DEMIAN"만 쓴다)
        return _candidates(groups)

    if text.authors:
        same_author = [g for g in groups if any(s.author_hit for s in g)]
        if len(same_author) == 1:
            return Match("found", [_pick(same_author[0])], "text")
        if len(same_author) > 1:
            # 같은 책인데 저자 표기가 달라("생텍쥐페리"·"앙투안 드 생택쥐페리") 판본 묶기가 갈라 놓았을 수 있다.
            # 제목·저자에 출판사까지 같으면 한 책의 판본들로 보고 그중에서 고른다
            same_publisher = [
                s for g in same_author if any(s.publisher_hit for s in g) for s in g
            ]
            if same_publisher:
                return Match("found", [_pick(same_publisher)], "text")
            return _candidates(same_author)
        # 저자를 읽었는데 맞는 책이 없다. 제목만 같은 다른 책일 수 있어 확정하지 않는다
        return _candidates(groups)

    # 저자를 못 읽었다. 제목이 같은 책이 하나뿐이거나 출판사로 하나가 남을 때만 확정한다
    if len(groups) == 1:
        return Match("found", [_pick(groups[0])], "text")
    only = _one_by_publisher(groups)
    if only is not None:
        return Match("found", [_pick(only)], "text")
    return _candidates(groups)


async def match(conn: asyncpg.Connection, text: BookText) -> Match:
    """표지 글자로 책을 찾는다. ISBN 이 맞으면 그 책, 아니면 제목 검색 뒤 decide 의 규칙이다."""
    book = await catalog.by_isbn(conn, text.isbn)
    if book is not None:
        return Match("found", [Scored(book, True, True, True)], "isbn")
    if not text.is_book_cover or not text.title:
        return NOTHING
    ids = await keyword.search_ids(conn, text.title, SearchFilters(), SEARCH_LIMIT)
    rows = await conn.fetch(catalog.BY_IDS_SQL, ids)
    by_id = {row["book_id"]: catalog.book(row) for row in rows}
    return decide(text, [by_id[i] for i in ids if i in by_id])
