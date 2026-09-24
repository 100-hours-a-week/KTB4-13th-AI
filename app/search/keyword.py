"""키워드 검색 — 제목·저자·소개글에서 검색어를 찾아 순위를 매긴다.

검색어가 제목인지 저자인지 구분하지 않는다(명세 ①: 검색어에서 조건을 추출하지 않는다).
검색어를 띄어쓰기로 나눈 뒤, 낱말마다 제목·저자·소개글 중 가장 잘 맞는 곳의 점수를 준다.
"""

import asyncpg

from app.search.filters import build_where
from app.search.schemas import SearchFilters

# 한 번에 가져오는 후보 수. 응답에는 이 중 앞에서부터 size 만큼만 나간다.
# 200 은 다음 단계(#35)에서 벡터 검색 결과와 순위를 합칠 때 쓸 여유분이다.
CANDIDATE_LIMIT = 200
# 검색어는 200자까지라 낱말이 수십 개일 수 있다. 낱말마다 색인을 한 번씩 뒤지므로 묶는다.
MAX_TOKENS = 10

# 어디서 맞았는지에 따른 가중치. 제목 > 저자 > 소개글.
# 소개글은 길어서 흔한 낱말이 우연히 걸리기 쉬우므로 낮게 둔다.
W_TITLE = 3.0
W_AUTHOR = 2.0
W_DESCRIPTION = 0.5
# 검색어 전체가 제목 전체와 비슷할수록 더 올린다. "마음" 을 치면 제목에 마음이 든 책이
# 수십 권인데, 그중 제목이 정확히 『마음』 인 책이 맨 위여야 한다.
# word_similarity 가 아니라 similarity 를 쓴다 — 앞의 것은 "일부와 맞는가" 라 전부 1.0 이 된다.
W_WHOLE_TITLE = 2.0

# 낱말들이 평균 이만큼은 맞아야 결과에 넣는다(낱말별 0~1, 제목·저자에 있으면 1에 가깝다).
# 이 선이 없으면 "꿀벌이 주인공인 그림책" 처럼 낱말이 여럿인 검색어에서 "그림책" 하나만
# 맞은 책 수십 권이 결과를 채운다. 값은 고정 검색어 25개로 0 부터 0.9 까지 재서 골랐다
# (측정 기록은 이슈 #35 코멘트).
MIN_COVERAGE = 0.6
# 소개글에만 있는 낱말은 절반만 쳐 준다. 그래서 소개글에서만 맞은 책은 선을 못 넘는다.
# 그런 책은 다음 단계(#35)의 벡터 검색이 뜻으로 찾는다.
DESCRIPTION_COVERAGE = 0.5
# 색인을 한 번 조회할 때 가져오는 책 수 상한. 흔한 낱말은 수만 권이 걸리는데 그 전부를 채점하면
# 책 수에 비례해 느려진다(266만 권에서 1초가 넘음, #120). 순서 없이 먼저 찾은 만큼만 가져오므로
# 제목이 검색어와 똑같은 책은 따로 찾아 늘 넣는다(_same_title).
LOOKUP_LIMIT = 500

