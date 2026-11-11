"""③ 후보 세 갈래 — 검색(A) 말고 떠올린 제목(B)과 기준 책의 함께 대출·마니아 목록(C)으로 책을 모은다.

CHAT_AGENT 를 켰을 때 쓴다.
- 떠올린 제목: 사용자가 관심 주제를 말하면 그에 맞는 실제 책 제목·저자. 1단계와 나란히 따로 부른다(app/chat/recall.py).
  LLM 은 제목만 떠올리고(검색어 역할), 책인지는 책 표가 정한다. 제목과 저자가 둘 다 맞는 책만 받는다(10/28 실험: 제목만
  맞춘 9권 중 5권이 이름만 같은 다른 책)
- 기준 책: 사용자가 보여 준 카드나 특정 책을 짚으면 그 책. 1단계가 내고, 코드가 메시지에 근거가 있는지 본다(fill_plan).
  그 책의 함께 대출·마니아 목록(app/chat/reading_graph.py)에서 한 걸음 이웃을 데려온다. 순서는 쓰지 않고 허브만 뺀 전부다.
  관계를 물은 턴에 허브 아닌 이웃이 카드 장수보다 적으면 허브로 채운다(neighbor_books)

A 는 지금의 get_candidates 그대로다(app/routers/chat.py). 크로스 인코더(#639)도 A 에만 쓴다. 관련 책은 소개글이 없는
책이 30% 라 글로 줄 세우면 밀린다(11/10 실측).

책마다 출처 꼬리표(sources)를 붙인다. 판단 호출이 한 목록으로 보고 고르고, 카드의 이유를 무엇으로 쓸지 가른다. 같은
책이 여러 갈래에서 오면 꼬리표를 합친다. 우리 서점(책 표)에 있는 책만 남기고, 사용자 조건(가격·연도·분야·출판사)을
A 와 같이 건다. 소개글이 없어도 빼지 않는다.
"""

import asyncio
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import asyncpg

from app.chat import reading_graph
from app.chat.reading_graph import Kind, ReadingGraph
from app.chat.shown import ShownBook
from app.core import categories, db, editions, products
from app.photo import match_cover
from app.photo.read import BookText
from app.search import vector
from app.search.schemas import SearchFilters

logger = logging.getLogger(__name__)

Lane = Literal["search", "title", "co", "mania"]
Relation = Literal["co", "similar"]

# 떠올린 제목은 이만큼만 찾는다. 10/28 실험의 6권 권고보다 하나 적다 — 제목 검색 한 번이 DB 를 한 번 읽는다
MAX_TITLES = 5
# 후보마다 붙이는 이웃 줄. 종류마다 이만큼(허브 보정 순서)
NEIGHBORS_PER_KIND = 3
# 기준 책에서 데려오는 이웃(C) 상한. 정보나루가 목록마다 10권씩 주니 함께 대출·마니아를 합친 한 판본 몫이다
MAX_NEIGHBOR_BOOKS = 20
# 기준 책과 소개가 가까운 책(near_books) 상한. 이웃(C)과 같은 수다. 가까운 자리는 기준 책의 다른 판본이 먼저 차지해서
# 그만큼 더 받아 놓고 뺀다
MAX_NEAR_BOOKS = MAX_NEIGHBOR_BOOKS
_NEAR_FETCH = 30
# 모델이 준 제목·저자 칸의 길이 상한. 검색어로만 쓴다
MAX_TITLE_CHARS = 100
_SUBTITLE = re.compile(r"\s*[:=]\s*.*$| - .*$")

_ROWS_SQL = f"""
SELECT b.book_id, b.isbn13 AS isbn, b.title, b.author, b.publisher,
       {products.price_sql("b")} AS price, {products.in_stock_sql("b")} AS in_stock,
       b.cover_url, b.category, b.pub_year, coalesce(b.description, '') AS description
FROM v_books b
"""
_BY_ISBNS_SQL = _ROWS_SQL + "WHERE b.isbn13 = ANY($1::text[])"
_BY_IDS_SQL = _ROWS_SQL + "WHERE b.book_id = ANY($1::int[])"


