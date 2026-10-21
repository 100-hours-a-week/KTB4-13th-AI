"""⑥ 취향 프로필 저장 테스트. 실제 PostgreSQL(pgvector 포함)이 있어야 돈다.

SEARCH_TEST_DATABASE_URL 에 DB 주소를 주면 돈다. 넣은 데이터는 트랜잭션을 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest

from app.core.config import get_settings
from app.core.pgvector import to_vector_literal
from app.feed.interest_feed import Feed
from app.feed.interest_list import Placed
from app.profile import labels, service
from app.profile.schemas import ProfileRequest

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


@pytest.fixture(autouse=True)
def _no_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """라벨 벡터는 테스트마다 직접 넣는다. 다른 테스트가 모델을 읽어 뒀어도 여기서 만들지 않는다."""
    monkeypatch.setattr(labels, "_vectors", {})
    monkeypatch.setattr(labels.embedding, "is_loaded", lambda: False)


_USER = 9_100_001
_T0 = datetime(2026, 9, 1, tzinfo=UTC)
DIM = 384


def _unit(i: int) -> list[float]:
    vector = [0.0] * DIM
    vector[i] = 1.0
    return vector


# 책마다 서로 다른 축을 가리키는 벡터를 준다. 취향 벡터가 어느 책 쪽으로 끌렸는지 바로 보인다.
_BOOKS = {9100201: ("에세이", _unit(0)), 9100202: ("한국소설", _unit(1))}


def _isbn(book_id: int) -> str:
    """테스트 책의 ISBN. book_id 마다 다른 13자리 숫자다."""
    return str(9780000000000 + book_id)


def _request(**overrides) -> ProfileRequest:
    body = {
        "user_id": _USER,
        "idempotency_key": "prof_test",
        "onboarding": {"tags": ["재테크"], "liked_book_ids": [9100201]},
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
                    " in_stock, cover_url, category, pub_year, description, isbn13)"
                    " VALUES ($1, '책', '저자', '출판사', 10000, true, NULL, $2, 2024, '소개', $3)",
                    book_id,
                    category,
                    _isbn(book_id),
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
            "INSERT INTO v_user_purchases VALUES ($1, 9100202, $2, 'PAID')", _USER, _T0
        )
        result = await service.rebuild_on(conn, _request())
        return result, await _row(conn)

    (cold_start, version), row = _run(check)

    assert (cold_start, version) == (False, 1)
    centroid = json.loads(row["centroid"])
    # 좋아한 책(2) : 구매한 책(3)
    assert centroid[1] / centroid[0] == pytest.approx(1.5, rel=1e-5)
    assert json.loads(row["tag_weights"]) == {"경제학": 1, "한국소설": 3}
    assert row["cold_start"] is False
    assert row["computed_at"] == _T0


def _centroid_of(onboarding: dict) -> list[float]:
    async def check(conn):
        await service.rebuild_on(conn, _request(onboarding=onboarding))
        return json.loads((await _row(conn))["centroid"])

    return _run(check)


def test_좋아한_책을_ISBN으로_보내도_book_id로_보낸_것과_같은_취향_벡터가_된다() -> (
    None
):
    by_id = _centroid_of({"liked_book_ids": [9100201, 9100202]})
    by_isbn = _centroid_of({"liked_isbns": [_isbn(9100201), _isbn(9100202)]})

    assert by_isbn == pytest.approx(by_id)


def test_같은_책을_ISBN과_book_id로_함께_보내도_한_번만_친다() -> None:
    # BE 가 ISBN 으로 옮기는 동안 둘 다 실어 보낼 수 있다(#315).
    once = _centroid_of({"liked_book_ids": [9100201, 9100202]})
    both = _centroid_of(
        {"liked_book_ids": [9100201, 9100202], "liked_isbns": [_isbn(9100201)]}
    )

    assert both == pytest.approx(once)


def test_두_칸을_합쳐_상한만큼만_쓰고_ISBN으로_온_책이_앞이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service, "USED_LIKED_BOOKS", 1)

    centroid = _centroid_of(
        {"liked_book_ids": [9100202], "liked_isbns": [_isbn(9100201)]}
    )

    # 한 권만 쓰면 ISBN 으로 온 9100201 이다. 그 책의 벡터는 0번 축이다.
    assert centroid == pytest.approx(_unit(0))


def test_책_표에_없는_ISBN은_건너뛰고_나머지로_만든다() -> None:
    known = _centroid_of({"liked_isbns": [_isbn(9100201)]})
    with_unknown = _centroid_of({"liked_isbns": ["9789999999999", _isbn(9100201)]})

    assert with_unknown == pytest.approx(known)


def test_온보딩_카테고리를_카탈로그_분류_점수로_풀어_이력과_합쳐_저장한다() -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100202, $2, 'PAID')", _USER, _T0
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


def test_카테고리만_골라도_라벨_벡터로_취향_벡터를_만든다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 명세 ⑥: 이력이 없고 온보딩도 건너뛴 사용자만 cold_start 다.
    monkeypatch.setattr(labels, "_vectors", {"소설": _unit(5)})

    async def check(conn):
        result = await service.rebuild_on(
            conn, _request(onboarding={"categories": ["소설"]})
        )
        return result, await _row(conn)

    (cold_start, _), row = _run(check)

    assert cold_start is False
    assert json.loads(row["centroid"]) == pytest.approx(_unit(5))


def test_라벨은_모두_합쳐_기억_한_문장만큼_끌어당긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(labels, "_vectors", {"소설": _unit(5), "에세이": _unit(6)})

    async def check(conn):
        # 재테크는 이 테스트에서 라벨 벡터를 만들지 않았다. 벡터 재료에서만 빠지고 태그 가중치에는 남는다.
        onboarding = {
            "categories": ["소설", "에세이"],
            "tags": ["재테크"],
            "liked_book_ids": [9100201],
        }
        await service.rebuild_on(conn, _request(onboarding=onboarding))
        return await _row(conn)

    row = _run(check)

    centroid = json.loads(row["centroid"])
    # 좋아한 책(2) : 소설(1/2) : 에세이(1/2)
    assert centroid[5] / centroid[0] == pytest.approx(0.25, rel=1e-5)
    assert centroid[6] / centroid[0] == pytest.approx(0.25, rel=1e-5)
    assert json.loads(row["tag_weights"])["경제학"] == 1


def _memory(keyword: str, strength: int, vector: list[float], day: int) -> dict:
    return {
        "type": "topic",
        "value": f"{keyword}을 다룬 책",
        "strength": strength,
        "applies_to": [],
        "keyword": keyword,
        "created_at": f"2026-09-{day:02d}T09:00:00",
        "vector": vector,
        "dim": DIM,
    }


def test_취향_기억은_살아_있는_좋아함_줄만_취향_벡터에_섞는다() -> None:
    async def check(conn):
        memories = [
            _memory("무협", 2, _unit(7), day=1),
            _memory("로맨스", 2, _unit(8), day=1),
            _memory("로맨스", -2, _unit(8), day=2),  # 뒤에 싫다고 했다
        ]
        await service.rebuild_on(conn, _request(memories=memories))
        return await _row(conn)

    centroid = json.loads(_run(check)["centroid"])

    # 핵심어 벡터에는 좋고 싫음이 없어, 싫다고 한 로맨스를 섞으면 그쪽으로 끌려간다(#436)
    assert centroid[7] > 0
    assert centroid[8] == 0


def test_같은_요청을_다시_보내면_판_번호가_그대로고_바뀌면_오른다() -> None:
    async def check(conn):
        first = await service.rebuild_on(conn, _request())
        again = await service.rebuild_on(conn, _request())
        changed = await service.rebuild_on(
            conn, _request(onboarding={"tags": ["습관"], "liked_book_ids": [9100201]})
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
# 요청의 history 로 계산한다(#327). 칸이 없으면 지금처럼 복제 표에서 읽는다.
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
def at_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """요청을 받은 시각을 정해 둔다."""
    monkeypatch.setattr(service, "_now", lambda: _NOW)
    return _NOW


async def _rebuilt(conn, **overrides) -> asyncpg.Record:
    await service.rebuild_on(conn, _request(**overrides))
    return await _row(conn)


def test_history가_오면_복제_표가_아니라_요청의_이력으로_계산한다(
    at_now: datetime,
) -> None:
    async def check(conn):
        # 복제 표에는 한국소설(9100202)을 산 것으로 남아 있지만, 요청은 에세이(9100201)를 샀다고 한다.
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100202, $2, 'PAID')", _USER, _T0
        )
        return await _rebuilt(
            conn,
            onboarding={},
            history={"purchased_isbns": [_isbn(9100201)]},
        )

    row = _run(check)

    assert json.loads(row["tag_weights"]) == {"에세이": 3}
    centroid = json.loads(row["centroid"])
    assert (centroid[0], centroid[1]) == (pytest.approx(1.0), pytest.approx(0.0))


def test_history가_없으면_지금처럼_복제_표의_이력으로_계산한다() -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100202, $2, 'PAID')", _USER, _T0
        )
        return await _rebuilt(conn, onboarding={})

    row = _run(check)

    assert json.loads(row["tag_weights"]) == {"한국소설": 3}
    assert row["computed_at"] == _T0


def test_빈_history는_이력이_없다는_뜻이라_복제_표를_읽지_않는다(
    at_now: datetime,
) -> None:
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100202, $2, 'PAID')", _USER, _T0
        )
        return await _rebuilt(conn, history={})

    row = _run(check)

    # 온보딩 답(좋아한 책 9100201, 태그 재테크)만 남는다.
    assert json.loads(row["tag_weights"]) == {"경제학": 1}
    centroid = json.loads(row["centroid"])
    assert (centroid[0], centroid[1]) == (pytest.approx(1.0), pytest.approx(0.0))


def test_요청으로_받은_이력은_받은_시각까지_반영한_것으로_저장한다(
    at_now: datetime,
) -> None:
    # 이력이 비어 있어도 받은 시각을 적는다. 비워 두면 ③④ 가 복제 표에 남은 이력을 전부
    # "프로필 이후 이력"으로 보고 한 번 더 더한다.
    async def check(conn):
        filled = await _rebuilt(conn, history={"library_isbns": [_isbn(9100202)]})
        empty = await _rebuilt(conn, history={})
        return filled["computed_at"], empty["computed_at"]

    assert _run(check) == (at_now, at_now)


def test_같은_이력이면_요청으로_받아도_복제_표에서_읽어도_같은_프로필이다(
    at_now: datetime,
) -> None:
    # 9100201 은 사고 1점을 줬고, 9100202 는 담고 4.5점을 줬다.
    async def check(conn):
        await conn.execute(
            "INSERT INTO v_user_purchases VALUES ($1, 9100201, $2, 'PAID')", _USER, _T0
        )
        await conn.execute(
            "INSERT INTO v_user_library VALUES ($1, 9100202, $2)", _USER, _T0
        )
        await conn.executemany(
            "INSERT INTO v_user_reviews VALUES ($1, $2, $3, $4)",
            [(_USER, 9100201, 1.0, _T0), (_USER, 9100202, 4.5, _T0)],
        )
        replicated = await _rebuilt(conn, onboarding={"tags": ["재테크"]})
        reported = await _rebuilt(
            conn,
            onboarding={"tags": ["재테크"]},
            history={
                "purchased_isbns": [_isbn(9100201)],
                "library_isbns": [_isbn(9100202)],
                "reviews": [
                    {"isbn": _isbn(9100201), "rating": 1.0},
                    {"isbn": _isbn(9100202), "rating": 4.5, "content": "좋았어요"},
                ],
            },
        )
        return replicated, reported

    replicated, reported = _run(check)

    assert json.loads(reported["centroid"]) == pytest.approx(
        json.loads(replicated["centroid"])
    )
    assert json.loads(reported["tag_weights"]) == json.loads(replicated["tag_weights"])
    # 결과가 같아서 판 번호도 오르지 않는다.
    assert reported["profile_version"] == replicated["profile_version"] == 1


def test_요청의_이력에_책_표에_없는_ISBN이_있어도_나머지로_계산한다(
    at_now: datetime,
) -> None:
    async def check(conn):
        return await _rebuilt(
            conn,
            onboarding={},
            history={"purchased_isbns": ["9780000000001", _isbn(9100202)]},
        )

    assert json.loads(_run(check)["tag_weights"]) == {"한국소설": 3}


# ---------------------------------------------------------------------------
# 멱등 키를 확인하지 않는다(#303). 사용자별 차례 처리는 그대로다(#93)
# ---------------------------------------------------------------------------


def test_같은_요청을_다시_보내면_다시_계산해도_같은_응답이다() -> None:
    # 키로 저장한 응답을 돌려주지 않아도 재시도는 같은 답을 받는다. 판 번호는 결과가 달라질 때만 오른다.
    async def check(conn):
        first = await service.rebuild_locked_on(conn, _request())
        again = await service.rebuild_locked_on(conn, _request())
        return first, again

    first, again = _run(check)

    assert again == first
    assert first["data"] == {"cold_start": False, "profile_version": 1}


def test_같은_키에_다른_본문이어도_계산해서_프로필을_바꾼다() -> None:
    async def check(conn):
        await service.rebuild_locked_on(conn, _request())
        second = await service.rebuild_locked_on(
            conn, _request(onboarding={"tags": ["습관"]})
        )
        return second, await _row(conn)

    second, row = _run(check)

    assert second["data"] == {"cold_start": True, "profile_version": 2}
    assert "경제학" not in json.loads(row["tag_weights"])


def test_멱등_기록을_남기지_않는다() -> None:
    async def check(conn):
        await service.rebuild_locked_on(conn, _request())
        return await conn.fetchval(
            "SELECT count(*) FROM idempotency_records"
            " WHERE idempotency_key = 'profile:prof_test'"
        )

    assert _run(check) == 0


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
                for key, tag in (("k1", "재테크"), ("k2", "습관"))
            ]
            results = await asyncio.gather(
                *(
                    service.rebuild_locked_on(conn, req)
                    for conn, req in zip(conns, requests, strict=True)
                )
            )
            return sorted(r["data"]["profile_version"] for r in results)
        finally:
            await conns[0].execute("DELETE FROM taste_profile WHERE user_id = $1", user)
            for conn in conns:
                await conn.close()

    assert asyncio.run(go()) == [1, 2]


# ---------------------------------------------------------------------------
# 관심사 점수 저장(#440). 기능 플래그가 켜져 있을 때만 새 표·칸을 읽고 쓴다.
# ---------------------------------------------------------------------------

_MIGRATION = (
    Path(__file__).parent.parent
    / "db"
    / "migrations"
    / "20261016_taste_profile_interests.sql"
)


def _run_with_interest_tables(check):
    """새 칸 마이그레이션(#439)과 AI 책 분류 표(#421)가 있는 DB 에서 돈다. 둘 다 되돌린다."""

    async def with_tables(conn):
        await conn.execute(_MIGRATION.read_text(encoding="utf-8"))
        await conn.execute(
            "CREATE TEMP TABLE book_classes (isbn13 text PRIMARY KEY, class_no text)"
        )
        await conn.execute(
            "INSERT INTO book_classes VALUES ($1, '594.5')", _isbn(9100201)
        )
        return await check(conn)

    return _run(with_tables)


async def _interest_row(conn: asyncpg.Connection) -> dict:
    row = await conn.fetchrow(
        "SELECT interest_scores::text, interest_vectors::text, conditions::text,"
        " book_first_seen::text FROM taste_profile WHERE user_id = $1",
        _USER,
    )
    return {key: json.loads(value) for key, value in dict(row).items()}


class _FakeBuild:
    """추천 목록 뽑기를 가짜로 바꾼다(뽑기는 tests/test_feed_interest_feed.py 에서 본다).

    받은 값을 남기고, feeds 에 넣어 둔 목록을 차례로 돌려준다. 없으면 빈 목록이다.
    """

    def __init__(self) -> None:
        self.feeds: list[Feed] = []
        self.calls: list[dict] = []

    async def __call__(
        self,
        conn,
        scores,
        book_vectors,
        keyword_vectors,
        conditions,
        owned,
        book_anchors=None,
    ):
        self.calls.append(
            {
                "scores": scores,
                "keyword_vectors": keyword_vectors,
                "conditions": conditions,
                "owned": owned,
                "book_anchors": book_anchors,
            }
        )
        return self.feeds.pop(0) if self.feeds else Feed([], ())


@pytest.fixture
def interest_feed(monkeypatch: pytest.MonkeyPatch) -> _FakeBuild:
    monkeypatch.setattr(get_settings(), "interest_feed", True)
    # 소설의 장르를 가를 이름 벡터는 없다고 친다. 다른 테스트가 만들어 둔 것을 쓰지 않는다
    monkeypatch.setattr(labels, "_name_vectors", {})
    build = _FakeBuild()
    monkeypatch.setattr(service.interest_feed, "build", build)
    return build


def _memories() -> list[dict]:
    return [
        _memory("무협", 2, _unit(7), day=1),
        {**_memory("300쪽 이하", 2, _unit(9), day=2), "type": "condition"},
    ]


def test_기능_플래그가_켜지면_관심사_점수와_조건과_처음_본_날을_저장한다(
    interest_feed, at_now
) -> None:
    async def check(conn):
        req = _request(
            onboarding={
                "categories": ["역사"],
                "tags": ["재테크"],
                "liked_book_ids": [9100201],
            },
            memories=_memories(),
            history={},
        )
        await service.rebuild_on(conn, req)
        return await _interest_row(conn)

    row = _run_with_interest_tables(check)

    assert row["interest_scores"] == {
        "역사": {"onboarding": 1.0},
        "재테크": {"onboarding": 1.0},
        "키워드:무협": {"memory": 2.0},
        # 좋아한 책(무게 2)은 세부 분류 번호 594 로 요리다
        "요리": {"books": 2.0},
    }
    # 책이 있는 관심사는 책 벡터 평균, 키워드 관심사는 핵심어 벡터(밤에 다시 뽑을 때 쓴다, #456)
    assert row["interest_vectors"] == {
        "요리": pytest.approx(_unit(0)),
        "키워드:무협": pytest.approx(_unit(7)),
    }
    assert row["conditions"] == ["300쪽 이하"]
    # 받은 날(한국 시간 10/12)의 전날
    assert row["book_first_seen"] == {_isbn(9100201): "2026-10-11"}


def test_처음_본_날은_다시_불려도_지키고_이력에서_빠진_책은_지운다(
    interest_feed, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check(conn):
        monkeypatch.setattr(service, "_now", lambda: _NOW)
        both = {"liked_book_ids": [9100201, 9100202]}
        await service.rebuild_on(conn, _request(onboarding=both, history={}))
        monkeypatch.setattr(service, "_now", lambda: _NOW + timedelta(days=8))
        one = {"liked_book_ids": [9100201]}
        await service.rebuild_on(conn, _request(onboarding=one, history={}))
        return await _interest_row(conn)

    row = _run_with_interest_tables(check)

    assert row["book_first_seen"] == {_isbn(9100201): "2026-10-11"}


async def _feed_row(conn: asyncpg.Connection) -> dict:
    row = await conn.fetchrow(
        "SELECT feed::text, feed_version, feed_prev::text, feed_partial"
        " FROM taste_profile WHERE user_id = $1",
        _USER,
    )
    return {
        "feed": row["feed"] and json.loads(row["feed"]),
        "feed_version": row["feed_version"],
        "feed_prev": row["feed_prev"] and json.loads(row["feed_prev"]),
        "feed_partial": row["feed_partial"],
    }


def test_기능_플래그가_켜지면_추천_목록을_뽑아_판_번호를_올리고_앞_판을_남긴다(
    interest_feed,
) -> None:
    first = Feed([Placed(9100201, "요리", 80)], ())
    second = Feed([Placed(9100202, "SF", 70)], ("작가:한강",))
    interest_feed.feeds = [first, second]

    async def check(conn):
        await service.rebuild_on(conn, _request(history={}))
        once = await _feed_row(conn)
        await service.rebuild_on(conn, _request(history={}))
        return once, await _feed_row(conn)

    once, twice = _run_with_interest_tables(check)

    assert once == {
        "feed": first.stored(),
        "feed_version": 1,
        "feed_prev": None,
        "feed_partial": False,
    }
    # 시간 예산을 넘겨 대신한 관심사가 있으면 표시해 밤에 다시 뽑는다
    assert twice == {
        "feed": second.stored(),
        "feed_version": 2,
        "feed_prev": first.stored(),
        "feed_partial": True,
    }


def test_목록_뽑기에_관심사_점수와_핵심어_벡터와_조건과_가진_책을_넘긴다(
    interest_feed,
) -> None:
    async def check(conn):
        req = _request(
            onboarding={"categories": ["역사"], "liked_book_ids": [9100201]},
            memories=_memories(),
            history={"purchased_isbns": [_isbn(9100202)]},
        )
        await service.rebuild_on(conn, req)

    _run_with_interest_tables(check)

    [call] = interest_feed.calls
    assert {"역사", "키워드:무협", "요리"} <= set(call["scores"])
    # 키워드 관심사의 검색어는 그 취향 기억 줄의 벡터다
    assert call["keyword_vectors"] == {"키워드:무협": _unit(7)}
    assert call["conditions"] == ["300쪽 이하"]
    # 산 책은 빼고, 좋아한다고만 한 책(9100201)은 빼지 않는다
    assert call["owned"] == {9100202}


def test_목록_뽑기에_관심사마다_매칭_점수에_쓸_내_책을_넘긴다(interest_feed) -> None:
    async def check(conn):
        req = _request(
            onboarding={"liked_book_ids": [9100201]},
            history={"purchased_isbns": [_isbn(9100202)]},
        )
        await service.rebuild_on(conn, req)

    _run_with_interest_tables(check)

    [call] = interest_feed.calls
    # 9100201 은 요리 번호(594.5)라 요리의 내 책이다. 9100202 는 한국소설인데 장르를 가를 이름 벡터가 없어
    # 관심사를 정하지 못한다(#483)
    assert call["book_anchors"] == {"요리": (9100201,)}


def test_기능_플래그가_꺼져_있으면_새_칸을_쓰지_않는다() -> None:
    async def check(conn):
        await service.rebuild_on(conn, _request(memories=_memories(), history={}))
        return await _interest_row(conn), await _feed_row(conn)

    row, feed = _run_with_interest_tables(check)

    assert row == {
        "interest_scores": {},
        "interest_vectors": {},
        "conditions": [],
        "book_first_seen": {},
    }
    assert feed == {
        "feed": None,
        "feed_version": 0,
        "feed_prev": None,
        "feed_partial": False,
    }
