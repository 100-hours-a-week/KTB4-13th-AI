"""키워드 검색 테스트.

앞부분은 DB 없이 돈다. 뒷부분은 실제 PostgreSQL(pg_trgm·색인 포함)이 있어야 해서
SEARCH_TEST_DATABASE_URL 에 DB 주소를 주면 돈다. 넣은 책은 트랜잭션을 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import os

import asyncpg
import pytest

from app.core import products
from app.search import books, keyword
from app.search.filters import build_where
from app.search.schemas import SearchFilters


def test_띄어쓰기로_나누고_같은_낱말은_한_번만_쓴다() -> None:
    assert keyword.tokenize("  김영하  여행의 이유 김영하 ") == [
        "김영하",
        "여행의",
        "이유",
    ]


def test_낱말은_MAX_TOKENS_개까지만_쓴다() -> None:
    query = " ".join(f"낱말{i}" for i in range(30))

    assert len(keyword.tokenize(query)) == keyword.MAX_TOKENS


def test_문장부호만_있는_낱말은_뺀다() -> None:
    # 글자 조각 색인은 글자·숫자로만 조각을 만든다. `-` 같은 낱말은 색인을 못 타고 표 전체를 훑는다.
    assert keyword.tokenize("- 워크북 : ·") == ["워크북"]


def test_기호가_섞인_낱말은_남긴다() -> None:
    assert keyword.tokenize("C++ (주)현암사") == ["c++", "(주)현암사"]


def test_문장부호만_친_검색어는_DB에_묻지_않고_빈_결과다() -> None:
    # 낱말이 없으면 DB 에 가기 전에 끝난다. 그래서 연결 없이도 돈다.
    assert asyncio.run(keyword.search_ids(None, "- · :", SearchFilters())) == []


def test_문장부호는_낱말_수_한도를_먹지_않는다() -> None:
    words = [f"낱말{i}" for i in range(keyword.MAX_TOKENS)]

    assert keyword.tokenize("- " + " ".join(words)) == words


def test_낱말_속_와일드카드_글자를_막는다() -> None:
    assert keyword.like_pattern("100%_") == "%100\\%\\_%"


def test_필터가_없으면_조건도_없다() -> None:
    assert build_where(SearchFilters(), first_param=5) == ("", [])


def test_필터는_값을_자리표시자로_넘긴다() -> None:
    filters = SearchFilters(category="한국문학", price_max=20000, in_stock_only=True)

    sql, params = build_where(filters, first_param=5)

    # 가격·재고는 상품 표에서 읽는다(#206)
    assert sql == (
        f" AND b.category = $5 AND {products.price_sql('b')} <= $6"
        f" AND {products.in_stock_sql('b')}"
    )
    assert params == ["한국문학", 20000]


def test_온보딩_값은_대응표의_핵심_분류들로_푼다() -> None:
    # 앱은 온보딩과 같은 값("에세이")으로 거른다. 카탈로그 분류명과 글자가 달라 그대로는 0건이다(#219).
    sql, params = build_where(SearchFilters(category="에세이"), first_param=5)

    assert sql == " AND b.category = ANY($5::text[])"
    # 일부 분류(한국문학·문학)는 넣지 않는다. 에세이로 걸렀는데 소설이 섞이면 안 된다(#110).
    assert params == [["강연집·수필집·연설문집", "에세이"]]


def test_분류_하나로_풀리면_같다로_건다() -> None:
    # ANY 로 걸면 분류 + 신간순 색인을 못 골라 느려진다(#219). 여행 → 지리 하나.
    assert build_where(SearchFilters(category="여행"), first_param=5) == (
        " AND b.category = $5",
        ["지리"],
    )


def test_대응표에_없는_값은_지금처럼_정확히_일치하는_것만_거른다() -> None:
    # ③ 챗봇은 LLM 이 준 카탈로그 분류명을 그대로 넘길 수 있다.
    assert build_where(SearchFilters(category="법학"), first_param=5) == (
        " AND b.category = $5",
        ["법학"],
    )


# ---------------------------------------------------------------------------
# 실제 DB 가 필요한 테스트
# ---------------------------------------------------------------------------

# DATABASE_URL 을 그대로 쓰지 않는 이유: conftest 가 .env 없는 환경을 위해 가짜 주소를 넣어 두고,
# 환경변수가 .env 보다 우선이라 로컬에서도 가짜 주소가 읽힌다.
_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

# 실제 카탈로그에 없을 낱말로 책을 만들어, 다른 책이 결과에 끼어들지 않게 한다.
_BOOKS = [
    (9100001, "즈믄가람", "온새미 지음", 12000, True, "에세이", 2024, None),
    (9100002, "즈믄가람 이야기 모음", "다른이", 30000, True, "한국소설", 2010, None),
    (9100003, "딴 제목", "즈믄가람", 15000, False, "에세이", 2022, None),
    (
        9100004,
        "또 다른 제목",
        "아무개",
        9000,
        True,
        "에세이",
        None,
        "즈믄가람 에 대한 소개글",
    ),
    # 영문 제목·저자. 검색어는 소문자로 바꿔 쓰는데 책 제목은 원본 그대로라,
    # 대소문자 때문에 안 걸리는 일이 없는지 본다.
    (
        9100005,
        "Zephyrine Quillfeather",
        "Marlowe Brightwater",
        20000,
        True,
        "영어",
        2023,
        None,
    ),
    # 띄어 쓴 제목. 검색어를 붙여 쳐도 찾는지 본다. 실제 제목과 겹치지 않게 지어낸 낱말이다.
    (9100006, "퀼렌보르 사나톡", "아무개", 11000, True, "한국소설", 2021, None),
    # 전각공백으로 띄어 쓴 제목. 일반 공백만 지우면 붙여 친 검색어와 맞지 않는다.
    (9100007, "도리안토\u3000미르벨", "아무개", 11000, True, "한국소설", 2021, None),
    # 모든 낱말이 든 책을 먼저 찾는지 본다. 둘째 책은 세 낱말 중 둘만 들어 있다.
    (
        9100008,
        "벨로누아 헤스티르 카담네르",
        "아무개",
        10000,
        True,
        "한국소설",
        2020,
        None,
    ),
    (9100009, "벨로누아 헤스티르", "아무개", 10000, True, "한국소설", 2020, None),
]


def _run_in_rollback(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            # 책 표의 가격·재고는 읽지 않는다(#206). 일부러 0원·품절을 넣고 상품 표에 진짜 값을 둔다.
            await conn.executemany(
                "INSERT INTO v_books (book_id, title, author, publisher, price, in_stock,"
                " cover_url, category, pub_year, description)"
                " VALUES ($1, $2, $3, '테스트출판사', 0, false, NULL, $4, $5, $6)",
                [(b[0], b[1], b[2], b[5], b[6], b[7]) for b in _BOOKS],
            )
            await conn.executemany(
                "INSERT INTO v_products (id, book_id, discounted_price, stock_quantity)"
                " VALUES ($1, $2, $3, $4)",
                [(b[0], b[0], b[3], 1 if b[4] else 0) for b in _BOOKS],
            )
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


@needs_db
def test_제목이_정확히_같은_책이_맨_위고_제목_다음_저자_순이다() -> None:
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가람", SearchFilters()))

    assert ids == [9100001, 9100002, 9100003]


@needs_db
def test_제목이_검색어와_같은_책을_골라낸다() -> None:
    # 후보 안에서만 본다. 띄어쓰기만 다른 제목도 같은 것으로 본다(붙여 친 제목, #63).
    exact = _run_in_rollback(
        lambda c: keyword.exact_title_ids(c, [9100001, 9100006], "퀼렌보르사나톡")
    )

    assert exact == {9100006}


@needs_db
def test_제목의_일부만_친_검색어는_완전_일치가_아니다() -> None:
    exact = _run_in_rollback(
        lambda c: keyword.exact_title_ids(c, [9100001, 9100006], "퀼렌보르")
    )

    assert exact == set()


@needs_db
def test_소개글에서만_맞은_책은_키워드_결과에_넣지_않는다() -> None:
    # 그런 책은 뜻으로 찾는 벡터 검색의 몫이다. 넣으면 약하게 맞은 책이 결과를 채운다.
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가람", SearchFilters()))

    assert 9100004 not in ids


@needs_db
def test_낱말이_일부만_맞은_책은_넣지_않는다() -> None:
    ids = _run_in_rollback(
        lambda c: keyword.search_ids(
            c, "즈믄가람 없는말 또없는말 다른말", SearchFilters()
        )
    )

    assert ids == []


@needs_db
def test_오타가_있어도_찾는다() -> None:
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가랑", SearchFilters()))

    assert 9100001 in ids


@needs_db
def test_띄어_쓴_제목을_붙여_쳐도_찾는다() -> None:
    ids = _run_in_rollback(
        lambda c: keyword.search_ids(c, "퀼렌보르사나톡", SearchFilters())
    )

    assert ids == [9100006]


@needs_db
def test_전각공백으로_띄어_쓴_제목도_붙여_쳐서_찾는다() -> None:
    ids = _run_in_rollback(
        lambda c: keyword.search_ids(c, "도리안토미르벨", SearchFilters())
    )

    assert ids == [9100007]


@needs_db
@pytest.mark.parametrize(
    "query",
    ["zephyrine quillfeather", "ZEPHYRINE QUILLFEATHER", "Zephyrine Quillfeather"],
)
def test_영문_제목은_대소문자를_어떻게_쳐도_찾는다(query: str) -> None:
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, query, SearchFilters()))

    assert ids == [9100005]


@needs_db
def test_영문_저자도_소문자로_찾는다() -> None:
    ids = _run_in_rollback(
        lambda c: keyword.search_ids(c, "marlowe brightwater", SearchFilters())
    )

    assert ids == [9100005]


@needs_db
def test_제목과_저자를_섞어_쳐도_둘_다_맞는_책이_위다() -> None:
    ids = _run_in_rollback(
        lambda c: keyword.search_ids(c, "온새미 즈믄가람", SearchFilters())
    )

    assert ids[0] == 9100001


@needs_db
@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        (SearchFilters(category="한국소설"), [9100002]),
        # 온보딩 값은 대응표로 풀어 거른다(#219). 한국소설은 "소설"의 핵심 분류다.
        (SearchFilters(category="소설"), [9100002]),
        (SearchFilters(price_min=10000, price_max=20000), [9100001, 9100003]),
        (SearchFilters(pub_year_from=2020), [9100001, 9100003]),
        (SearchFilters(in_stock_only=True), [9100001, 9100002]),
    ],
)
def test_필터를_적용한다(filters: SearchFilters, expected: list[int]) -> None:
    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가람", filters))

    assert ids == expected


@needs_db
def test_후보_상한에_걸려도_제목이_똑같은_책은_맨_위다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 즈믄가람이 든 책은 셋인데 한 번에 한 권만 가져오게 줄인다. 어느 책이 그 자리를 차지하든,
    # 제목이 검색어와 똑같은 책은 따로 찾아 넣으므로 맨 위에 있어야 한다.
    monkeypatch.setattr(keyword, "LOOKUP_LIMIT", 1)

    ids = _run_in_rollback(lambda c: keyword.search_ids(c, "즈믄가람", SearchFilters()))

    assert ids[0] == 9100001
    assert len(ids) <= 2


@needs_db
def test_후보_상한은_필터를_건_뒤에_건다(monkeypatch: pytest.MonkeyPatch) -> None:
    # 필터보다 먼저 자르면 소설이 아닌 책이 한 자리를 먹고 필터에서 빠져 결과가 빈다.
    monkeypatch.setattr(keyword, "LOOKUP_LIMIT", 1)

    ids = _run_in_rollback(
        lambda c: keyword.search_ids(c, "즈믄가람", SearchFilters(category="소설"))
    )

    assert ids == [9100002]


def _explain(query: str, cand_sql) -> str:
    """search_ids 와 같은 설정(_run)으로 후보 쿼리의 실행 계획을 받는다."""

    async def _go(conn: asyncpg.Connection) -> str:
        tokens = keyword.tokenize(query)
        where, params = build_where(SearchFilters(), first_param=6)
        sql = "EXPLAIN " + keyword._sql(cand_sql(len(tokens), where))
        args = [
            tokens,
            [keyword.like_pattern(t) for t in tokens],
            query,
            keyword.CANDIDATE_LIMIT,
            keyword.MIN_COVERAGE,
            *params,
        ]
        rows = await keyword._run(conn, sql, args)
        return "\n".join(r[0] for r in rows)

    return _run_in_rollback(_go)


@needs_db
@pytest.mark.parametrize("query", ["즈믄가람", "즈믄가람 이야기 모음"])
def test_낱말마다_찾을_때_표_전체를_훑지_않는다(query: str) -> None:
    # 표 전체를 훑으면 느린 데다, 상한으로 자를 때마다 다른 책이 남아 페이지가 어긋난다(#148).
    assert "Seq Scan" not in _explain(query, keyword._cand_each_word)


@needs_db
def test_모든_낱말이_든_책이_충분하면_넓히지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 기준을 1권으로 낮춘다. 세 낱말이 모두 든 책이 한 권 있으니 넓히지 않고, 둘만 든 책은 빠진다.
    monkeypatch.setattr(keyword, "MIN_ALL_WORDS", 1)

    ids = _run_in_rollback(
        lambda c: keyword.search_ids(c, "벨로누아 헤스티르 카담네르", SearchFilters())
    )

    assert ids == [9100008]


@needs_db
def test_모든_낱말이_든_책이_모자라면_낱말마다_넓힌다() -> None:
    # 기본 기준(10권)보다 적으니 넓힌다. 둘만 든 책도 선(0.6)을 넘어 뒤에 붙는다.
    ids = _run_in_rollback(
        lambda c: keyword.search_ids(c, "벨로누아 헤스티르 카담네르", SearchFilters())
    )

    assert ids == [9100008, 9100009]


@needs_db
@pytest.mark.parametrize("query", ["즈믄가람 이야기", "즈믄가람 이야기 모음"])
def test_모든_낱말을_찾을_때도_표_전체를_훑지_않는다(query: str) -> None:
    assert "Seq Scan" not in _explain(query, keyword._cand_all_words)


@needs_db
def test_책_정보는_받은_순서대로_돌려주고_없는_책은_빠진다() -> None:
    rows = _run_in_rollback(lambda c: books.fetch(c, [9100003, 123456789, 9100001]))

    assert [r["book_id"] for r in rows] == [9100003, 9100001]
    assert rows[0] == {
        "book_id": 9100003,
        "title": "딴 제목",
        "author": "즈믄가람",
        "publisher": "테스트출판사",
        "price": 15000,
        "in_stock": False,
        "cover_url": None,
    }


@pytest.mark.parametrize(
    ("n_tokens", "limit"), [(1, 500), (2, 500), (3, 333), (5, 200), (10, 100)]
)
def test_낱말이_많을수록_낱말마다_모으는_권수를_줄인다(
    n_tokens: int, limit: int
) -> None:
    # 문장으로 친 검색어는 후보가 낱말 수 × 500권까지 늘어 채점이 느렸다(#198). 합계를 1,000권으로 나눈다.
    assert keyword.lookup_limit(n_tokens) == limit


def test_낱말마다_넓힐_때_나눈_권수로_자른다() -> None:
    sql = keyword._cand_each_word(5, "")

    assert sql.count("LIMIT 200)") == 5
    assert f"LIMIT {keyword.LOOKUP_LIMIT})" not in sql


def test_모든_낱말이_든_책을_먼저_찾을_때는_줄이지_않는다() -> None:
    # 이 경로는 결과가 적어 싸다. 줄이면 제목이 긴 책을 찾을 때 후보만 줄어든다.
    assert f"LIMIT {keyword.LOOKUP_LIMIT}" in keyword._cand_all_words(5, "")
