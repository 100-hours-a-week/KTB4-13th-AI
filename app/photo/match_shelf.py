"""책장 사진에서 읽은 책등 글자로 카탈로그의 책을 찾는다(#615).

책등은 글씨가 작아 모델이 저자 이름을 그럴듯하게 지어낸다. 저자를 증거로 쓰면 맞힌 책이 88%에서 44%로
떨어졌다(11/04 책장 실험). 그래서 저자는 보지 않고 제목만 엄격하게 본다. 카탈로그에 없는 책을 가장 가까운 다른
책에 붙이지 않는 것이 목적이다.

1. ISBN 이 맞으면 그 책이다(책등에는 거의 없다).
2. 제목, 제목+부제로 글자 조각이 비슷한 제목을 찾는다(띄어쓰기를 뺀 제목 색인).
3. 1등 후보의 제목이 읽은 글자와 90% 이상 같고 권 번호 숫자가 어긋나지 않을 때만 찾은 것이다. 제목 점수가 같으면
   새 판을 고른다.

같은 사진의 책장 실험과 같은 규칙이다(교육장 127권 중 97권, 집 87권 중 78권을 맞게 확정, #615 시험 결과). 제목이
같은 다른 책은 이 규칙으로 못 가른다. 그래서 사용자가 결과 확인 화면에서 사진과 견줘 고친다.
"""

import re
import unicodedata
from collections.abc import Sequence

import asyncpg
from rapidfuzz import fuzz

from app.photo import catalog, volume
from app.photo.catalog import Book
from app.photo.read import BookText
from app.search import keyword

TITLE_MIN = 0.9
# 검색어 하나로 받을 후보 수
SEARCH_LIMIT = 40
# 글자 조각 비슷함의 하한. 책등을 한두 글자 틀리게 읽어도 후보에 들게 기본값(0.3)보다 낮춘다
SIMILARITY_FLOOR = 0.25
# 한 글자 검색어는 너무 많은 책과 비슷해 찾지 않는다
MIN_QUERY_CHARS = 2

_NON_KEY = re.compile(r"[^0-9a-z가-힣]+")
_SUBTITLE_SPLIT = re.compile(r"\s*[:=]\s*| - ")
_LEADING_PAREN = re.compile(r"^\s*\([^)]*\)\s*")
_PARENS = re.compile(r"[()]")

# 띄어쓰기를 뺀 제목의 글자 조각 색인(20260922 마이그레이션)을 타야 210만 권에서 빨리 찾는다. 식이 색인과 같아야
# 해서 ① 검색과 같은 식을 쓴다
_NOSPACE_TITLE = keyword._nospace("b.title")
_SEARCH_SQL = (
    catalog.COLUMNS
    + f"WHERE {_NOSPACE_TITLE} % $1 "
    + f"ORDER BY similarity({_NOSPACE_TITLE}, $1) DESC LIMIT $2"
)


def key(text: str | None) -> str:
    """견주기용 글자. 전각·반각을 맞추고 소문자로 바꾼 뒤 숫자·로마자·한글만 남긴다."""
    return _NON_KEY.sub("", unicodedata.normalize("NFKC", text or "").lower())


def catalog_forms(title: str) -> set[str]:
    """카탈로그 제목의 견주기용 모양. 부제를 떼고, 앞에 붙은 괄호 말("(언어 자료 처리를 위한)")도 뗀다."""
    head = _SUBTITLE_SPLIT.split(title or "")[0]
    core = _LEADING_PAREN.sub("", head)
    return {key(core), key(_PARENS.sub(" ", head))} - {""}


def read_forms(text: BookText) -> set[str]:
    """책등에서 읽은 글자의 견주기용 모양. 부제가 제목 앞뒤 어디에 붙어도 맞게 본다."""
    title, subtitle = text.title or "", text.subtitle or ""
    return {key(title), key(title + subtitle), key(subtitle + title)} - {""}


def title_score(text: BookText, book: Book) -> float:
    """읽은 제목과 카탈로그 제목이 얼마나 같은가(0–1). 여러 모양 중 가장 잘 맞는 짝으로 본다."""
    reads, forms = read_forms(text), catalog_forms(book.title)
    return max((fuzz.ratio(r, f) / 100 for r in reads for f in forms), default=0.0)


def _read_numbers_text(text: BookText) -> str:
    """권 번호 견주기에 쓸 읽은 글자. 판 표시는 권 번호가 아니라 넣지 않는다."""
    vol = "" if volume.is_edition_mark(text.volume) else (text.volume or "")
    return f"{text.title or ''} {vol}"


def decide(text: BookText, books: Sequence[Book]) -> Book | None:
    """검색 후보 중에서 책등의 책을 고른다. 확정할 수 없으면 None. DB 를 읽지 않아 규칙만 따로 시험할 수 있다."""
    if not text.title or not books:
        return None
    # 제목 점수, 그다음 새 판 순. 정렬은 순서를 지켜서 둘 다 같으면 검색 순위대로다
    ranked = sorted(
        books,
        key=lambda b: (title_score(text, b), b.pub_year or 0),
        reverse=True,
    )
    best = ranked[0]
    if title_score(text, best) < TITLE_MIN:
        return None
    if not volume.compatible(_read_numbers_text(text), best.title):
        return None
    return best


def _queries(text: BookText) -> list[str]:
    queries = []
    for q in (text.title, f"{text.title or ''} {text.subtitle or ''}"):
        k = key(q)
        if len(k) >= MIN_QUERY_CHARS and k not in queries:
            queries.append(k)
    return queries


async def _search(conn: asyncpg.Connection, query: str) -> list[Book]:
    # SET LOCAL 은 트랜잭션 안에서만 먹고, 끝나면 연결이 원래 설정으로 돌아간다(① 검색과 같은 방식)
    async with conn.transaction():
        await conn.execute(
            f"SET LOCAL pg_trgm.similarity_threshold = {SIMILARITY_FLOOR}"
        )
        rows = await conn.fetch(_SEARCH_SQL, query, SEARCH_LIMIT)
    return [catalog.book(row) for row in rows]


async def match(
    conn: asyncpg.Connection, text: BookText
) -> tuple[Book | None, str | None]:
    """책등 글자로 책을 찾는다. (찾은 책, 찾은 길 "isbn"·"text") 이고, 못 찾으면 (None, None) 이다."""
    book = await catalog.by_isbn(conn, text.isbn)
    if book is not None:
        return book, "isbn"
    candidates: dict[int, Book] = {}
    for query in _queries(text):
        for found in await _search(conn, query):
            candidates.setdefault(found.book_id, found)
    best = decide(text, list(candidates.values()))
    return (best, "text") if best is not None else (None, None)
