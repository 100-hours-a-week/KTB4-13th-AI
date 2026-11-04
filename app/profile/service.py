"""⑥ 취향 프로필을 다시 만들어 taste_profile 에 저장한다.

부를 때마다 전부 다시 계산한다(명세 ⑥). 멱등 키는 확인하지 않는다(#303). 같은 사용자의 요청은
하나씩 차례로 처리한다(명세 ⑥, #93).
"""

# 홈 목록 장치(app/feed/build_queue.py)와 서로 부른다. 타입 표기를 함수를 만들 때 읽지 않게 한다
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg

from app.core import db, history, isbn
from app.core.config import get_settings
from app.core.pgvector import to_vector_literal
from app.feed import build_queue, interest_fetch
from app.profile import compute, interests, labels
from app.profile.compute import Profile
from app.profile.schemas import USED_LIKED_BOOKS, Memory, Onboarding, ProfileRequest

# 사용자별 잠금. pg_advisory_xact_lock(앞, 뒤) 두 숫자 형태의 앞 숫자로, 다른 곳의 잠금과
# 번호가 겹치지 않게 API 번호(⑥)를 쓴다. 행 잠금이 아니라 숫자에 거는 잠금이라 첫 호출(프로필 행이
# 아직 없음)에도 걸리고, 트랜잭션이 끝나면 저절로 풀린다.
LOCK_SPACE = 6

_EMBEDDINGS_SQL = """
SELECT book_id, embedding::text AS embedding
FROM book_embeddings
WHERE book_id = ANY($1::int[])
"""

_READ_SQL = """
SELECT centroid::text AS centroid, tag_weights::text AS tag_weights,
       cold_start, profile_version
FROM taste_profile
WHERE user_id = $1
"""

_SAVE_SQL = """
INSERT INTO taste_profile (user_id, centroid, tag_weights, cold_start, profile_version, computed_at)
VALUES ($1, $2::vector, $3::jsonb, $4, $5, $6)
ON CONFLICT (user_id) DO UPDATE SET
    centroid = EXCLUDED.centroid,
    tag_weights = EXCLUDED.tag_weights,
    cold_start = EXCLUDED.cold_start,
    profile_version = EXCLUDED.profile_version,
    computed_at = EXCLUDED.computed_at
"""


# 관심사 점수(#440). 기능 플래그(interest_feed)가 켜져 있을 때만 돈다. 꺼져 있으면 새 표·칸을 읽지도
# 쓰지도 않는다. AI 책 분류 표(book_classes)에 없는 책은 세부 분류 번호 없이 가른다
_BOOK_INFO_SQL = """
SELECT b.book_id, b.isbn13, b.category, c.class_no
FROM v_books b LEFT JOIN book_classes c ON c.isbn13 = b.isbn13
WHERE b.book_id = ANY($1::int[])
"""

# ⑨ 가 저장된 "책을 처음 본 날"의 책(ISBN)으로 관심사마다 내 책을 다시 가를 때 쓴다(#483)
_BOOK_INFO_BY_ISBN_SQL = """
SELECT b.book_id, b.isbn13, b.category, c.class_no
FROM v_books b LEFT JOIN book_classes c ON c.isbn13 = b.isbn13
WHERE b.isbn13 = ANY($1::text[])
"""

_READ_FIRST_SEEN_SQL = """
SELECT book_first_seen::text FROM taste_profile WHERE user_id = $1
"""


async def book_vectors(
    conn: asyncpg.Connection, book_ids: list[int]
) -> dict[int, list[float]]:
    """책 벡터. 소개글이 없어 벡터가 없는 책은 빠진다. 이력으로 만드는 임시 취향(#246)도 쓴다."""
    if not book_ids:
        return {}
    rows = await conn.fetch(_EMBEDDINGS_SQL, book_ids)
    return {r["book_id"]: json.loads(r["embedding"]) for r in rows}


async def _label_vectors(onboarding: Onboarding) -> list[list[float]]:
    """고른 카테고리·태그 중 라벨 벡터가 있는 것. 목록에 없는 라벨은 태그 가중치에만 쓰인다."""
    await labels.ensure_loaded()
    picked = dict.fromkeys([*onboarding.categories, *onboarding.tags])
    return [v for v in map(labels.vector, picked) if v is not None]