@dataclass(frozen=True)
class Title:
    title: str
    author: str | None


@dataclass(frozen=True)
class Plan:
    """첫 LLM 호출이 검색어 말고 더 낸 것과 코드가 메시지로 보탠 것. 비었으면 지금처럼 검색만 한다."""

    # 카드 번호(1부터) 또는 제목. 사용자가 짚은 책이다
    anchor: int | Title | None = None
    # 사용자가 기준 책의 관계를 물었다: 함께 읽은 책(co)이나 비슷한 책 더(similar)
    relation: Relation | None = None
    # 모델이 낸 제목 기준 책(anchor_title). 모델이 카드 번호도 냈는데 그 번호에 근거가 없어 버리면 이것을 쓴다
    named: Title | None = None
    # "이 작가 말고": 기준 책 저자의 책은 빼 달라는 말
    other_author: bool = False
    # 보여 준 카드가 여럿인데 어느 책을 읽은 사람들인지 말하지 않았다. 찾지 않고 번호를 묻는다
    ask_card: bool = False


EMPTY_PLAN = Plan()


def _title(raw: Any) -> Title | None:
    """{"title", "author"} 칸. 제목만 글자로 와도 받는다 — 11/10 실험 1에서 4o 가 기준 책을
    "anchor_title": "데미안" 처럼 글자로 냈는데, 모양이 틀렸다고 버려 제목으로 짚은 기준 책이 0/6 이었다."""
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        return None
    title, author = raw.get("title"), raw.get("author")
    if not isinstance(title, str) or not title.strip():
        return None
    author = author.strip() if isinstance(author, str) and author.strip() else None
    return Title(title.strip()[:MAX_TITLE_CHARS], author and author[:MAX_TITLE_CHARS])


def parse_titles(raw_titles: Any) -> tuple[Title, ...]:
    """떠올린 제목 목록. 저자가 있어야 찾으므로 {"title", "author"} 모양만 받는다. 겹친 칸은 버리고 MAX_TITLES 개까지."""
    titles: list[Title] = []
    if isinstance(raw_titles, list):
        for raw in raw_titles:
            found = _title(raw) if isinstance(raw, dict) else None
            if found is not None and found not in titles:
                titles.append(found)
    return tuple(titles[:MAX_TITLES])


def parse_plan(patch: dict) -> Plan:
    """1단계 patch 에서 기준 책·관계를 꺼낸다. 모양이 틀린 칸은 버린다. 카드 번호가 있으면 그것을 먼저 본다."""
    raw_card = patch.get("anchor_card")
    named = _title(patch.get("anchor_title"))
    anchor: int | Title | None = named
    if isinstance(raw_card, int) and not isinstance(raw_card, bool) and raw_card >= 1:
        anchor = raw_card
    relation = patch.get("relation")
    return Plan(
        anchor=anchor,
        relation=relation if relation in ("co", "similar") else None,
        named=named,
    )


@dataclass(frozen=True)
class Anchor:
    """기준 책. work 는 그래프의 작품 번호이고, 그래프에 없거나 목록이 없는 책이면 None 이다."""

    book_id: int | None
    title: str
    author: str | None
    work: int | None


def display_title(title: str) -> str:
    """꼬리표와 이유에 쓸 제목. 부제를 뗀다."""
    return _SUBTITLE.sub("", title).strip() or title


def title_key(title: str | None) -> str:
    """부제를 뗀 표시 제목의 키. 화면에서 같은 제목으로 보이는 책을 가른다."""
    return editions.title_key(display_title(title or ""))


