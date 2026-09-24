"""⑥ 취향 프로필 저장 테스트. 실제 PostgreSQL(pgvector 포함)이 있어야 돈다.

SEARCH_TEST_DATABASE_URL 에 DB 주소를 주면 돈다. 넣은 데이터는 트랜잭션을 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import json
import os
from datetime import UTC, datetime

import asyncpg
import pytest

from app.core import idempotency
from app.core.pgvector import to_vector_literal
from app.profile import service
from app.profile.schemas import ProfileRequest

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)

_USER = 9_100_001
_T0 = datetime(2026, 9, 1, tzinfo=UTC)
DIM = 384


def _unit(i: int) -> list[float]:
    vector = [0.0] * DIM
    vector[i] = 1.0
    return vector


# 책마다 서로 다른 축을 가리키는 벡터를 준다. 취향 벡터가 어느 책 쪽으로 끌렸는지 바로 보인다.
_BOOKS = {9100201: ("에세이", _unit(0)), 9100202: ("한국소설", _unit(1))}


def _request(**overrides) -> ProfileRequest:
    body = {
        "user_id": _USER,
        "idempotency_key": "prof_test",
        "onboarding": {"tags": ["힐링"], "liked_book_ids": [9100201]},
    }
    body.update(overrides)
    return ProfileRequest.model_validate(body)


def _run(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            for book_id, (category, vector) in _BOOKS.items():
                await conn.execute(
                    "INSERT INTO v_books (book_id, title, author, publisher, price,"
                    " in_stock, cover_url, category, pub_year, description)"
                    " VALUES ($1, '책', '저자', '출판사', 10000, true, NULL, $2, 2024, '소개')",
                    book_id,
                    category,
                )
                await conn.execute(
                    "INSERT INTO book_embeddings VALUES ($1, $2::vector, $3, 'test')",
                    book_id,
                    to_vector_literal(vector),
                    DIM,
                )
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


async def _row(conn: asyncpg.Connection) -> asyncpg.Record:
    return await conn.fetchrow(
        "SELECT centroid::text AS centroid, tag_weights::text AS tag_weights,"
        " cold_start, profile_version, computed_at FROM taste_profile WHERE user_id = $1",
        _USER,
    )


def test_좋아한_책과_이력으로_취향_벡터를_만들어_저장한다() -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100202, $2)", _USER, _T0
        )
        result = await service.rebuild_on(conn, _request())
        return result, await _row(conn)

    (cold_start, version), row = _run(check)

    assert (cold_start, version) == (False, 1)
    centroid = json.loads(row["centroid"])
    # 좋아한 책(2) : 구매한 책(3)
    assert centroid[1] / centroid[0] == pytest.approx(1.5, rel=1e-5)
    assert json.loads(row["tag_weights"]) == {"힐링": 1, "한국소설": 3}
    assert row["cold_start"] is False
    assert row["computed_at"] == _T0


def test_온보딩_카테고리를_카탈로그_분류_점수로_풀어_이력과_합쳐_저장한다() -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100202, $2)", _USER, _T0
        )
        onboarding = {"categories": ["소설"], "liked_book_ids": [9100201]}
        await service.rebuild_on(conn, _request(onboarding=onboarding))
        return await _row(conn)

    row = _run(check)

    weights = json.loads(row["tag_weights"])
    # 한국소설: 구매(3) + 소설의 핵심 분류(2). 문학은 소설의 일부 분류(1).
    assert weights["한국소설"] == 5
    assert weights["문학"] == 1
    assert "소설" not in weights


def test_같은_요청을_다시_보내면_판_번호가_그대로고_바뀌면_오른다() -> None:
    async def check(conn):
        first = await service.rebuild_on(conn, _request())
        again = await service.rebuild_on(conn, _request())
        changed = await service.rebuild_on(
            conn, _request(onboarding={"tags": ["성장"], "liked_book_ids": [9100201]})
        )
        return first, again, changed

    first, again, changed = _run(check)

    assert first == (False, 1)
    assert again == (False, 1)
    assert changed == (False, 2)


def test_재료가_없으면_cold_start로_저장한다() -> None:
    async def check(conn):
        # 벡터가 없는 책만 좋아한다고 보냈다
        result = await service.rebuild_on(
            conn, _request(onboarding={"liked_book_ids": [123456789]})
        )
        return result, await _row(conn)

    (cold_start, version), row = _run(check)

    assert (cold_start, version) == (True, 1)
    assert row["centroid"] is None
    assert row["computed_at"] is None


# ---------------------------------------------------------------------------
# 멱등 처리와 사용자별 차례 처리 (#93)
# ---------------------------------------------------------------------------


def test_같은_키와_본문이면_다시_계산하지_않고_저장한_응답을_준다() -> None:
    async def check(conn):
        first = await service.rebuild_once_on(conn, _request(), "hash-a")
        # 그사이 구매가 생겨도, 재시도는 처음 응답을 그대로 받고 프로필도 다시 쓰지 않는다.
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100202, $2)", _USER, _T0
        )
        again = await service.rebuild_once_on(conn, _request(), "hash-a")
        return first, again, await _row(conn)

    first, again, row = _run(check)

    assert again == first
    assert first["data"] == {"cold_start": False, "profile_version": 1}
    assert json.loads(row["tag_weights"]) == {"힐링": 1}


def test_같은_키에_다른_본문이면_409이고_프로필을_바꾸지_않는다() -> None:
    async def check(conn):
        await service.rebuild_once_on(conn, _request(), "hash-a")
        with pytest.raises(idempotency.IdempotencyConflict):
            await service.rebuild_once_on(
                conn, _request(onboarding={"tags": ["성장"]}), "hash-b"
            )
        return await _row(conn)

    row = _run(check)

    assert json.loads(row["tag_weights"]) == {"힐링": 1}
    assert row["profile_version"] == 1


def test_같은_사용자의_요청이_동시에_오면_하나씩_처리해_판_번호가_두_번_오른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 두 연결이 서로의 기록을 봐야 해서 트랜잭션을 되돌리는 방식을 못 쓴다. 넣고 끝나면 지운다.
    user = 9_100_301
    real_read = service.history.read

    async def slow_read(conn, user_id):
        # 둘이 확실히 겹치게, 이력을 읽은 뒤 잠깐 멈춘다. 잠금이 없으면 둘 다 판 번호 0을 보고 1을 쓴다.
        result = await real_read(conn, user_id)
        await asyncio.sleep(0.3)
        return result

    monkeypatch.setattr(service.history, "read", slow_read)

    async def go():
        conns = [await asyncpg.connect(_DB_URL) for _ in range(2)]
        try:
            requests = [
                ProfileRequest.model_validate(
                    {
                        "user_id": user,
                        "idempotency_key": key,
                        "onboarding": {"tags": [tag]},
                    }
                )
                for key, tag in (("k1", "힐링"), ("k2", "성장"))
            ]
            results = await asyncio.gather(
                *(
                    service.rebuild_once_on(conn, req, req.idempotency_key)
                    for conn, req in zip(conns, requests, strict=True)
                )
            )
            return sorted(r["data"]["profile_version"] for r in results)
        finally:
            await conns[0].execute("DELETE FROM taste_profile WHERE user_id = $1", user)
            await conns[0].execute(
                "DELETE FROM idempotency_records WHERE idempotency_key LIKE 'profile:k_'"
            )
            for conn in conns:
                await conn.close()

    assert asyncio.run(go()) == [1, 2]
