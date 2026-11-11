"""저자를 짚은 요청의 후보를 그 저자의 책으로 좁힌다(#655).

"한강 책 추천해줘"는 1단계가 "한강"을 exact.author 에 맞게 넣는다. 그런데 ① 검색은 그 이름을 낱말로 찾아서, 이름이
흔한 낱말과 같은 작가는 그 낱말이 제목·소개글에 든 책이 후보를 채운다(평가 DB 검색 50권 중 작가 한강의 책 2권, 이상 1권.
황석영 35권·은희경 36권은 괜찮다).

그 저자의 책인지는 저자 칸을 쉼표로 나눈 이름 하나가 통째로 같은지로 본다. 부분 일치로 보면 다른 사람이 섞인다(저자 칸에
"한강"이 든 책 중 한강현 23권·한강희 7권·김한강 등). "J.K. 롤링"과 "조앤 K. 롤링"처럼 표기가 다르면 못 찾는데, 그때는
호출부가 지금까지의 검색 결과를 그대로 쓴다.
"""

import re

import asyncpg

from app.core import popularity
from app.search import books, filters
from app.search.schemas import SearchFilters

# 저자 칸 하나에 여러 사람이 "김인숙, 공선옥, 한강"처럼 이어진다. 210만 권 중 60만 권이 이렇다.
_SEPARATOR = re.compile(r"\s*[;,]\s*")

# 아래 SQL 의 이름 목록 식. names() 와 같은 일을 한다. "한강 지음"처럼 역할 말이 붙은 칸(210만 권 중 약 3천 권)은 떼지 않아 못 찾는다.
NAMES_SQL = r"regexp_split_to_array(btrim(lower(b.author)), '\s*[;,]\s*')"

# 저자 조회를 기다리는 최대 시간(초). 평가 DB 에서 "한강" 1.7ms·"김훈" 5.8ms·"이상" 72ms 다. 그래도 한 글자 이름처럼
# 후보가 아주 많은 이름이 있을 수 있어 연결 기본 한도(db.py 의 command_timeout 5초)에 맡기지 않는다. 넘으면 챗봇이
# 500 을 내므로, 짧게 끊고 호출부가 지금까지의 검색으로 넘어가게 한다(#658 리뷰).
QUERY_TIMEOUT_SECONDS = 1.0

# 이보다 짧은 이름은 DB 에서 찾지 않는다. 한 글자("김")는 저자 칸의 낱말 39만 개가 후보라 평가 DB 에서 2.8초였고,
# 한 글자 이름의 작가를 찾는 요청은 드물다. 그때는 호출부가 지금까지의 검색으로 후보를 만든다.
MIN_NAME_CHARS = 2

# 저자가 같은 책을 이만큼까지 가져온다. 뒤에서 판본을 묶고(한강 66권 → 작품 수십 개) 10권으로 자른다.
AUTHOR_BOOKS_LIMIT = 50

# 순서: 판매 수 → 첫 저자인 책 → 소개글 있는 책(카드 이유의 근거) → 최근 판. 평가 DB 처럼 판매 수가 모두 0이면 뒤의
# 셋으로 정해진다. 첫 저자를 앞에 두는 까닭: 여러 명이 함께 쓴 문집이 그 작가의 소설보다 앞을 채운다(평가 DB "황석영"
# 10권 중 5권이 『통일대담』 같은 문집, 11/11).
_SQL = f"""
SELECT b.book_id
FROM v_books b
LEFT JOIN v_book_popularity p USING (book_id)
WHERE {{match}} AND {NAMES_SQL} @> ARRAY[$1::text]{{filters}}
ORDER BY {popularity.score_sql("p")} DESC,
         ({NAMES_SQL})[1] = $1 DESC,
         (coalesce(b.description, '') <> '') DESC,
         b.pub_year DESC NULLS LAST,
         b.book_id DESC
LIMIT $2
"""