@dataclass
class Skip:
    """후보에서 뺄 책: 보여 준 책·뺀 책·가진 책(book_id)과 그 판본들, 기준 책 작품, 빼 달라고 한 저자,
    기준 책과 표시 제목이 같은 책.

    기준 책 작품(works)은 판본만 묶는다. 저자가 다른 같은 제목 책(칼 세이건 『코스모스』를 짚었는데 앤 드루얀
    『코스모스』)은 다른 작품이지만 카드로 내면 기준 책이 다시 나온 것처럼 보인다(11/11 실측) — titles 로 뺀다.
    """

    book_ids: set[int] = field(default_factory=set)
    edition_keys: set[tuple[str, str]] = field(default_factory=set)
    works: set[int] = field(default_factory=set)
    authors: set[str] = field(default_factory=set)
    titles: set[str] = field(default_factory=set)

    def blocks(self, book: dict, graph: ReadingGraph | None) -> bool:
        if book["book_id"] in self.book_ids:
            return True
        if self.authors and editions.author_key(book.get("author")) in self.authors:
            return True
        if self.titles and title_key(book.get("title")) in self.titles:
            return True
        if (
            editions.edition_key(book.get("title"), book.get("author"))
            in self.edition_keys
        ):
            return True
        return graph is not None and _work_of(graph, book) in self.works


def _work_of(graph: ReadingGraph, book: dict) -> int | None:
    return graph.work_of(book.get("isbn"), book.get("title"), book.get("author"))


def passes(book: dict, filters: SearchFilters, publisher: str | None) -> bool:
    """사용자 조건(가격·출간연도·분야·출판사)에 맞는가. ① 의 SQL 필터(app/search/filters.py)와 같은 뜻이다.

    값이 없는 책은 그 조건을 걸면 빠진다(SQL 의 NULL 비교와 같다).
    """
    price, year = book.get("price"), book.get("pub_year")
    if filters.price_min is not None and (price is None or price < filters.price_min):
        return False
    if filters.price_max is not None and (price is None or price > filters.price_max):
        return False
    if filters.pub_year_from is not None and (
        year is None or year < filters.pub_year_from
    ):
        return False
    if filters.pub_year_to is not None and (year is None or year > filters.pub_year_to):
        return False
    if filters.category is not None:
        allowed = categories.filter_categories(filters.category)
        if allowed is None:
            allowed = (filters.category,)
        if allowed and book.get("category") not in allowed:
            return False
    if publisher:
        want = editions.title_key(publisher)
        if want and want not in editions.title_key(book.get("publisher")):
            return False
    return True


def _best_edition(rows: Sequence[dict]) -> dict:
    """한 작품의 판본 중 카드로 낼 하나. 소개글이 있는 것, 살 수 있는 것, 새 판 순이다."""
    return max(
        rows,
        key=lambda r: (
            bool(r["description"].strip()),
            r["in_stock"],
            r["pub_year"] or 0,
            r["book_id"],
        ),
    )


async def _rows_by_isbns(isbns: Iterable[str]) -> list[dict]:
    wanted = sorted(set(isbns))
    if not wanted:
        return []
    async with db.get_pool().acquire() as conn:
        rows = await conn.fetch(_BY_ISBNS_SQL, wanted)
    return [dict(r) for r in rows]


async def resolve_anchor(
    plan: Plan,
    shown_cards: Sequence[tuple[int, str]],
    shown_books: Sequence[ShownBook],
    graph: ReadingGraph | None,
) -> Anchor | None:
    """사용자가 짚은 책. 카드 번호면 보여 준 카드에서, 제목이면 책 표에서 찾는다. 못 찾으면 None."""
    if plan.anchor is None:
        return None
    if isinstance(plan.anchor, int):
        book = next((b for b in shown_books if b.rank == plan.anchor), None)
        isbn = dict(shown_cards).get(plan.anchor)
        if book is None:
            return None
        work = graph.work_of(isbn, book.title, book.author) if graph else None
        return Anchor(book.book_id, display_title(book.title), book.author, work)
    try:
        found = await _find_named(plan.anchor, graph)
    except Exception:
        # 기준 책을 못 찾아도 턴은 이어 간다. 검색만으로 찾던 고치기 전과 같다
        logger.exception("기준 책 찾기 실패 — 기준 책 없이 찾는다")
        return None
    if found is not None:
        work = _work_of(graph, found) if graph else None
        return Anchor(
            found["book_id"], display_title(found["title"]), found["author"], work
        )
    # 우리 서점에 없는 책이어도 그래프에는 있을 수 있다(정보나루는 도서관 책이다)
    work = graph.work_of(None, plan.anchor.title, plan.anchor.author) if graph else None
    if work is None:
        return None
    return Anchor(None, plan.anchor.title, plan.anchor.author, work)