# 후보는 제목·저자의 글자 조각 색인(pg_trgm)으로만 고른다. `<%` 는 낱말이 글 안 어딘가와
# 비슷하면 참이다(기준은 pg_trgm.word_similarity_threshold, 기본 0.6). 조사가 붙은 말
# ("투자를" → "투자")과 가벼운 오타를 잡는다.
#
# 소개글은 후보를 고를 때 보지 않는다. 소개글에서만 맞은 낱말은 0.5 로 치므로, 선(0.6)을
# 넘으려면 적어도 한 낱말은 제목·저자에서 0.6 이상 맞아야 한다. 소개글까지 뒤지면 "때",
# "책" 같은 흔한 낱말이 수만 권을 후보로 끌고 와 13만 권에서 5초가 넘었다(전부 버려질
# 책이다). MIN_COVERAGE 를 0.6 아래로 내리거나 DESCRIPTION_COVERAGE 를 올리면 이 전제가
# 깨지니 같이 고쳐야 한다.
#
# 낱말마다 색인을 따로 조회하고 LOOKUP_LIMIT 권까지만 모은다. 낱말 목록과 책 표를 한 번에
# 조인하면 낱말이 늘 때 PostgreSQL 이 색인을 버리고 표 전체를 훑는다(#120). 그래서 선을 넘을
# 책이 전부 후보가 되지는 않는다. 흔한 낱말에서 순서 없이 잘리는 대신 제목이 검색어와 똑같은
# 책은 늘 넣는다. 고정 표본 300권으로 잰 품질은 전과 같거나 나았다(#120).
#
# 제목은 공백을 뺀 형태와도 비교한다. "메타포워즈" 처럼 붙여 치면 "메타포 워즈" 와 글자
# 조각이 절반만 겹쳐 선을 못 넘는다(이슈 #63). 일반 공백만 빼면 탭·전각공백·nbsp 로 띄어 쓴
# 제목이 남아서, 네 가지를 함께 지운다.
#
# 공백 뺀 제목에는 20260922 마이그레이션의 색인이 있다. 색인은 식이 이것과 같아야 타니,
# 바꾸면 마이그레이션도 같이 고친다. 네 군데에서 쓰므로 여기 한 곳에만 둔다.
_BLANKS = "E' \\t\\u3000\\u00a0'"


def _nospace(expr: str) -> str:
    return f"translate({expr}, {_BLANKS}, '')"


_SQL = """
WITH toks AS (
    SELECT * FROM unnest($1::text[], $2::text[]) AS x(t, pat)
),
{cand},
scored AS (
    SELECT b.book_id, b.title, m.score, m.coverage
    FROM cand
    JOIN v_books b USING (book_id)
    -- 소개글은 길어서 훑는 값이 비싸다. 제목·저자만 먼저 보고, 나머지 낱말이 소개글에서
    -- 전부 맞는다고 쳐도 선을 못 넘는 책은 소개글을 읽기 전에 버린다.
    CROSS JOIN LATERAL (
        SELECT avg(greatest(word_similarity(toks.t, b.title),
                            word_similarity(toks.t, {nospace_b_title}),
                            word_similarity(toks.t, coalesce(b.author, '')),
                            {desc_coverage})) AS best_possible
        FROM toks
    ) pre
    CROSS JOIN LATERAL (
        SELECT
            sum(greatest({w_title} * x.in_title, {w_author} * x.in_author,
                         {w_desc} * x.in_desc)) AS score,
            avg(greatest(x.in_title, x.in_author, {desc_coverage} * x.in_desc)) AS coverage
        FROM (
            SELECT greatest(word_similarity(toks.t, b.title),
                            word_similarity(toks.t, {nospace_b_title})) AS in_title,
                   word_similarity(toks.t, coalesce(b.author, '')) AS in_author,
                   CASE WHEN b.description ILIKE toks.pat THEN 1 ELSE 0 END AS in_desc
            FROM toks
        ) x
    ) m
    WHERE pre.best_possible >= $5
)
SELECT book_id
FROM scored
WHERE coverage >= $5
ORDER BY score + {w_whole} * greatest(
    similarity($3, title),
    similarity({nospace_query}, {nospace_title})
) DESC, book_id
LIMIT $4
"""


def _word_matches(i: int) -> str:
    """i번째 낱말($1[i])이 제목·공백 뺀 제목·저자 중 하나와 비슷하면 참."""
    return (
        f"($1[{i}] <% b.title OR $1[{i}] <% {_nospace('b.title')}"
        f" OR $1[{i}] <% b.author)"
    )


def _same_title(where: str) -> str:
    """제목이 검색어($3)와 공백 빼고 똑같은 책. 공백 뺀 제목 색인으로 찾는다."""
    return (
        f"SELECT b.book_id FROM v_books b"
        f" WHERE {_nospace('b.title')} = {_nospace('$3')}{where}"
    )


def _cand_each_word(n_tokens: int, where: str) -> str:
    """낱말마다 색인을 따로 조회해 LOOKUP_LIMIT 권씩 모으고, 제목이 똑같은 책을 더한다.

    필터(where)는 자르기 전에 건다. 뒤에 걸면 필터에 안 맞는 책이 자리를 먹는다.
    """
    lookups = "\n        UNION ALL\n".join(
        f"        (SELECT b.book_id FROM v_books b"
        f" WHERE {_word_matches(i)}{where} LIMIT {int(LOOKUP_LIMIT)})"
        for i in range(1, n_tokens + 1)
    )
    return (
        "cand AS (\n"
        f"    SELECT book_id FROM (\n{lookups}\n    ) each_word\n"
        f"    UNION\n    {_same_title(where)}\n"
        ")"
    )


