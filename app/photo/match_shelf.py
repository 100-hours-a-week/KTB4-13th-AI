"""책장 사진에서 읽은 책등 글자로 카탈로그의 책을 찾는다(#615).

책등은 글씨가 작아 모델이 저자 이름을 그럴듯하게 지어낸다. 저자를 증거로 쓰면 맞힌 책이 88%에서 44%로
떨어졌다(11/04 책장 실험). 그래서 저자는 보지 않고 제목만 엄격하게 본다. 카탈로그에 없는 책을 가장 가까운 다른
책에 붙이지 않는 것이 목적이다.

1. ISBN 이 맞으면 그 책이다(책등에는 거의 없다).
2. 제목, 제목+부제로 글자 조각이 비슷한 제목을 찾는다(띄어쓰기를 뺀 제목 색인).
3. 1등 후보의 제목이 읽은 글자와 90% 이상 같고 권 번호 숫자가 어긋나지 않을 때만 찾은 것이다. 제목 점수가 같으면
   새 판을 고른다.
4. 3번에서 확정하지 못하면, 3번과 똑같이 다듬은 제목(부제·앞 괄호 말을 뗀 것)의 색인으로 다시 찾아 3번을 한 번 더 한다.

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
# 글자 조각 비슷함의 하한. 낮출수록 색인이 고른 줄을 DB 가 많이 다시 읽어 느려진다. 책장 18장의 검색에서 0.25 는
# 0.4 보다 다시 본 줄이 7.9배였고(78권 한 장 38~120초), 늘어난 확정은 앞 괄호 말이 긴 책 2권뿐이었다. 그 책은
# 다듬은 제목으로 다시 찾아 되찾는다(#615, docs/trouble/2026-11-09.md)
SIMILARITY_FLOOR = 0.4
# 한 글자 검색어는 너무 많은 책과 비슷해 찾지 않는다
MIN_QUERY_CHARS = 2

_NON_KEY = re.compile(r"[^0-9a-z가-힣]+")
_SUBTITLE_SPLIT = re.compile(r"\s*[:=]\s*| - ")
_LEADING_PAREN = re.compile(r"^\s*\([^)]*\)\s*")
_PARENS = re.compile(r"[()]")

# 띄어쓰기를 뺀 제목의 글자 조각 색인(20260922 마이그레이션)을 타야 210만 권에서 빨리 찾는다. 식이 색인과 같아야
# 해서 ① 검색과 같은 식을 쓴다
_NOSPACE_TITLE = keyword._nospace("b.title")
# 판정(title_key)과 똑같이 다듬은 제목: 부제와 앞 괄호 말을 떼고 key() 를 한 모양. 띄어쓰기만 뺀 제목으로는
# "(…전문가가 알려 주는) 챗GPT시대 기업이 살아남는 법"을 그대로 읽어도 비슷함이 0.31 이라 후보에 못 든다(책
# 목록의 4.3%). 다듬은 제목 색인(20261109 마이그레이션)과 식이 글자 하나까지 같아야 탄다
_KEY_TITLE = (
    "regexp_replace(lower(normalize(regexp_replace(regexp_replace(b.title, "
    r"'\s*[:=]\s*.*$| - .*$', ''), '^\s*\([^)]*\)\s*', ''), NFKC)), "
    "'[^0-9a-z가-힣]', '', 'g')"
)


def _search_sql(title_expr: str) -> str:
    return (
        catalog.COLUMNS
        + f"WHERE {title_expr} % $1 "
        + f"ORDER BY similarity({title_expr}, $1) DESC LIMIT $2"
    )


# 띄어쓰기만 뺀 제목으로 먼저 찾고, 확정하지 못했을 때만 다듬은 제목으로 다시 찾는다. 앞 괄호 말은 책등에
# 적히기도("(박미정의) 깃&깃허브 입문") 안 적히기도 해서 한쪽 모양만으로는 놓친다. 다듬은 제목만 쓰면 오히려
# 교육장 64권 → 61권이었다. 다시 찾는 책은 4권 중 1권꼴이라 DB 가 다시 보는 줄은 11%만 는다(#615)
_SEARCH_SQLS = (_search_sql(_NOSPACE_TITLE), _search_sql(_KEY_TITLE))


def key(text: str | None) -> str:
    """견주기용 글자. 전각·반각을 맞추고 소문자로 바꾼 뒤 숫자·로마자·한글만 남긴다."""
    return _NON_KEY.sub("", unicodedata.normalize("NFKC", text or "").lower())


def title_key(title: str | None) -> str:
    """부제와 앞 괄호 말("(언어 자료 처리를 위한)")을 뗀 제목의 key(). 다듬은 제목 색인(_KEY_TITLE)이 DB 에서
    하는 일과 같다."""
    head = _SUBTITLE_SPLIT.split(title or "")[0]
    return key(_LEADING_PAREN.sub("", head))


def catalog_forms(title: str) -> set[str]:
    """카탈로그 제목의 견주기용 모양. 부제를 뗀 제목을, 앞 괄호 말을 뗀 것과 괄호만 지운 것 두 가지로 본다."""
    head = _SUBTITLE_SPLIT.split(title or "")[0]
    return {title_key(title), key(_PARENS.sub(" ", head))} - {""}


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


async def _search(conn: asyncpg.Connection, sql: str, query: str) -> list[Book]:
    # SET LOCAL 은 트랜잭션 안에서만 먹고, 끝나면 연결이 원래 설정으로 돌아간다(① 검색과 같은 방식)
    async with conn.transaction():
        await conn.execute(
            f"SET LOCAL pg_trgm.similarity_threshold = {SIMILARITY_FLOOR}"
        )
        rows = await conn.fetch(sql, query, SEARCH_LIMIT)
    return [catalog.book(row) for row in rows]


async def match(
    conn: asyncpg.Connection, text: BookText
) -> tuple[Book | None, str | None]:
    """책등 글자로 책을 찾는다. (찾은 책, 찾은 길 "isbn"·"text") 이고, 못 찾으면 (None, None) 이다."""
    book = await catalog.by_isbn(conn, text.isbn)
    if book is not None:
        return book, "isbn"
    queries = _queries(text)
    for sql in _SEARCH_SQLS:
        candidates: dict[int, Book] = {}
        for query in queries:
            for found in await _search(conn, sql, query):
                candidates.setdefault(found.book_id, found)
        best = decide(text, list(candidates.values()))
        if best is not None:
            return best, "text"
    return None, None