async def _stored(conn: asyncpg.Connection, user_id: int) -> tuple[Profile, int] | None:
    row = await conn.fetchrow(_READ_SQL, user_id)
    if row is None:
        return None
    centroid = json.loads(row["centroid"]) if row["centroid"] else None
    profile = Profile(centroid, json.loads(row["tag_weights"]), row["cold_start"])
    return profile, row["profile_version"]


async def _save(
    conn: asyncpg.Connection,
    user_id: int,
    profile: Profile,
    version: int,
    computed_at: datetime | None,
) -> None:
    centroid = to_vector_literal(profile.centroid) if profile.centroid else None
    await conn.execute(
        _SAVE_SQL,
        user_id,
        centroid,
        json.dumps(profile.tag_weights, ensure_ascii=False),
        profile.cold_start,
        version,
        computed_at,
    )


def _now() -> datetime:
    return datetime.now(UTC)


async def _history(conn: asyncpg.Connection, req: ProfileRequest) -> history.History:
    """산 책·나의 도서관 책·리뷰. 요청에 실려 왔으면 그것을, 아니면 복제 표를 읽는다(#327).

    칸이 없으면 BE 가 아직 보내지 않는 것이라 지금처럼 복제 표에서 읽는다. 복제를 멈출 때 이 길을
    빼고, 그때부터 칸이 없으면 이력이 없는 것이 된다(#307). 빈 이력이 오면 복제 표를 읽지 않는다.
    """
    if req.history is None:
        return await history.read(conn, req.user_id)
    return await history.from_request(
        conn,
        purchased_isbns=req.history.purchased_isbns,
        library_isbns=req.history.library_isbns,
        reviews=[(r.isbn, r.rating) for r in req.history.reviews],
        at=_now(),
    )


async def rebuild_on(conn: asyncpg.Connection, req: ProfileRequest) -> tuple[bool, int]:
    """계산해서 저장하고 (cold_start, profile_version) 을 돌려준다. 부르는 쪽이 트랜잭션을 연다.

    홈 목록을 응답 뒤에 만들도록 켜져 있어도 줄에 넣지 않고 그 자리에서 설정한 예산으로 만든다. 평가 도구가
    이 길로 프로필 계산 시간을 잰다. 응답 뒤에 만드는 것은 rebuild_locked_on 이 한다.
    """
    cold_start, version, job = await _rebuild_on(conn, req)
    if job is not None:
        await build_queue.build_and_save(conn, job)
    return cold_start, version


async def _rebuild_on(
    conn: asyncpg.Connection, req: ProfileRequest
) -> tuple[bool, int, build_queue.Job | None]:
    """계산해서 저장하고 (cold_start, profile_version, 줄에 넣을 작업) 을 돌려준다.

    관심사별 홈 추천이 켜져 있으면 목록 칸에 요청 표를 적고 목록 재료를 계산한다. 목록을 응답 뒤에 만들도록
    켜져 있으면(#581) 목록은 만들지 않고 재료를 작업으로 돌려준다. 꺼져 있으면 지금처럼 응답 안에서 3초
    예산으로 만들어 저장하고 작업은 None 이다. 두 경우 모두 같은 재료와 같은 저장(build_queue)을 쓴다.
    """
    hist = await _history(conn, req)
    # 좋아한 책은 ISBN 으로도 온다(#308). book_id 로 바꿔 앞에 두고, book_id 로 온 것을 뒤에 붙인다.
    # 같은 책을 두 번 적어 보내도(두 칸에 나눠 보내도) 한 번만 치고, 합쳐서 앞에서부터 상한만큼 쓴다.
    liked_by_isbn = await isbn.to_book_ids(conn, req.used_liked_isbns())
    liked = list(dict.fromkeys([*liked_by_isbn, *req.used_liked_book_ids()]))
    liked = liked[:USED_LIKED_BOOKS]
    weights = compute.book_weights(liked, hist.weights, hist.disliked_book_ids)
    vectors = await book_vectors(conn, list(weights))

    parts = [(vectors[b], w) for b, w in weights.items() if b in vectors]
    # 취향 기억은 살아 있는 좋아함 줄만 섞는다. 핵심어 벡터에는 좋고 싫음이 담기지 않아, 싫다고 한 것을
    # 섞으면 그쪽으로 끌려간다(명세 ⑥, #436)
    likes = interests.living_likes(req.used_memories())
    parts += [(m.vector, compute.MEMORY_WEIGHT) for m in likes]
    parts += compute.label_parts(await _label_vectors(req.onboarding))

    centroid = compute.centroid(parts)
    profile = Profile(
        centroid=centroid,
        tag_weights=compute.tag_weights(
            req.onboarding.tags, req.onboarding.categories, hist.category_scores
        ),
        # 취향 벡터를 만들지 못했으면 개인화를 끈다. ③④도 벡터가 없으면 채점할 수 없다(#92).
        cold_start=centroid is None,
    )

    version = compute.next_version(await _stored(conn, req.user_id), profile)
    await _save(conn, req.user_id, profile, version, hist.computed_at)
    settings = get_settings()
    if not settings.interest_feed:
        return profile.cold_start, version, None
    owned = set(hist.weights) | hist.disliked_book_ids
    token = await build_queue.mark(conn, req.user_id)
    job = await _feed_job(
        conn, req, weights, vectors, owned, hist.disliked_book_ids, token
    )
    if settings.profile_feed_async:
        return profile.cold_start, version, job
    await build_queue.build_and_save(
        conn, replace(job, seconds=interest_fetch.BUDGET_SECONDS)
    )
    return profile.cold_start, version, None


