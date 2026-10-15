"""④ 관심사마다 책 뽑기(app/feed/interest_picks.py) 테스트(#449, #450).

"실제 DB" 절의 테스트만 PostgreSQL 이 필요하다(SEARCH_TEST_DATABASE_URL).
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass

import asyncpg
import pytest

from app.core.pgvector import to_vector_literal
from app.feed import interest_picks
from app.feed.personalized import EXACT_LIMIT, FALLBACK_LIMIT

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
DIM = 384

# --- 어느 분류에서 뽑나 ---


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("여행", ("지리",)),  # 카테고리는 그 카테고리의 분류
        ("우주", ("천문학",)),  # 세부 카테고리 → 도서관 분류 대응표
        ("요리", ("생활과학", "오락·스포츠")),  # 대응표에 없으면 속한 카테고리의 분류
        ("어린이", None),  # 분류로 좁힐 수 없는 카테고리
        ("그림책", None),  # 대응표가 "거르지 않음"인 세부 카테고리
        ("어린이 지식·교양", None),  # 속한 카테고리를 분류로 좁힐 수 없다
        ("의학", None),  # 어느 카테고리에도 속하지 않는다
    ],
)
def test_관심사를_뽑을_도서관_분류(name: str, expected: tuple[str, ...] | None) -> None:
    assert interest_picks.classes_of(name) == expected


def test_추리_스릴러는_소설의_분류_안에서_뽑는다() -> None:
    assert interest_picks.classes_of("추리/스릴러") == interest_picks.classes_of("소설")


def test_번호가_이_관심사로_이어지는_책을_앞에_둔다() -> None:
    books = [(1, "598"), (2, "594.5"), (3, None), (4, "594")]  # 육아, 요리, 모름, 요리

    ordered = interest_picks.numbered_first("요리", books, lambda book: book[1])

    assert [book_id for book_id, _ in ordered] == [2, 4, 1, 3]


# --- 가짜 연결로 뽑는 방법 보기 ---


def _row(book_id: int, distance: float, class_no: str | None = None) -> dict:
    return {
        "book_id": book_id,
        "isbn13": None,
        "title": "책",
        "author": None,
        "category": "생활과학",
        "class_no": class_no,
        "distance": distance,
    }


class _FakeConn:
    """분류 안의 책 수(count)와, 거리를 전부 계산할 때(rows)·벡터 색인으로 뽑을 때(index_rows) 돌려줄 줄."""

    def __init__(
        self,
        count: int = 0,
        rows: list[dict] | None = None,
        index_rows: list[dict] | None = None,
    ) -> None:
        self.count = count
        self.rows = rows or []
        self.index_rows = self.rows if index_rows is None else index_rows
        self.estimated = False
        self.ran: list[str] = []
        self.sqls: list[str] = []
        self.args: list[tuple] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    async def execute(self, sql: str) -> None:
        pass

    async def fetchval(self, sql: str, *args, timeout: float):
        assert sql.startswith("EXPLAIN (FORMAT JSON)")
        self.estimated = True
        return json.dumps([{"Plan": {"Plan Rows": self.count}}])

    async def fetch(self, sql: str, *args, timeout: float):
        self.sqls.append(sql)
        self.args.append(args)
        if "+ 0" in sql:
            self.ran.append("exact")
            return self.rows
        self.ran.append("index")
        return self.index_rows


def _pick(
    conn: _FakeConn,
    name: str = "요리",
    vector: list[float] | None = None,
    *,
    by_name: bool = True,
    limit: int = 3,
    left=lambda: 1.0,
) -> list[interest_picks.Pick]:
    return asyncio.run(
        interest_picks.pick(
            conn,
            name,
            vector or [1.0, 0.0],
            by_name=by_name,
            limit=limit,
            left=left,
        )
    )


def test_분류가_없는_관심사는_세지_않고_전체에서_벡터_색인으로_뽑는다() -> None:
    conn = _FakeConn(rows=[_row(1, 0.1)])

    _pick(conn, "그림책")

    assert not conn.estimated
    assert conn.ran == ["index"]
    assert "WHERE b.category" not in conn.sqls[0]


def test_분류_안의_책이_적으면_거리를_전부_계산한다() -> None:
    conn = _FakeConn(count=EXACT_LIMIT, rows=[_row(1, 0.1)])

    _pick(conn)

    assert conn.ran == ["exact"]


def test_분류_안의_책이_많으면_벡터_색인으로_뽑고_모자라면_정확히_다시_뽑는다() -> None:
    conn = _FakeConn(count=EXACT_LIMIT + 1, rows=[_row(1, 0.1)], index_rows=[])

    _pick(conn)

    assert conn.ran == ["index", "exact"]


def test_분류_안의_책이_너무_많으면_모자라도_다시_뽑지_않는다() -> None:
    conn = _FakeConn(count=FALLBACK_LIMIT + 1, rows=[_row(1, 0.1)], index_rows=[])

    _pick(conn)

    assert conn.ran == ["index"]


def test_이름_벡터면_코사인을_이름_벡터의_구간으로_편다() -> None:
    # 코사인 0.84 는 이름 벡터 구간(0.80–0.88)의 가운데다
    picks = _pick(_FakeConn(rows=[_row(1, 0.16)]), by_name=True)

    assert picks[0].part == pytest.approx(0.5)


def test_책_벡터_평균이면_코사인에_평균의_길이를_곱해_편다() -> None:
    # 코사인 0.9 × 길이 0.95 = 0.855 → 책 벡터 평균 구간(0.82–0.90)에서 0.4375
    picks = _pick(_FakeConn(rows=[_row(1, 0.1)]), vector=[0.95, 0.0], by_name=False)

    assert picks[0].part == pytest.approx(0.4375)


def test_시간을_다_쓰면_질의를_보내지_않고_실패한다() -> None:
    def spent() -> float:
        raise TimeoutError

    conn = _FakeConn(count=EXACT_LIMIT, rows=[_row(1, 0.1)])

    with pytest.raises(TimeoutError):
        _pick(conn, left=spent)

    assert not conn.estimated
    assert conn.ran == []


# --- 작가 관심사 (#450) ---


def _author_row(book_id: int, title: str) -> dict:
    return {
        "book_id": book_id,
        "isbn13": None,
        "title": title,
        "author": "한강",
        "category": "한국소설",
    }


def _pick_author(conn: _FakeConn, name: str, limit: int = 3) -> list:
    return asyncio.run(
        interest_picks.pick_author(conn, name, limit=limit, left=lambda: 1.0)
    )


def test_작가_관심사는_같은_제목의_판본을_하나만_두고_유사도_항은_1이다() -> None:
    rows = [
        _author_row(1, "해리 포터"),
        _author_row(2, "해리포터"),  # 띄어쓰기만 다른 판본
        _author_row(3, "다른 책"),
    ]

    picks = _pick_author(_FakeConn(rows=rows), "작가:한강")

    assert [p.book_id for p in picks] == [1, 3]
    assert [p.part for p in picks] == [1.0, 1.0]


def test_작가_이름으로_찾고_판본을_걸러도_모자라지_않게_넉넉히_읽는다() -> None:
    conn = _FakeConn()

    _pick_author(conn, "작가:J.K.  롤링", limit=10)

    assert conn.args == [("J.K. 롤링", 10 * interest_picks.AUTHOR_READ_FACTOR)]


def test_작가_이름이_비면_조회하지_않는다() -> None:
    conn = _FakeConn()

    assert _pick_author(conn, "작가: ") == []
    assert conn.sqls == []


# --- 키워드 관심사 (#450) ---


def _keyword_row(book_id: int, distance: float, mentioned: bool) -> dict:
    return {**_row(book_id, distance), "mentioned": mentioned}


def test_키워드_관심사는_그_말이_든_책을_앞에_두고_나머지는_가까운_순이다() -> None:
    rows = [
        _keyword_row(1, 0.10, False),
        _keyword_row(2, 0.12, True),
        _keyword_row(3, 0.13, False),
        _keyword_row(4, 0.16, True),
    ]
    conn = _FakeConn(rows=rows)

    picks = asyncio.run(
        interest_picks.pick_keyword(
            conn, "키워드:Cat", [1.0, 0.0], limit=4, left=lambda: 1.0
        )
    )

    assert [p.book_id for p in picks] == [2, 4, 1, 3]
    # 핵심어의 이름 벡터라 이름 벡터의 구간으로 편다. 코사인 0.84 는 가운데다
    assert picks[1].part == pytest.approx(0.5)
    # 영어는 대소문자를 가리지 않게 소문자로 견준다
    assert conn.args[0][2] == "cat"


# --- 실제 DB ---


def _unit_like(cosine: float) -> list[float]:
    """0번 축과의 코사인이 cosine 인 길이 1 벡터. 실제 책 벡터는 한 축과 이만큼 가깝지 않다."""
    vector = [0.0] * DIM
    vector[0] = cosine
    vector[1] = (1 - cosine**2) ** 0.5
    return vector


@dataclass(frozen=True)
class _Book:
    book_id: int
    category: str = "생활과학"
    class_no: str | None = None
    cosine: float = 0.5  # 0번 축과의 코사인
    title: str = "책"
    author: str = "저자"
    pub_year: int = 2024
    description: str = "소개"


async def _with_books(books: list[_Book], check):
    conn = await asyncpg.connect(_DB_URL)
    tx = conn.transaction()
    await tx.start()
    try:
        await conn.execute(
            "CREATE TEMP TABLE book_classes (isbn13 text PRIMARY KEY, class_no text)"
        )
        for book in books:
            isbn = str(9780000000000 + book.book_id)
            await conn.execute(
                "INSERT INTO v_books (book_id, title, author, publisher, price,"
                " in_stock, cover_url, category, pub_year, description, isbn13)"
                " VALUES ($1, $2, $3, '출판사', 10000, true, NULL, $4, $5, $6, $7)",
                book.book_id,
                book.title,
                book.author,
                book.category,
                book.pub_year,
                book.description,
                isbn,
            )
            await conn.execute(
                "INSERT INTO book_embeddings VALUES ($1, $2::vector, $3, 'test')",
                book.book_id,
                to_vector_literal(_unit_like(book.cosine)),
                DIM,
            )
            if book.class_no:
                await conn.execute(
                    "INSERT INTO book_classes VALUES ($1, $2)", isbn, book.class_no
                )
        return await check(conn)
    finally:
        await tx.rollback()
        await conn.close()


_needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


@_needs_db
def test_분류_안에서_가까운_책을_뽑고_번호가_이어지는_책을_앞에_둔다() -> None:
    books = [
        _Book(9100301, class_no="598", cosine=1.0),  # 가장 가깝지만 육아 번호
        _Book(9100302, class_no="594.5", cosine=0.95),  # 요리 번호
        _Book(9100303, category="오락·스포츠", cosine=0.84),  # 번호를 모른다
        # 요리 번호지만 요리의 분류가 아니다
        _Book(9100304, category="에세이", class_no="594", cosine=1.0),
    ]

    async def check(conn):
        return await interest_picks.pick(
            conn,
            "요리",
            _unit_like(1.0),
            by_name=True,
            limit=3,
            left=lambda: 5.0,
        )

    picks = asyncio.run(_with_books(books, check))

    # 에세이 책은 더 가까워도 요리의 분류가 아니라 뽑지 않는다
    assert [p.book_id for p in picks] == [9100302, 9100301, 9100303]
    assert [p.part for p in picks] == pytest.approx([1.0, 1.0, 0.5])
    assert picks[0].category == "생활과학"


@_needs_db
def test_저자_칸에_이름이_낱말로_든_책만_새_책부터_뽑는다() -> None:
    # 실제 카탈로그에 없을 이름을 쓴다
    books = [
        _Book(9100311, author="쀍뷁", title="첫 책", pub_year=2020),
        _Book(9100312, author="지은이: 쀍뷁", title="둘째 책", pub_year=2023),
        _Book(9100313, author="홍길동, 쀍뷁 (옮긴이)", title="셋째 책", pub_year=2022),
        _Book(9100314, author="쀍뷁꿻", title="다른 작가", pub_year=2024),
        _Book(9100315, author="쀍뷁", title="둘째  책!", pub_year=2021),  # 판본
    ]

    async def check(conn):
        return await interest_picks.pick_author(
            conn, "작가:쀍뷁", limit=10, left=lambda: 5.0
        )

    picks = asyncio.run(_with_books(books, check))

    assert [p.book_id for p in picks] == [9100312, 9100313, 9100311]


@_needs_db
def test_키워드는_가까운_책_가운데_제목이나_소개글에_그_말이_든_책을_앞에_둔다() -> (
    None
):
    books = [
        _Book(9100321, cosine=1.0),
        _Book(9100322, cosine=0.95, title="쀍뷁 이야기"),
        _Book(9100323, cosine=0.9, description="쀍뷁이 나오는 소설"),
    ]

    async def check(conn):
        # 벡터 색인은 어림이라 실제 책과 동떨어진 테스트 벡터를 놓칠 때가 있다. 조회 글을 보려는 테스트라
        # 색인을 끄고 거리를 전부 계산한다
        await conn.execute("SET LOCAL enable_indexscan = off")
        return await interest_picks.pick_keyword(
            conn, "키워드:쀍뷁", _unit_like(1.0), limit=3, left=lambda: 5.0
        )

    picks = asyncio.run(_with_books(books, check))

    assert [p.book_id for p in picks] == [9100322, 9100323, 9100321]
