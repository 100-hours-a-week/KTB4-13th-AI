"""⑥ 취향 프로필을 다시 만들어 taste_profile 에 저장한다.

부를 때마다 전부 다시 계산한다(명세 ⑥). 멱등 키는 확인하지 않는다(#303). 같은 사용자의 요청은
하나씩 차례로 처리한다(명세 ⑥, #93).
"""

import json
from datetime import datetime
from typing import Any

import asyncpg

from app.core import db, history, isbn
from app.core.pgvector import to_vector_literal
from app.profile import compute, labels
from app.profile.compute import Profile
from app.profile.schemas import USED_LIKED_BOOKS, Onboarding, ProfileRequest

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


async def rebuild_on(conn: asyncpg.Connection, req: ProfileRequest) -> tuple[bool, int]:
    """계산해서 저장하고 (cold_start, profile_version) 을 돌려준다. 부르는 쪽이 트랜잭션을 연다."""
    hist = await history.read(conn, req.user_id)
    # 좋아한 책은 ISBN 으로도 온다(#308). book_id 로 바꿔 앞에 두고, book_id 로 온 것을 뒤에 붙인다.
    # 같은 책을 두 번 적어 보내도(두 칸에 나눠 보내도) 한 번만 치고, 합쳐서 앞에서부터 상한만큼 쓴다.
    liked_by_isbn = await isbn.to_book_ids(conn, req.used_liked_isbns())
    liked = list(dict.fromkeys([*liked_by_isbn, *req.used_liked_book_ids()]))
    liked = liked[:USED_LIKED_BOOKS]
    weights = compute.book_weights(liked, hist.weights, hist.disliked_book_ids)
    vectors = await book_vectors(conn, list(weights))

    parts = [(vectors[b], w) for b, w in weights.items() if b in vectors]
    parts += [(m.vector, compute.MEMORY_WEIGHT) for m in req.used_memories()]
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
    return profile.cold_start, version


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
            "SELECT pg_advisory_xact_lock($1, $2)", _LOCK_SPACE, req.user_id
        )
        cold_start, version = await rebuild_on(conn, req)
    return {
        "message": "profile_success",
        "data": {"cold_start": cold_start, "profile_version": version},
    }


async def rebuild(req: ProfileRequest) -> dict[str, Any]:
    async with db.get_pool().acquire() as conn:
        return await rebuild_locked_on(conn, req)