def _sql(cand: str) -> str:
    """후보 고르기(cand 절)에 점수·정렬을 붙인 쿼리."""
    return _SQL.format(
        cand=cand,
        w_title=W_TITLE,
        w_author=W_AUTHOR,
        w_desc=W_DESCRIPTION,
        w_whole=W_WHOLE_TITLE,
        desc_coverage=DESCRIPTION_COVERAGE,
        nospace_b_title=_nospace("b.title"),
        nospace_title=_nospace("title"),
        nospace_query=_nospace("$3"),
    )


async def _run(conn: asyncpg.Connection, sql: str, args: list) -> list[asyncpg.Record]:
    """후보 쿼리가 표 전체를 훑지 않게 하고 돌린다.

    `<%` 는 실제로 몇 권이 걸릴지 어림을 못 해(낱말과 상관없이 3% 로 본다) PostgreSQL 이 가끔
    색인 대신 표를 앞에서부터 훑는다. 그러면 느린 데다, 큰 표는 직전 훑기가 멈춘 자리부터
    이어 훑어서 LOOKUP_LIMIT 로 자를 때마다 다른 책이 남는다. 페이지를 넘길 때마다 검색을 다시
    돌리므로 결과가 바뀌면 같은 책이 두 번 나오거나 빠진다. 266만 권에서 432번 중 8번 달라지던
    결과가 이 설정으로 0번이 됐다(#148).
    """
    async with conn.transaction():
        await conn.execute("SET LOCAL enable_seqscan = off")
        return await conn.fetch(sql, *args)


def tokenize(query: str) -> list[str]:
    """띄어쓰기로 나누고, 같은 낱말은 한 번만, 앞에서부터 MAX_TOKENS 개까지.

    글자나 숫자가 하나도 없는 조각(`-`, `·`)은 뺀다. 글자 조각 색인은 글자·숫자로만 조각을
    만들어서, 이런 낱말이 끼면 그 낱말은 색인을 못 타고 표 전체를 훑는다(#147).
    """
    seen: dict[str, None] = {}
    for token in query.split():
        if any(ch.isalnum() for ch in token):
            seen.setdefault(token.lower(), None)
    return list(seen)[:MAX_TOKENS]


def like_pattern(token: str) -> str:
    """ILIKE 에 넣을 '%낱말%'. 낱말 속 %, _ 가 와일드카드로 읽히지 않게 막는다."""
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


_EXACT_TITLE_SQL = f"""
SELECT book_id
FROM v_books
WHERE book_id = ANY($1::int[])
  AND {_nospace("title")} = {_nospace("$2")}
"""


async def exact_title_ids(
    conn: asyncpg.Connection, book_ids: list[int], query: str
) -> set[int]:
    """후보 중 제목이 검색어와 (공백 빼고) 똑같은 책.

    13만 권을 다시 훑지 않고 이미 고른 후보 안에서만 본다. 제목이 검색어와 같은 책은 제목·저자를
    글자 조각으로 훑는 후보 고르기에서 빠질 수 없으므로, 이렇게 해도 놓치는 책이 없다.
    """
    if not book_ids:
        return set()
    rows = await conn.fetch(_EXACT_TITLE_SQL, book_ids, query)
    return {r["book_id"] for r in rows}


async def search_ids(
    conn: asyncpg.Connection,
    query: str,
    filters: SearchFilters,
    limit: int = CANDIDATE_LIMIT,
) -> list[int]:
    """잘 맞는 순서대로 book_id 를 돌려준다."""
    tokens = tokenize(query)
    if not tokens:
        return []
    where, filter_params = build_where(filters, first_param=6)
    args = [
        tokens,
        [like_pattern(t) for t in tokens],
        query.strip(),
        limit,
        MIN_COVERAGE,
        *filter_params,
    ]
    rows = await _run(conn, _sql(_cand_each_word(len(tokens), where)), args)
    return [r["book_id"] for r in rows]
