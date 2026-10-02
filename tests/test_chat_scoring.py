"""③ get_candidates()가 match_score를 채우는 부분(_attach_match_scores) 테스트.

라우터 계약 테스트(tests/test_chat_router.py)와 후보 조립 테스트
(tests/test_chat_candidates.py)는 이 함수를 가짜로 바꿔 끼운다. 여기서는 그
안쪽 — 카테고리·인기 점수를 실제 DB에서 읽어 app.feed.scoring.match_score와
합치는 부분을 본다. app.feed의 함수들과 같은 방식으로 conn을 직접 받으므로
(tests/test_feed_rule_only.py 참고) 트랜잭션 하나로 묶어 흔적 없이 돌린다.
실제 PostgreSQL이 있어야 돈다.
"""

import asyncio
import json
import os

import asyncpg
import pytest

from app.chat.schemas import Spec, SpecExact
from app.core.pgvector import to_vector_literal
from app.routers import chat
from app.search.schemas import SearchFilters

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER = 9_100_901
_LIKED = "채점테스트좋아함"
_OTHER = "채점테스트다른분류"
DIM = 384


def _vector(first: float) -> list[float]:
    """첫 칸만 다른 단위 벡터(app/feed/personalized.py 테스트와 같은 방식).

    두 단위 벡터를 이렇게 만들면 내적(코사인 유사도)이 정확히 first가 된다 —
    유사도를 원하는 값으로 쉽게 만드는 용도.
    """
    rest = (1 - first**2) ** 0.5
    vector = [0.0] * DIM
    vector[0] = first
    vector[1] = rest
    return vector


