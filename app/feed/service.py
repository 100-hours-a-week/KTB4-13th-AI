"""④ 목록을 고르는 흐름 — 프로필이 있으면 채점한 목록, 없으면 개인화를 끈 목록.

취향 벡터를 만들지 못한 사용자(cold_start)와 프로필이 아직 없는 사용자는 같은 길로 간다.
벡터 조회가 안 될 때는 취향 유사도를 빼고 규칙 점수만으로 답한다(명세 ④: 축소 응답).
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.core import db
from app.feed import cold_start, personalized
from app.feed.schemas import FeedRequest

logger = logging.getLogger(__name__)

RULE_ONLY = "rule-only"

_PROFILE_SQL = """
SELECT centroid::text AS centroid, tag_weights::text AS tag_weights, cold_start
FROM taste_profile
WHERE user_id = $1
"""


@dataclass
class FeedOutcome:
    items: list[dict[str, Any]]
    # 기능을 줄여 응답했으면 그 이름, 아니면 None. 라우터가 헤더로 알린다.
    degraded: str | None = None
    # 개인화를 끈 목록이면 True. 응답 본문에 그대로 나간다(명세 ④).
    cold_start: bool = True


async def _profile(
    conn: asyncpg.Connection, user_id: int
) -> tuple[list[float], dict[str, float]] | None:
    """취향 벡터와 태그 가중치. 프로필이 없거나 벡터를 못 만든 사용자면 None."""
    row = await conn.fetchrow(_PROFILE_SQL, user_id)
    if row is None or row["cold_start"] or not row["centroid"]:
        return None
    return json.loads(row["centroid"]), json.loads(row["tag_weights"])


async def feed(req: FeedRequest) -> FeedOutcome:
    async with db.get_pool().acquire() as conn:
        profile = await _profile(conn, req.user_id)
        if profile is None:
            # 개인화를 끈 목록은 모두 0점이다. 하한이 1 이상이면 DB 를 더 볼 것 없이 0건이다
            # (명세: 필터로 0건이면 빈 목록으로 200, 필터를 임의로 풀지 않는다).
            if req.match_score_min:
                return FeedOutcome(items=[])
            return FeedOutcome(items=await cold_start.fetch(conn, req))

        centroid, tag_weights = profile
        try:
            items = await personalized.fetch(conn, req, centroid, tag_weights)
        except Exception:
            # 벡터 조회가 어떤 이유로 안 되든 할 일은 같다 — 유사도를 빼고 규칙 점수만으로
            # 답하고 헤더로 알린다(명세 ④). 좁게 잡으면 빠지는 게 생긴다(① 과 같은 판단).
            logger.exception("취향 벡터 조회 실패. 규칙 점수만으로 응답한다")
            items = await cold_start.fetch(conn, req)
            return FeedOutcome(items=items, degraded=RULE_ONLY, cold_start=False)

    return FeedOutcome(items=items, cold_start=False)
