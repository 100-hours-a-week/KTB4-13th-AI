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

# 아래 SQL 의 이름 목록 식. db/migrations/20261111_author_names_idx.sql 의 색인과 글자까지 같아야 색인을 탄다.
# names() 와 같은 일을 한다. "한강 지음"처럼 역할 말이 붙은 칸(210만 권 중 약 3천 권)은 떼지 않아 못 찾는다.
NAMES_SQL = r"regexp_split_to_array(btrim(lower(b.author)), '\s*[;,]\s*')"

# 저자가 같은 책을 이만큼까지 가져온다. 뒤에서 판본을 묶고(한강 66권 → 작품 수십 개) 10권으로 자른다.
AUTHOR_BOOKS_LIMIT = 50

# 순서: 판매 수 → 첫 저자인 책 → 소개글 있는 책(카드 이유의 근거) → 최근 판. 평가 DB 처럼 판매 수가 모두 0이면 뒤의
# 셋으로 정해진다. 첫 저자를 앞에 두는 까닭: 여러 명이 함께 쓴 문집이 그 작가의 소설보다 앞을 채운다(평가 DB "황석영"
# 10권 중 5권이 『통일대담』 같은 문집, 11/11).
_SQL = f"""
SELECT b.book_id
FROM v_books b
LEFT JOIN v_book_popularity p USING (book_id)
WHERE b.author ILIKE $1 AND {NAMES_SQL} @> ARRAY[$2::text]{{filters}}
ORDER BY {popularity.score_sql("p")} DESC,
         ({NAMES_SQL})[1] = $2 DESC,
         (coalesce(b.description, '') <> '') DESC,
         b.pub_year DESC NULLS LAST,
         b.book_id DESC
LIMIT $3
"""


def key(name: str | None) -> str:
    """사용자가 말한 저자 이름을 저자 칸의 이름과 견줄 모양으로 바꾼다."""
    return (name or "").strip().lower()


def names(author: str | None) -> set[str]:
    """저자 칸의 이름들. NAMES_SQL 과 같은 규칙이다."""
    return {part for part in _SEPARATOR.split(key(author)) if part}


def wrote(book: dict, author: str) -> bool:
    """그 저자의 책인가. 이름 일부만 같은 사람(한강 ↔ 한강현)은 아니다."""
    return key(author) in names(book.get("author"))


def _like_pattern(name: str) -> str:
    """ILIKE 의 % 와 _ 를 글자 그대로 찾게 한다."""
    escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def fetch(
    conn: asyncpg.Connection, author: str, search_filters: SearchFilters
) -> list[dict]:
    """그 저자의 책을 검색 결과와 같은 모양으로 가져온다. 사용자 조건(가격·출간연도·분야)도 건다.

    ILIKE 를 먼저 거는 까닭: 이름 목록 색인이 없는 DB 에서도 이름이 세 글자 이상이면 저자 글자 조각 색인을 탄다.
    두 글자 이름은 이름 목록 색인이 없으면 표 전체를 훑는다(평가 DB 0.8초).
    """
    name = key(author)
    if not name:
        return []
    where, params = filters.build_where(search_filters, first_param=4)
    rows = await conn.fetch(
        _SQL.format(filters=where),
        _like_pattern(name),
        name,
        AUTHOR_BOOKS_LIMIT,
        *params,
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