async def _match_title(conn: asyncpg.Connection, title: Title) -> int | None:
    """제목과 저자가 둘 다 맞는 책 한 묶음이 있으면 그 판본의 book_id. 표지 글자로 책을 찾는 규칙(#572)을 쓴다."""
    text = BookText(
        is_book_cover=True,
        title=title.title,
        authors=[title.author] if title.author else [],
    )
    found = await match_cover.match(conn, text)
    if found.kind != "found" or not found.books[0].author_hit:
        return None
    return found.books[0].book.book_id


async def _find_titled(title: Title) -> dict | None:
    if not title.author:
        return None
    async with db.get_pool().acquire() as conn:
        book_id = await _match_title(conn, title)
        if book_id is None:
            return None
        rows = await conn.fetch(_BY_IDS_SQL, [book_id])
    return dict(rows[0]) if rows else None


async def _find_named(title: Title, graph: ReadingGraph | None) -> dict | None:
    """사용자가 이름을 말한 기준 책을 책 표에서 찾는다. 제목이 같은 책만 받고, 저자가 없어도 찾는다.

    떠올린 제목(B)과 달리 사용자가 직접 말한 제목이라 지어낸 책일 걱정이 없다. 모델은 이름만 말한 책에 저자를 안
    붙이거나(11/10 실험 1: "데미안"·"코스모스") 틀리게 붙인다. 제목이 같은 책이 여럿이면(『코스모스』는 칼 세이건의 책과
    다른 저자의 책이 있다) 함께 대출·마니아 목록이 있는 작품을 먼저, 그다음은 표지 글자 규칙의 순서(저자가 맞는 책,
    살 수 있는 책)를 따른다.
    """
    text = BookText(
        is_book_cover=True,
        title=title.title,
        authors=[title.author] if title.author else [],
    )
    async with db.get_pool().acquire() as conn:
        found = await match_cover.match(conn, text)
        ids = [s.book.book_id for s in found.books if s.title_hit]
        if not ids:
            return None
        rows = {r["book_id"]: dict(r) for r in await conn.fetch(_BY_IDS_SQL, ids)}
    books = [rows[i] for i in ids if i in rows]
    if graph is not None:
        listed = [
            b
            for b in books
            if (work := _work_of(graph, b)) is not None and graph.has_lists(work)
        ]
        books = listed or books
    return books[0] if books else None


async def _find_titled_or_skip(title: Title) -> dict | None:
    """한 제목 찾기가 DB 제한 시간을 넘거나 실패하면 그 제목만 건너뛴다. 11/10 실험 1에서 제목 찾기 하나가 5초를 넘자
    예외가 갈래 모으기를 통째로 터뜨려 턴이 500 이 됐다. 떠올린 제목은 덤이라 없어도 검색으로 답한다."""
    try:
        return await _find_titled(title)
    except Exception:
        logger.warning(
            "떠올린 제목 찾기 실패 — 건너뛴다: %s", title.title, exc_info=True
        )
        return None


async def titled_books(titles: Sequence[Title]) -> list[dict]:
    """B — 떠올린 제목을 책 표에서 찾는다. 저자 없이 온 제목은 찾지 않는다(동명이서를 가를 수 없다)."""
    wanted = [t for t in titles if t.author]
    if not wanted:
        return []
    found = await asyncio.gather(*(_find_titled_or_skip(t) for t in wanted))
    books = [book for book in found if book is not None]
    for book in books:
        book["sources"] = [("title", None)]
    logger.info("떠올린 제목 %d개 중 %d권을 책 표에서 찾았다", len(wanted), len(books))
    return books


