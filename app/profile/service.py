"""⑥ 취향 프로필을 다시 만들어 taste_profile 에 저장한다.

부를 때마다 전부 다시 계산한다(명세 ⑥). 같은 멱등 키로 다시 오면 계산하지 않고 저장한 응답을
돌려주고, 같은 사용자의 요청은 하나씩 차례로 처리한다(명세 ⑥, #93).
"""

import json
from datetime import datetime
from typing import Any

import asyncpg

from app.core import db, history, idempotency
from app.core.pgvector import to_vector_literal
from app.profile import compute
from app.profile.compute import Profile
from app.profile.schemas import ProfileRequest

# ⑦도 같은 멱등 테이블을 쓰므로 키 앞에 붙여 나눈다.
IDEMPOTENCY_SCOPE = "profile"

# 사용자별 잠금. pg_advisory_xact_lock(앞, 뒤) 두 숫자 형태의 앞 숫자로, 다른 곳의 잠금과
# 번호가 겹치지 않게 API 번호(⑥)를 쓴다. 행 잠금이 아니라 숫자에 거는 잠금이라 첫 호출(프로필 행이
# 아직 없음)에도 걸리고, 트랜잭션이 끝나면 저절로 풀린다.
_LOCK_SPACE = 6

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


async def _book_vectors(
    conn: asyncpg.Connection, book_ids: list[int]
) -> dict[int, list[float]]:
    """책 벡터. 소개글이 없어 벡터가 없는 책은 빠진다."""
    if not book_ids:
        return {}
    rows = await conn.fetch(_EMBEDDINGS_SQL, book_ids)
    return {r["book_id"]: json.loads(r["embedding"]) for r in rows}


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


async def rebuild_on(conn: asyncpg.Connection, req: ProfileRequest) -> tuple[bool, int]:
    """계산해서 저장하고 (cold_start, profile_version) 을 돌려준다. 부르는 쪽이 트랜잭션을 연다."""
    hist = await history.read(conn, req.user_id)
    # 같은 책을 두 번 적어 보내도 한 번만 친다.
    liked = list(dict.fromkeys(req.used_liked_book_ids()))
    weights = compute.book_weights(liked, hist.weights, hist.disliked_book_ids)
    vectors = await _book_vectors(conn, list(weights))

    parts = [(vectors[b], w) for b, w in weights.items() if b in vectors]
    parts += [(m.vector, compute.MEMORY_WEIGHT) for m in req.used_memories()]

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
    return profile.cold_start, version


async def rebuild_once_on(
    conn: asyncpg.Connection, req: ProfileRequest, body_hash: str
) -> dict[str, Any]:
    """멱등 키와 사용자별 잠금을 걸고 처리한 뒤 응답 본문을 돌려준다.

    같은 키·다른 본문이면 idempotency.IdempotencyConflict(409).
    잠금 없이 두 요청이 동시에 돌면 둘 다 같은 profile_version 을 읽고 +1 을 써서, 프로필이 두 번
    바뀌었는데 판 번호는 한 번만 오른다. 프로필 저장과 멱등 기록은 한 트랜잭션이라 함께 남거나 함께 빠진다.
    """
    key = idempotency.scoped_key(IDEMPOTENCY_SCOPE, req.idempotency_key)
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1, $2)", _LOCK_SPACE, req.user_id
        )
        stored = await idempotency.lookup(conn, key, body_hash)
        if stored is not None:
            return stored
        cold_start, version = await rebuild_on(conn, req)
        response = {
            "message": "profile_success",
            "data": {"cold_start": cold_start, "profile_version": version},
        }
        await idempotency.remember(conn, key, body_hash, response)
    return response


async def rebuild(req: ProfileRequest, body_hash: str) -> dict[str, Any]:
    async with db.get_pool().acquire() as conn:
        return await rebuild_once_on(conn, req, body_hash)
