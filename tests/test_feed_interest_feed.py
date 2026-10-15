"""④ 관심사별 홈 추천 목록 뽑기(app/feed/interest_feed.py) 테스트(#452).

마지막 테스트만 실제 PostgreSQL 이 필요하다(SEARCH_TEST_DATABASE_URL).
"""

import asyncio
import os

import asyncpg
import pytest

from app.core.pgvector import to_vector_literal
from app.feed import interest_feed, interest_picks
from app.feed.interest_picks import Pick

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
DIM = 384

# --- 관심사마다 검색어 정하기 ---


def test_자리를_많이_받은_관심사부터_종류에_맞는_검색어로_뽑는다() -> None:
    seats = {"요리": 30, "작가:한강": 100, "키워드:고양이": 50, "SF": 3}
    book_vectors = {"요리": [0.5]}
    keyword_vectors = {"키워드:고양이": [0.7]}

    jobs = interest_feed.jobs(seats, book_vectors, keyword_vectors, lambda _: [0.9])

    assert [(j.name, j.vector, j.by_name) for j in jobs] == [
        ("작가:한강", None, False),  # 작가는 저자 칸으로 뽑아 검색어가 없다
        ("키워드:고양이", [0.7], True),
        ("요리", [0.5], False),  # 산 책이 있으면 책 벡터 평균
        ("SF", [0.9], True),  # 없으면 이름 벡터
    ]
    # 받은 자리의 두 배를 뽑되, 자리가 적어도 PICK_MIN 권은 뽑는다
    assert [j.limit for j in jobs] == [200, 100, 60, interest_feed.PICK_MIN]


# --- 목록에 둘 책 ---


def _pick(
    book_id: int,
    *,
    isbn: str | None = None,
    title: str = "책",
    author: str = "저자",
    category: str = "생활과학",
) -> Pick:
    return Pick(book_id, isbn, title, author, category, 0.5)


def _keep(negatives: set[str], facts: dict | None = None, **owned) -> object:
    return interest_feed._keeper(
        owned.get("isbns", set()),
        owned.get("editions", set()),
        negatives,
        facts or {},
        lambda _name: None,
    )


def test_가진_책과_그_다른_판본은_뺀다() -> None:
    keep = _keep(set(), isbns={"9780000000001"}, editions={("해리포터", "jk롤링")})

    assert not keep(_pick(1, isbn="9780000000001"))
    assert not keep(_pick(2, title="해리 포터", author="J.K. 롤링 지음"))
    assert keep(_pick(3, title="해리 포터", author="다른 사람"))


@pytest.mark.parametrize(
    ("negatives", "pick", "class_no"),
    [
        ({"육아"}, _pick(1), "598"),  # 세부 분류 번호로 가른 관심사가 음수
        ({"라이프스타일"}, _pick(1), "594.5"),  # 요리가 속한 카테고리가 음수
        (
            {"라이프스타일"},
            _pick(1, category="오락·스포츠"),
            None,
        ),  # 그 카테고리의 분류
        ({"작가:한강"}, _pick(1, author="한강, 데보라 스미스"), None),  # 작가가 음수
    ],
)
def test_음수인_관심사의_책은_뺀다(
    negatives: set[str], pick: Pick, class_no: str | None
) -> None:
    keep = _keep(negatives, {1: {"class_no": class_no}})

    assert not keep(pick)


def test_음수인_관심사와_다른_책은_남긴다() -> None:
    keep = _keep({"육아", "작가:한강"}, {1: {"class_no": "594.5"}})

    # 요리 책이고, 이름의 일부만 같은 작가는 다른 사람이다
    assert keep(_pick(1, author="한강준"))


# --- 실제 DB ---


def _near(cosine: float) -> list[float]:
    """0번 축과의 코사인이 cosine 인 길이 1 벡터. 실제 책 벡터는 한 축과 이만큼 가깝지 않다."""
    vector = [0.0] * DIM
    vector[0] = cosine
    vector[1] = (1 - cosine**2) ** 0.5
    return vector


# book_id → (도서관 분류, 제목, 저자, 출간연도, 세부 분류 번호, 0번 축과의 코사인)
_BOOKS = {
    9100401: ("쀍분류", "가진 책", "가", 2025, "594", 1.0),
    9100402: ("쀍분류", "다른 판본", "나", 2025, "594", 0.99),
    9100403: ("쀍분류", "오래된 책", "다", 2020, "594", 0.98),
    9100404: ("쀍분류", "새 책", "라", 2025, "594", 0.97),
    9100405: ("쀍분류", "육아 책", "마", 2025, "598", 0.96),
    9100406: ("에세이", "다른 판본", "나", 2024, None, 0.1),  # 가진 책(분류 밖)
}


def _isbn(book_id: int) -> str:
    return str(9780000000000 + book_id)


@pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)
def test_가진_책과_싫다고_한_관심사의_책을_빼고_조건을_지키는_책부터_놓는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 실제 분류에는 실제 책이 많다. 테스트 책만 든 분류를 요리의 분류로 친다
    monkeypatch.setattr(interest_picks, "classes_of", lambda _name: ("쀍분류",))

    async def run():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(
                "CREATE TEMP TABLE book_classes"
                " (isbn13 text PRIMARY KEY, class_no text, page_count int)"
            )
            for book_id, (cat, title, author, year, cls, cos) in _BOOKS.items():
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, author, publisher, price,"
                    " in_stock, cover_url, category, pub_year, description, isbn13)"
                    " VALUES ($1, $2, $3, '출판사', 10000, true, NULL, $4, $5, '소개', $6)",
                    book_id,
                    title,
                    author,
                    cat,
                    year,
                    _isbn(book_id),
                )
                await conn.execute(
                    "INSERT INTO book_embeddings VALUES ($1, $2::vector, $3, 'test')",
                    book_id,
                    to_vector_literal(_near(cos)),
                    DIM,
                )
                await conn.execute(
                    "INSERT INTO book_classes VALUES ($1, $2, NULL)",
                    _isbn(book_id),
                    cls,
                )
            return await interest_feed.build(
                conn,
                {"요리": 2.0, "육아": -2.0},
                {},
                {},
                ["2024년 이후"],
                {_isbn(9100401), _isbn(9100406)},
                name_vector=lambda name: _near(1.0) if name == "요리" else None,
                total=2,
            )
        finally:
            await tx.rollback()
            await conn.close()

    feed = asyncio.run(run())

    # 가진 책(401), 가진 책의 다른 판본(402), 음수인 육아의 책(405)을 빼고, 2024년 이후 책을 앞에 둔다
    assert [book.book_id for book in feed.books] == [9100404, 9100403]
    assert {book.interest for book in feed.books} == {"요리"}
    assert feed.substituted == ()