# 1단계 모델이 기준 책을 못 짚은 말을 코드가 채운다. 11/10 점검에서 gpt-4o-mini 는 "이런 책 더 없어?"(보여 준 카드 한 장)에
# 기준 책을 한 번도 내지 않았다. 카드 번호를 말했거나, 지시어를 썼는데 보여 준 카드가 한 장뿐일 때만 정한다. 카드가
# 여럿인데 "이 책"이라고만 하면 어느 책인지 모르니 정하지 않는다.
_CARD_NUMBER = re.compile(r"(?<!\d)(\d{1,2})\s?번")
_ORDINAL = {"첫": 1, "두": 2, "세": 3}
_ORDINAL_WORD = re.compile(r"(첫|두|세)\s?번째")
# "이 작가 말고 다른 작가로 비슷한 거"도 보여 준 책을 짚는 말이다(11/10 실험 1에서 못 잡음)
_POINTING = re.compile(
    r"(?<![가-힣])((이|그|저|이런|그런|저런)\s?(책|거|것|류|작가|저자)|이거|그거|저거)"
)
_OTHER_AUTHOR = re.compile(
    r"(이|그|저)\s?(작가|저자)\s?(말고|빼고|아닌|외에|제외)|다른\s?(작가|저자)"
)
# "다른 거 보여줘"는 보여 준 책과 다른 책을 원하는 말이다. 11/10 실험 1에서 4o 가 카드 3장 앞의 이 말을 "1번과 비슷한
# 책"으로 읽었다. 함께 읽은·비슷한 낱말이 같이 있으면("다른 작가 책으로 비슷한 거") 기준 책을 짚는 말일 수 있어 그대로 둔다
_OTHER_BOOKS = re.compile(r"(?<![가-힣])(다른|딴)\s?(거|것|걸|책)")
# "함께 읽은 책"을 묻는 말. "이 책 읽은 사람들은 또 뭐 읽었어?"처럼 같이·함께 없이도 묻는다. 빌린 책을 묻는 말도 같다
_CO_WORDS = re.compile(
    r"같이\s?읽|함께\s?읽|같이\s?본|함께\s?본|읽은\s?(사람|분|독자)|(?<![가-힣])본\s?(사람|분)|또\s?(뭐|무슨\s?책)\s?읽"
    r"|빌린|빌려|빌렸"
)
_SIMILAR_WORDS = re.compile(
    r"비슷|같은\s?느낌|같은\s?책|이런\s?(책|거)\s?더|그런\s?(책|거)\s?더"
)


def _said_card(
    message: str, ranks: Sequence[int], card_titles: Mapping[int, str]
) -> int | None:
    """메시지가 말로 짚은 카드: 번호("2번"), 차례("두 번째"), 카드 제목."""
    if (match := _CARD_NUMBER.search(message)) and int(match.group(1)) in ranks:
        return int(match.group(1))
    if (match := _ORDINAL_WORD.search(message)) and _ORDINAL[match.group(1)] in ranks:
        return _ORDINAL[match.group(1)]
    said = editions.title_key(message)
    for rank, title in card_titles.items():
        key = editions.title_key(display_title(title))
        if rank in ranks and len(key) >= 2 and key in said:
            return rank
    return None


def _card_titled(title: Title, card_titles: Mapping[int, str]) -> int | None:
    """모델이 제목으로 짚은 책이 보여 준 카드면 그 카드 번호. 카드면 ISBN 으로 그래프를 찾을 수 있다."""
    key = editions.title_key(title.title)
    return next(
        (
            rank
            for rank, shown in card_titles.items()
            if key and editions.title_key(display_title(shown)) == key
        ),
        None,
    )


def points_at_book(message: str) -> bool:
    """보여 준 책이나 특정 책을 기준으로 삼는 말 같은가. 떠올린 제목을 부를지 정하는 데 쓴다(낱말로 어림한다)."""
    return bool(
        _CARD_NUMBER.search(message)
        or _ORDINAL_WORD.search(message)
        or _POINTING.search(message)
        or _CO_WORDS.search(message)
    )