def _run(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


async def _insert_book(
    conn: asyncpg.Connection, book_id: int, category: str | None
) -> None:
    await conn.execute(
        "INSERT INTO v_books (book_id, title, author, publisher, price,"
        " in_stock, cover_url, category, pub_year, description)"
        " VALUES ($1, '책', '저자', '출판사', 10000, true, NULL, $2, 2020, '소개')",
        book_id,
        category,
    )


def _candidates(*book_ids: int) -> list[dict]:
    return [{"book_id": book_id} for book_id in book_ids]


def _spec(semantic: str | None = None) -> Spec:
    """semantic이 없으면(기본값) 옛 식(취향+카테고리+인기)으로 폴백하는 spec.

    기존 테스트 대부분은 이 폴백 경로를 그대로 본다 — #222 이전과 같은 계산이라
    기존 기대값이 안 바뀐다.
    """
    return Spec(
        intent="semantic" if semantic else "exact",
        exact=SpecExact(title=None, author=None, publisher=None),
        filters=SearchFilters(),
        semantic=semantic,
        anchor_book=None,
        exclude=[],
    )


def test_취향_프로필의_카테고리_점수를_반영한다() -> None:
    async def check(conn):
        await _insert_book(conn, 9100901, _LIKED)
        await conn.execute(
            "INSERT INTO taste_profile (user_id, tag_weights, cold_start, profile_version)"
            " VALUES ($1, $2::jsonb, true, 1)",
            _USER,
            json.dumps({_LIKED: 4}, ensure_ascii=False),
        )
        candidates = _candidates(9100901)
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    candidates = _run(check)
    # 원점수 4는 CATEGORY_SATURATION(4)에서 1.0으로 펴져 카테고리 항(0.25)을 다 받는다.
    # 인기 집계 행이 없어 인기 항은 0, 유사도 항은 애초에 없다(with_similarity=False).
    assert candidates[0]["match_score"] == 25
    assert candidates[0]["popularity"] == 0


def test_결이_다른_분류면_카테고리_점수가_안_붙는다() -> None:
    async def check(conn):
        await _insert_book(conn, 9100902, _OTHER)
        await conn.execute(
            "INSERT INTO taste_profile (user_id, tag_weights, cold_start, profile_version)"
            " VALUES ($1, $2::jsonb, true, 1)",
            _USER,
            json.dumps({_LIKED: 4}, ensure_ascii=False),
        )
        candidates = _candidates(9100902)
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    candidates = _run(check)
    assert candidates[0]["match_score"] == 0


def test_취향_프로필이_없으면_인기_점수만_반영한다() -> None:
    async def check(conn):
        await _insert_book(conn, 9100903, _OTHER)
        await conn.execute(
            "INSERT INTO v_book_popularity (book_id, sales, rating_avg, rating_count, as_of)"
            " VALUES ($1, 1000, NULL, 0, now())",
            9100903,
        )
        candidates = _candidates(9100903)
        # taste_profile 행을 아예 안 만든다 — 프로필 없는 사용자.
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    candidates = _run(check)
    assert candidates[0]["popularity"] > 0
    # 카테고리 재료가 전혀 없어 CATEGORY_WEIGHT(25)만큼은 못 붙고, POPULARITY_WEIGHT(15)
    # 이하만 나올 수 있다.
    assert 0 < candidates[0]["match_score"] <= 15


def test_인기_집계가_없는_책은_0점_재료로_본다() -> None:
    async def check(conn):
        await _insert_book(conn, 9100904, None)
        candidates = _candidates(9100904)
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    candidates = _run(check)
    assert candidates[0]["popularity"] == 0
    assert candidates[0]["match_score"] == 0


def test_후보가_없으면_아무_일도_안_한다() -> None:
    async def check(conn):
        candidates: list[dict] = []
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    assert _run(check) == []


async def _insert_embedding(
    conn: asyncpg.Connection, book_id: int, first: float
) -> None:
    await conn.execute(
        "INSERT INTO book_embeddings VALUES ($1, $2::vector, $3, 'test')",
        book_id,
        to_vector_literal(_vector(first)),
        DIM,
    )


def test_취향_프로필이_있으면_유사도가_점수에_반영된다() -> None:
    """#180 — 후보가 이미 정해져 있어도(①의 키워드 검색 결과) 취향 centroid와의

    유사도를 계산해 반영해야 한다. 카테고리·인기 재료를 없앤 채(둘 다 0점) 유사도만
    다른 책 둘을 비교해, 유사도 항만으로 점수가 갈리는지 본다.
    """

    async def check(conn):
        await _insert_book(conn, 9100905, None)  # 카테고리 없음 → 카테고리 항 0
        await _insert_book(conn, 9100906, None)
        await _insert_embedding(conn, 9100905, 0.95)  # SIMILARITY_CEILING 이상 → 만점
        await _insert_embedding(conn, 9100906, 0.5)  # SIMILARITY_FLOOR 미만 → 0점
        await conn.execute(
            "INSERT INTO taste_profile (user_id, centroid, tag_weights, cold_start,"
            " profile_version) VALUES ($1, $2::vector, '{}'::jsonb, false, 1)",
            _USER,
            to_vector_literal(_vector(1.0)),
        )
        candidates = _candidates(9100905, 9100906)
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    candidates = _run(check)
    # SIMILARITY_WEIGHT(0.6) 만큼만 붙는다 — 카테고리·인기 항은 둘 다 0.
    assert candidates[0]["match_score"] == 60
    assert candidates[1]["match_score"] == 0


def test_cold_start면_centroid가_있어도_유사도를_안_쓴다() -> None:
    """cold_start 플래그가 참이면(④의 _profile()과 같은 기준) centroid가 실제로

    있어도 유사도를 계산하지 않는다 — 데이터가 남아있는 낡은 프로필 같은 경우를 방어한다.
    """

    async def check(conn):
        await _insert_book(conn, 9100907, None)
        await _insert_embedding(conn, 9100907, 1.0)
        await conn.execute(
            "INSERT INTO taste_profile (user_id, centroid, tag_weights, cold_start,"
            " profile_version) VALUES ($1, $2::vector, '{}'::jsonb, true, 1)",
            _USER,
            to_vector_literal(_vector(1.0)),
        )
        candidates = _candidates(9100907)
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    candidates = _run(check)
    assert candidates[0]["match_score"] == 0


def test_semantic이_없으면_임베딩_없이_옛_식으로_계산한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#222 — exact 의도라 semantic이 없으면 비교할 조건이 없다. 임베딩을 아예

    안 불러야 한다 — 실패 시 로그만 남기고 넘어가는 방어 코드가 있다고 필요도
    없는데 매번 부르면 안 된다.
    """

    def _boom(texts, purpose):
        raise AssertionError("semantic이 없으면 embedding.embed를 부르면 안 됨")

    async def check(conn):
        monkeypatch.setattr(chat.embedding, "embed", _boom)
        await _insert_book(conn, 9100908, _LIKED)
        await conn.execute(
            "INSERT INTO taste_profile (user_id, tag_weights, cold_start, profile_version)"
            " VALUES ($1, $2::jsonb, true, 1)",
            _USER,
            json.dumps({_LIKED: 4}, ensure_ascii=False),
        )
        candidates = _candidates(9100908)
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    candidates = _run(check)
    # semantic이 없으니 옛 식(취향+카테고리+인기) 그대로 — CATEGORY_WEIGHT(25)만 붙는다.
    assert candidates[0]["match_score"] == 25


def test_semantic이_있으면_질의_유사도가_점수에_반영된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#222 — 이번 요청 조건(spec.semantic)과 후보 책의 유사도를 메인으로 채점한다.

    취향 프로필이 없는(cold_start) 사용자라 취향 항은 0이고, 카테고리·인기
    재료도 없앤 채 질의 유사도만으로 점수가 갈리는지 본다.
    """

    async def _fake_embed(texts, purpose):
        assert texts == ["비 오는 날 읽을 잔잔한 책"]
        assert purpose == "query"
        return [_vector(1.0)], DIM, "test"

    async def check(conn):
        monkeypatch.setattr(chat.embedding, "embed", _fake_embed)
        await _insert_book(conn, 9100909, None)
        await _insert_book(conn, 9100910, None)
        await _insert_embedding(conn, 9100909, 0.95)  # 질의 벡터와 거의 같음 → 만점
        await _insert_embedding(conn, 9100910, 0.5)  # SIMILARITY_FLOOR 미만 → 0점
        candidates = _candidates(9100909, 9100910)
        await chat._attach_match_scores(
            conn, candidates, _USER, _spec(semantic="비 오는 날 읽을 잔잔한 책")
        )
        return candidates

    candidates = _run(check)
    # QUERY_SIMILARITY_WEIGHT(0.5)만큼만 붙는다 — 취향(프로필 없음)·카테고리·
    # 인기 항은 모두 0.
    assert candidates[0]["match_score"] == 50
    assert candidates[1]["match_score"] == 0


def test_질의_임베딩이_실패하면_옛_식으로_폴백한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#222 — 임베딩 장애로 ③ 전체가 500이 되면 안 된다. 취향 기반 옛 식으로 계속 응답한다."""

    async def _boom(texts, purpose):
        raise RuntimeError("모델 장애")

    async def check(conn):
        monkeypatch.setattr(chat.embedding, "embed", _boom)
        await _insert_book(conn, 9100911, _LIKED)
        await conn.execute(
            "INSERT INTO taste_profile (user_id, tag_weights, cold_start, profile_version)"
            " VALUES ($1, $2::jsonb, true, 1)",
            _USER,
            json.dumps({_LIKED: 4}, ensure_ascii=False),
        )
        candidates = _candidates(9100911)
        await chat._attach_match_scores(
            conn, candidates, _USER, _spec(semantic="아무 조건")
        )
        return candidates

    candidates = _run(check)
    assert candidates[0]["match_score"] == 25


def test_질의_유사도와_취향_유사도가_함께_있으면_둘_다_반영한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#222 — 취향 프로필이 있는 사용자면 질의 유사도(메인)에 취향 유사도(보정)를 더한다."""

    async def _fake_embed(texts, purpose):
        return [_vector(1.0)], DIM, "test"

    async def check(conn):
        monkeypatch.setattr(chat.embedding, "embed", _fake_embed)
        await _insert_book(conn, 9100912, None)  # 카테고리 없음 → 카테고리 항 0
        await _insert_embedding(conn, 9100912, 1.0)  # 질의·취향 둘 다와 완전히 같음
        await conn.execute(
            "INSERT INTO taste_profile (user_id, centroid, tag_weights, cold_start,"
            " profile_version) VALUES ($1, $2::vector, '{}'::jsonb, false, 1)",
            _USER,
            to_vector_literal(_vector(1.0)),
        )
        candidates = _candidates(9100912)
        await chat._attach_match_scores(
            conn, candidates, _USER, _spec(semantic="아무 조건")
        )
        return candidates

    candidates = _run(check)
    # QUERY_SIMILARITY_WEIGHT(0.5) + TASTE_SIMILARITY_WEIGHT(0.2) = 0.7. 카테고리·
    # 인기 재료는 없어 0.
    assert candidates[0]["match_score"] == 70


@pytest.mark.parametrize("cold_start_row", [False, True])
def test_저장된_취향_벡터가_없으면_구매_이력이_있어도_취향을_반영하지_않는다(
    cold_start_row: bool,
) -> None:
    """#266 — BE는 개인화 추천에 동의한 사용자만 ⑥을 부른다. 프로필이 없으면 동의하지 않은 사용자다.

    이력으로 임시 취향을 만들던 경로(#246)를 뺐다. 프로필이 없는 사용자와 재료 없이 cold_start로
    저장된 사용자 모두, _LIKED 분류의 책(9100920)을 샀어도 같은 분류·같은 벡터의 후보(9100921)에
    취향 유사도 항도 카테고리 항도 붙지 않는다.
    """

    async def check(conn):
        await _insert_book(conn, 9100920, _LIKED)
        await _insert_book(conn, 9100921, _LIKED)
        await _insert_embedding(conn, 9100920, 1.0)
        await _insert_embedding(conn, 9100921, 1.0)
        if cold_start_row:
            await conn.execute(
                "INSERT INTO taste_profile (user_id, tag_weights, cold_start, profile_version)"
                " VALUES ($1, '{}'::jsonb, true, 1)",
                _USER,
            )
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100920, now())", _USER
        )
        candidates = _candidates(9100921)
        await chat._attach_match_scores(conn, candidates, _USER, _spec())
        return candidates

    candidates = _run(check)

    # 취향도 카테고리 점수도 인기 행도 없다.
    assert candidates[0]["match_score"] == 0