# 이름으로 먼저 거르는 식. 두 글자 이름은 낱말 통째 비슷함(<<%), 세 글자 이상은 부분 일치(ILIKE)다. 둘 다 저자 글자 조각
# 색인(v_books_author_trgm_idx)을 탄다. 진짜 거르기는 뒤의 이름 목록 식이 해서 결과는 같다(fetch 설명)
_MATCH_SHORT = "$1 <<% b.author"
_MATCH_LONG = "b.author ILIKE $3"
# 이 길이부터 부분 일치로 거른다. 부분 일치는 이름의 글자 조각(세 글자 묶음)으로 색인을 찾는데, 두 글자 이름은 조각이 없다
LONG_NAME_CHARS = 3


def _like(name: str) -> str:
    """부분 일치 무늬. 이름 속 %·_·\\ 는 글자 그대로 찾는다(ILIKE 기본 이스케이프 문자는 \\)."""
    escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def key(name: str | None) -> str:
    """사용자가 말한 저자 이름을 저자 칸의 이름과 견줄 모양으로 바꾼다."""
    return (name or "").strip().lower()


def names(author: str | None) -> set[str]:
    """저자 칸의 이름들. NAMES_SQL 과 같은 규칙이다."""
    return {part for part in _SEPARATOR.split(key(author)) if part}


def wrote(book: dict, author: str) -> bool:
    """그 저자의 책인가. 이름 일부만 같은 사람(한강 ↔ 한강현)은 아니다."""
    return key(author) in names(book.get("author"))


async def fetch(
    conn: asyncpg.Connection, author: str, search_filters: SearchFilters
) -> list[dict]:
    """그 저자의 책을 검색 결과와 같은 모양으로 가져온다. 사용자 조건(가격·출간연도·분야)도 건다.

    두 글자 이름을 `$1 <<% b.author`(pg_trgm 의 낱말 통째 비슷함)로 먼저 거르는 까닭: 이미 있는 저자 글자 조각 색인
    (v_books_author_trgm_idx)을 두 글자 이름도 탄다. 세 글자 이상은 부분 일치로 거른다(아래 match). 부분 일치(ILIKE '%한강%')는 세 글자보다 짧으면 이 색인을 못 타서
    표 전체를 훑었다(평가 DB 0.8초, 개발 AI DB 4.6~5.5초). 저자 칸에 이름이 낱말 통째로 있으면 비슷함이 1이라 기준값
    (0.5)을 늘 넘고, 진짜 거르기는 이름 목록 식이 한다. 평가 DB 에서 한강·김훈·이상·히가시노 게이고·J.K. 롤링·황석영의
    책 수가 부분 일치로 찾은 것과 같았다. 새 색인을 만들지 않아도 된다(11/11).
    """
    name = key(author)
    if len(name) < MIN_NAME_CHARS:
        return []
    # 세 글자 이상은 부분 일치로 거른다(#658 두 번째 리뷰). <<% 는 이름의 앞 두 글자로 시작하는 낱말을 모두 후보로 받아,
    # "김영하"면 "김영"으로 시작하는 저자 칸 2.7만 줄을 받고 첫 조회가 2초 안팎이라 1초 한도에 걸렸다(개발 AI DB).
    # 부분 일치는 같은 색인에서 김영하 147줄·0.003초, 김수영 771줄·0.019초였다
    long_name = len(name) >= LONG_NAME_CHARS
    match = [_like(name)] if long_name else []
    where, params = filters.build_where(search_filters, first_param=3 + len(match))
    rows = await conn.fetch(
        _SQL.format(match=_MATCH_LONG if long_name else _MATCH_SHORT, filters=where),
        name,
        AUTHOR_BOOKS_LIMIT,
        *match,
        *params,
        timeout=QUERY_TIMEOUT_SECONDS,
    )
    return await books.fetch(conn, [r["book_id"] for r in rows])


def narrow(results: list[dict], author: str, by_author: list[dict]) -> list[dict]:
    """검색 결과에서 그 저자의 책을 앞에 두고, DB 에서 가져온 그 저자의 책을 뒤에 붙인다.

    그 저자의 책이 한 권도 없으면 검색 결과를 그대로 돌려준다. 저자 표기가 달라 못 찾은 것일 수 있어서, 비우는 것보다
    지금까지처럼 검색 결과로 카드를 만드는 쪽이 낫다.
    """
    matched = [r for r in results if wrote(r, author)]
    seen = {r["book_id"] for r in matched}
    merged = matched + [b for b in by_author if b["book_id"] not in seen]
    return merged or results