def fill_plan(
    plan: Plan,
    message: str,
    ranks: Sequence[int],
    card_titles: Mapping[int, str] | None = None,
) -> Plan:
    """모델이 낸 기준 책을 메시지에 비춰 보고, 비운 기준 책·관계를 메시지 낱말로 채운다.

    모델이 짚은 카드는 메시지에 근거가 있을 때만 믿는다: 번호·차례·카드 제목을 말했거나, 보여 준 카드가 한 장뿐일
    때다(한 장이면 "반도체 산업이 궁금해"처럼 그 책에서 이어 묻는 말도 받는다). 11/10 실험 1에서 4o 는 카드 3장 앞의
    "이거 읽은 사람들은 또 뭐 봐?"에 실행마다 다른 카드(2번, 1번)를 찍었고, "다른 거 보여줘"를 1번과 비슷한 책으로
    읽었다. 해석은 모델이 하고, 그 해석대로 기준 책을 쓸지는 코드가 정한다. 메시지가 번호를 말했으면 모델 번호보다
    메시지를 따른다.
    """
    card_titles = card_titles or {}
    said = _said_card(message, ranks, card_titles)
    # 번호나 제목으로 카드를 짚었으면 "다른 책"이어도 그 책에서 출발하는 말이다("1번 독자들이 좋아한 다른 책")
    if (
        said is None
        and _OTHER_BOOKS.search(message)
        and not (_CO_WORDS.search(message) or _SIMILAR_WORDS.search(message))
    ):
        return EMPTY_PLAN
    anchor = plan.anchor
    if isinstance(anchor, Title):
        anchor = _card_titled(anchor, card_titles) or anchor
    if not isinstance(anchor, Title):
        if said is not None:
            anchor = said
        elif len(ranks) == 1 and (anchor in ranks or _POINTING.search(message)):
            anchor = ranks[0]
        else:
            # 근거 없이 짚은 카드는 버린다. 모델이 제목도 냈으면 그 책을 쓴다
            anchor = plan.named
    relation = plan.relation
    if relation is None:
        if _CO_WORDS.search(message):
            relation = "co"
        elif anchor is not None and _SIMILAR_WORDS.search(message):
            relation = "similar"
    ask_card = anchor is None and relation == "co" and len(ranks) >= 2
    return Plan(
        anchor=anchor,
        relation=relation if anchor is not None else None,
        named=plan.named,
        other_author=anchor is not None and bool(_OTHER_AUTHOR.search(message)),
        ask_card=ask_card,
    )


def relation_kinds(relation: Relation | None) -> tuple[Kind, ...]:
    """기준 책에서 어느 목록을 데려올까. 함께 읽은 책을 물으면 함께 대출만, 비슷한 책을 더 원하면 마니아만, 아니면 둘 다."""
    if relation == "co":
        return ("co",)
    if relation == "similar":
        return ("mania",)
    return reading_graph.KINDS