async def _feed_job(
    conn: asyncpg.Connection,
    req: ProfileRequest,
    weights: dict[int, float],
    vectors: dict[int, list[float]],
    owned: set[int],
    disliked: set[int],
    token: str,
) -> build_queue.Job:
    """홈 목록 재료: 관심사 점수·관심사마다 책 벡터 평균·조건 목록·책을 처음 본 날(#440, #453).

    책은 취향 벡터에 쓴 것과 같다(좋아한 책과 이력, 2점 이하 리뷰를 단 책은 뺌). 벡터가 없는 책은 지금처럼
    쓰지 않는다. 챗봇이 쓰는 지금 칸(centroid, tag_weights)은 위에서 따로 저장했다. 추천 목록에서는 가진
    책(owned: 이력의 책)을 빼고, 밤에 다시 뽑는 ⑨ 가 쓰도록 목록과 함께 남긴다(#537). 목록과 이 값들은
    build_queue.save 가 함께 저장한다.
    """
    rows = await conn.fetch(_BOOK_INFO_SQL, list(weights)) if weights else []
    isbns = [row["isbn13"] for row in rows if row["isbn13"]]
    first_seen = await _first_seen(conn, req.user_id, isbns)
    books = []
    mine = []  # (관심사, 책 번호, 처음 본 날). 매칭 점수에 쓰는 내 책(#483)
    for row in rows:
        found = _book_interest(row, vectors)
        if found:
            interest, vector = found
            books.append((interest, float(weights[row["book_id"]]), vector))
            mine.append((interest, row["book_id"], first_seen.get(row["isbn13"], "")))
    book_scores, interest_vectors = interests.book_scores(books)

    memories = req.used_memories()
    table = interests.per_interest(
        interests.onboarding_scores(req.onboarding.categories, req.onboarding.tags),
        interests.memory_scores(memories),
        book_scores,
    )
    return build_queue.Job(
        user_id=req.user_id,
        token=token,
        table=table,
        book_vectors=interest_vectors,
        # 키워드 관심사의 핵심어 벡터도 남긴다. 밤에 목록을 다시 뽑는 ⑨ 는 요청 없이 저장된 것만 쓴다(#456)
        keyword_vectors=_keyword_vectors(memories),
        conditions=interests.conditions(memories),
        owned=frozenset(owned),
        disliked=frozenset(disliked),
        first_seen=first_seen,
        book_anchors=interests.anchors(mine),
        seconds=get_settings().profile_feed_seconds,
    )


