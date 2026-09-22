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

# 후보는 제목·저자의 글자 조각 색인(pg_trgm)으로만 고른다. `<%` 는 낱말이 글 안 어딘가와
# 비슷하면 참이다(기준은 pg_trgm.word_similarity_threshold, 기본 0.6). 조사가 붙은 말
# ("투자를" → "투자")과 가벼운 오타를 잡는다.
#
# 소개글은 후보를 고를 때 보지 않는다. 소개글에서만 맞은 낱말은 0.5 로 치므로, 선(0.6)을
# 넘으려면 적어도 한 낱말은 제목·저자에서 0.6 이상 맞아야 한다 — 즉 통과할 책은 전부 이
# 조건에 걸린다. 소개글까지 뒤지면 "때", "책" 같은 흔한 낱말이 수만 권을 후보로 끌고 와
# 13만 권에서 5초가 넘었다(전부 버려질 책이다). MIN_COVERAGE 를 0.6 아래로 내리거나
# DESCRIPTION_COVERAGE 를 올리면 이 전제가 깨지니 같이 고쳐야 한다.
#
# 제목은 공백을 뺀 형태와도 비교한다. "메타포워즈" 처럼 붙여 치면 "메타포 워즈" 와 글자
# 조각이 절반만 겹쳐 선을 못 넘는다(이슈 #63). 공백 뺀 제목에는 004 마이그레이션의 색인이
# 있다. 색인은 식이 `replace(title, ' ', '')` 와 글자까지 같아야 타니 식을 바꾸면 같이 고친다.
_SQL = """
WITH toks AS (
    SELECT * FROM unnest($1::text[], $2::text[]) AS x(t, pat)
),
cand AS (
    SELECT DISTINCT b.book_id
    FROM toks, v_books b
    WHERE (
        toks.t <% b.title
        OR toks.t <% replace(b.title, ' ', '')
        OR toks.t <% b.author
    ){where}
),
scored AS (
    SELECT b.book_id, b.title, m.score, m.coverage
    FROM cand
    JOIN v_books b USING (book_id)
    -- 소개글은 길어서 훑는 값이 비싸다. 제목·저자만 먼저 보고, 나머지 낱말이 소개글에서
    -- 전부 맞는다고 쳐도 선을 못 넘는 책은 소개글을 읽기 전에 버린다.
    CROSS JOIN LATERAL (
        SELECT avg(greatest(word_similarity(toks.t, b.title),
                            word_similarity(toks.t, replace(b.title, ' ', '')),
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
                            word_similarity(toks.t, replace(b.title, ' ', ''))) AS in_title,
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
    similarity(replace($3, ' ', ''), replace(title, ' ', ''))
) DESC, book_id
LIMIT $4
"""


def tokenize(query: str) -> list[str]:
    """띄어쓰기로 나누고, 같은 낱말은 한 번만, 앞에서부터 MAX_TOKENS 개까지."""
    seen: dict[str, None] = {}
    for token in query.split():
        seen.setdefault(token.lower(), None)
    return list(seen)[:MAX_TOKENS]


def like_pattern(token: str) -> str:
    """ILIKE 에 넣을 '%낱말%'. 낱말 속 %, _ 가 와일드카드로 읽히지 않게 막는다."""
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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
    sql = _SQL.format(
        where=where,
        w_title=W_TITLE,
        w_author=W_AUTHOR,
        w_desc=W_DESCRIPTION,
        w_whole=W_WHOLE_TITLE,
        desc_coverage=DESCRIPTION_COVERAGE,
    )
    patterns = [like_pattern(t) for t in tokens]
    rows = await conn.fetch(
        sql, tokens, patterns, query.strip(), limit, MIN_COVERAGE, *filter_params
    )
    return [r["book_id"] for r in rows]