async def neighbor_books(
    graph: ReadingGraph,
    anchor: Anchor,
    kinds: Sequence[Kind],
    *,
    skip: Skip | None = None,
    fill_to: int = 0,
) -> list[dict]:
    """C — 기준 책의 목록(한 걸음)에 든 작품을 우리 서점 책으로 바꾼다. 허브는 뺀다. 작품마다 판본 하나.

    fill_to 는 관계를 물은 턴에 채울 카드 장수다. 허브 아닌 이웃(skip 이 빼는 책 말고)이 그보다 적으면 남의 목록에 덜 든
    허브부터 모자란 만큼 채운다. 허브는 남의 목록 수 상위 100작품이라 그래프가 커지면 바뀐다 — 6.4만 권 그래프에서
    사피엔스·넥서스가 허브가 되자 "코스모스 읽은 사람들은?" 후보가 5권에서 3권으로 줄었다(11/11 실측). 허브 가운데 덜 든
    책은 막 허브가 된 책이라 불편한 편의점 같은 맨 위 허브보다 먼저 온다.
    """
    if anchor.work is None:
        return []
    kind_of: dict[int, list[Kind]] = {}
    for kind in kinds:
        for work in graph.neighbors(anchor.work, kind):
            kind_of.setdefault(work, []).append(kind)
    isbns = [isbn for work in kind_of for isbn in graph.isbns.get(work, ())]
    by_work: dict[int, list[dict]] = {}
    for row in await _rows_by_isbns(isbns):
        work = graph.by_isbn.get(row["isbn"])
        if work in kind_of and not (skip is not None and skip.blocks(row, graph)):
            by_work.setdefault(work, []).append(row)

    # 판본마다 받은 목록을 합치면 이웃이 40권을 넘기도 한다(데미안 12판본 → 마니아 41작품). 판단 호출에 넣는 책 수를
    # 고정하려고 허브 보정 순서(남의 목록에 덜 든 책부터)로 MAX_NEIGHBOR_BOOKS 권만 남긴다
    def by_listed(works: Iterable[int]) -> list[int]:
        return sorted(
            works, key=lambda w: (min(graph.listed[kind][w] for kind in kind_of[w]), w)
        )

    ranked = by_listed(w for w in by_work if not graph.is_hub(w))[:MAX_NEIGHBOR_BOOKS]
    hubs = by_listed(w for w in by_work if graph.is_hub(w))[
        : max(0, fill_to - len(ranked))
    ]
    books = []
    for work in ranked + hubs:
        book = _best_edition(by_work[work])
        book["sources"] = [(kind, anchor.title) for kind in kind_of[work]]
        books.append(book)
    logger.info(
        "기준 책 이웃 %d작품 중 우리 서점에 있고 뺄 책이 아닌 %d권, 그중 %d권을 데려왔다(허브로 채운 책 %d권)",
        len(kind_of),
        len(by_work),
        len(books),
        len(hubs),
    )
    return books


def has_mania(graph: ReadingGraph | None, anchor: Anchor | None) -> bool:
    """기준 책에 마니아 목록이 있는가. "비슷한 책 더"는 이 목록에서 고른다."""
    return (
        graph is not None
        and anchor is not None
        and anchor.work is not None
        and bool(graph.neighbors(anchor.work, "mania"))
    )


async def near_books(anchor: Anchor) -> list[dict] | None:
    """기준 책과 소개가 가까운 책 — 기준 책 벡터의 가까운 이웃. 마니아 목록이 없는 기준 책의 "비슷한 책 더"에 쓴다(#654).

    마니아 목록에 나오는 책의 76%는 자기 목록이 없다(11/11 그래프 실측). 그래서 한 번 넓혀 나온 카드에서 또 누르면
    대부분 여기로 온다. 이 갈래가 없을 때는 spec 에 남은 앞 턴 주제로 찾아 다른 주제의 책이 나왔다(11/11 dev,
    『편견 없는 유전자』). 꼬리표는 검색과 같다 — 카드 이유는 소개로 쓴다. 기준 책의 다른 판본은 뺀다.
    기준 책이 우리 서점에 없거나 벡터가 없으면(소개글 없는 책) None.
    """
    if anchor.book_id is None:
        return None
    async with db.get_pool().acquire() as conn:
        raw = await conn.fetchval(
            "SELECT embedding::text FROM book_embeddings WHERE book_id = $1",
            anchor.book_id,
        )
        if not raw:
            return None
        ids = [
            book_id
            for book_id in await vector.search_ids(
                conn, json.loads(raw), SearchFilters(), limit=_NEAR_FETCH
            )
            if book_id != anchor.book_id
        ]
        rows = await conn.fetch(_BY_IDS_SQL, ids)
    by_id = {r["book_id"]: dict(r) for r in rows}
    own = reading_graph.work_key(anchor.title, anchor.author)
    books = [
        by_id[book_id]
        for book_id in ids
        if book_id in by_id
        and (
            own is None
            or reading_graph.work_key(by_id[book_id]["title"], by_id[book_id]["author"])
            != own
        )
    ][:MAX_NEAR_BOOKS]
    for book in books:
        book["sources"] = [("search", None)]
    logger.info(
        "기준 책 마니아 목록 없음 — 기준 책과 소개가 가까운 책 %d권을 데려왔다",
        len(books),
    )
    return books