async def book_anchors(
    conn: asyncpg.Connection, first_seen: dict[str, str], excluded: set[int]
) -> dict[str, tuple[int, ...]]:
    """저장된 "책을 처음 본 날"의 책을 관심사로 갈라, 관심사마다 매칭 점수에 쓰는 내 책 번호(⑨ 가 쓴다, #483).

    ⑥ 이 이 책들로 관심사마다 책 벡터 평균을 만들었으므로 ⑥ 과 같은 책이다. 그 뒤 2점 이하 리뷰를 단 책
    (excluded)은 뺀다.
    """
    if not first_seen:
        return {}
    rows = await conn.fetch(_BOOK_INFO_BY_ISBN_SQL, list(first_seen))
    rows = [row for row in rows if row["book_id"] not in excluded]
    vectors = await book_vectors(conn, [row["book_id"] for row in rows])
    mine = []
    for row in rows:
        found = _book_interest(row, vectors)
        if found:
            mine.append((found[0], row["book_id"], first_seen[row["isbn13"]]))
    return interests.anchors(mine)


def _book_interest(
    row: asyncpg.Record, vectors: dict[int, list[float]]
) -> tuple[str, list[float]] | None:
    """책 한 권이 점수를 주는 관심사와 그 책 벡터. 벡터가 없거나 관심사를 정할 수 없으면 None 이다."""
    vector = vectors.get(row["book_id"])
    if vector is None:
        return None
    interest = interests.book_interest(
        row["class_no"], row["category"], vector, labels.name_vector
    )
    return (interest, vector) if interest else None


def _keyword_vectors(memories: list[Memory]) -> dict[str, list[float]]:
    """키워드 관심사마다 핵심어의 이름 벡터. 그 키워드를 말한 줄 가운데 가장 나중 줄의 벡터다.

    취향 기억의 벡터는 ⑤ 가 핵심어로 만든 것이라 핵심어의 이름 벡터와 같다(용도도 같은 query).
    """
    vectors: dict[str, list[float]] = {}
    for line in memories:
        for name in interests.interests_of(line):
            if name.startswith(interests.KEYWORD):
                vectors[name] = line.vector
    return vectors


async def _first_seen(
    conn: asyncpg.Connection, user_id: int, isbns: list[str]
) -> dict[str, str]:
    """이력의 책마다 AI 서버가 처음 본 날. 이미 본 책은 그날을 지키고, 처음 보는 책은 받은 날의 전날이다.

    BE 는 하루치 활동을 밤에 모아 부르므로 받은 날의 전날이 그 책이 생긴 날에 가깝다(#395). 이력에서 빠진
    책은 지운다. 오래된 취향을 약하게 할 때 쓴다(규칙은 나중에 정함).
    """
    stored = await conn.fetchval(_READ_FIRST_SEEN_SQL, user_id)
    seen = json.loads(stored) if stored else {}
    today = _now().astimezone(ZoneInfo(get_settings().tz)).date()
    yesterday = (today - timedelta(days=1)).isoformat()
    return {isbn13: seen.get(isbn13, yesterday) for isbn13 in isbns}


async def rebuild_locked_on(
    conn: asyncpg.Connection, req: ProfileRequest
) -> dict[str, Any]:
    """사용자별 잠금을 걸고 처리한 뒤 응답 본문을 돌려준다.

    잠금 없이 두 요청이 동시에 돌면 둘 다 같은 profile_version 을 읽고 +1 을 써서, 프로필이 두 번
    바뀌었는데 판 번호는 한 번만 오른다.

    멱등 키는 확인하지 않고 올 때마다 계산한다(#303). BE 는 키를 온보딩 답으로 만들어서, 기억이나
    이력만 바뀐 요청이 "같은 키·다른 본문"이 되어 409 로 막혔다. 재시도는 다시 계산해도 같은 답을
    받는다. 입력이 같으면 결과가 같고, 판 번호는 결과가 달라질 때만 오른다(compute.next_version).
    """
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1, $2)", LOCK_SPACE, req.user_id
        )
        cold_start, version, job = await _rebuild_on(conn, req)
    # 프로필과 요청 표가 확정된 뒤에 줄에 넣는다. 계산이 실패해 되돌려지면 넣지 않는다
    if job is not None:
        build_queue.submit(job)
    return {
        "message": "profile_success",
        "data": {"cold_start": cold_start, "profile_version": version},
    }


async def rebuild(req: ProfileRequest) -> dict[str, Any]:
    async with db.get_pool().acquire() as conn:
        return await rebuild_locked_on(conn, req)