def merge(*lanes: Sequence[dict], graph: ReadingGraph | None) -> list[dict]:
    """갈래들을 한 목록으로. 같은 책(같은 book_id, 판본, 작품)은 처음 나온 것 하나에 꼬리표를 합친다."""
    merged: list[dict] = []
    seen: dict[object, dict] = {}
    for lane in lanes:
        for book in lane:
            keys = [("id", book["book_id"])]
            if (
                edition := editions.edition_key(book.get("title"), book.get("author"))
            ) is not None:
                keys.append(("edition", edition))
            # 판본 표시("(리커버)", "(양장)")만 다른 책도 한 작품이다. 그래프 없이도 같은 키 규칙으로 묶는다
            if (
                key := reading_graph.work_key(book.get("title"), book.get("author"))
            ) is not None:
                keys.append(("work_key", key))
            if graph is not None and (work := _work_of(graph, book)) is not None:
                keys.append(("work", work))
            kept = next((seen[k] for k in keys if k in seen), None)
            if kept is None:
                kept = book
                kept.setdefault("sources", [("search", None)])
                merged.append(kept)
            else:
                for source in book.get("sources") or [("search", None)]:
                    if source not in kept["sources"]:
                        kept["sources"].append(source)
            for k in keys:
                seen.setdefault(k, kept)
    return merged


async def attach_neighbor_lines(
    graph: ReadingGraph, candidates: list[dict], skip: Skip
) -> None:
    """후보마다 함께 대출·마니아 이웃을 종류마다 NEIGHBORS_PER_KIND 권 붙인다(neighbors 칸).

    우리 서점에 있는 책만, 허브를 빼고 남의 목록에 덜 든 책부터 고른다(reading_graph.pick). 보여 준 책·가진 책과 후보
    자신은 뺀다. 말풍선이 다음 방향으로 제안할 책이라 사용자 조건(가격 등)은 걸지 않는다.
    """
    works = {id(c): _work_of(graph, c) for c in candidates}
    wanted = {
        w
        for c in candidates
        if (work := works[id(c)]) is not None
        for kind in reading_graph.KINDS
        for w in graph.neighbors(work, kind)
        if not graph.is_hub(w)
    }
    isbns = [isbn for w in wanted for isbn in graph.isbns.get(w, ())]
    shelf: dict[int, list[dict]] = {}
    for row in await _rows_by_isbns(isbns):
        work = graph.by_isbn.get(row["isbn"])
        if work is not None and not skip.blocks(row, graph):
            shelf.setdefault(work, []).append(row)
    titles = {
        work: display_title(_best_edition(rows)["title"])
        for work, rows in shelf.items()
    }
    for c in candidates:
        c["neighbors"] = _neighbor_titles(graph, works[id(c)], titles)


def _neighbor_titles(
    graph: ReadingGraph, work: int | None, titles: dict[int, str]
) -> dict[str, list[str]]:
    """한 후보의 이웃 줄. 우리 서점에 있는(titles 에 든) 작품만, 후보 자신은 빼고 고른다."""
    if work is None:
        return {kind: [] for kind in reading_graph.KINDS}
    return {
        kind: [
            titles[w]
            for w in graph.pick(
                work, kind, NEIGHBORS_PER_KIND, lambda w: w in titles and w != work
            )
        ]
        for kind in reading_graph.KINDS
    }


async def fetch_rows(book_ids: Sequence[int]) -> dict[int, dict]:
    """A 후보에 없는 칸(분류·출간연도)을 채울 때 쓴다."""
    if not book_ids:
        return {}
    async with db.get_pool().acquire() as conn:
        rows = await conn.fetch(_BY_IDS_SQL, list(book_ids))
    return {r["book_id"]: dict(r) for r in rows}
