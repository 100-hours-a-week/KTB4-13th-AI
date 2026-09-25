"""④ 목록을 고르는 흐름 — 프로필이 있으면 채점한 목록, 없으면 개인화를 끈 목록.

취향 벡터를 만들지 못한 사용자(cold_start)와 프로필이 아직 없는 사용자는 같은 길로 간다.
벡터 조회가 안 될 때는 취향 유사도를 빼고 규칙 점수만으로 답한다(명세 ④: 축소 응답).
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from app.core import db, history
from app.feed import cold_start, cursor, personalized, rule_only
from app.feed.schemas import FeedRequest

logger = logging.getLogger(__name__)

# 응답 모드. 페이지 사이에 모드가 바뀌면 순서가 아예 달라 커서를 이어 쓸 수 없다.
PERSONALIZED = "personalized"
COLD_START = "cold-start"
RULE_ONLY = "rule-only"

_PROFILE_SQL = """
SELECT centroid::text AS centroid, tag_weights::text AS tag_weights,
       cold_start, profile_version, computed_at
FROM taste_profile
WHERE user_id = $1
"""


@dataclass
class FeedOutcome:
    items: list[dict[str, Any]]
    # 다음 페이지 커서. 마지막 페이지면 None(명세: 끝은 이 값으로만 판정한다).
    next_cursor: str | None = None
    # 기능을 줄여 응답했으면 그 이름, 아니면 None. 라우터가 헤더로 알린다.
    degraded: str | None = None
    # 개인화를 끈 목록이면 True. 응답 본문에 그대로 나간다(명세 ④).
    cold_start: bool = True


@dataclass
class _Profile:
    centroid: list[float]
    tag_weights: dict[str, float]
    version: int
    # 프로필이 반영한 이력의 가장 늦은 시각. 반영한 이력이 없으면 None(명세 ⑥).
    computed_at: datetime | None


async def _profile(conn: asyncpg.Connection, user_id: int) -> _Profile | None:
    """취향 벡터와 태그 가중치. 프로필이 없거나 벡터를 못 만든 사용자면 None."""
    row = await conn.fetchrow(_PROFILE_SQL, user_id)
    if row is None or row["cold_start"] or not row["centroid"]:
        return None
    return _Profile(
        centroid=json.loads(row["centroid"]),
        tag_weights=json.loads(row["tag_weights"]),
        version=row["profile_version"],
        computed_at=row["computed_at"],
    )


async def _tag_weights(
    conn: asyncpg.Connection, req: FeedRequest, page: cursor.Page, profile: _Profile
) -> dict[str, float]:
    """프로필의 태그 가중치에, 프로필이 반영한 뒤 생긴 이력의 카테고리 점수를 더한다(명세 ⑥, #175).

    ⑥ 은 온보딩과 취향 기억이 바뀔 때만 불려서, 그 사이에 산 책·리뷰·담기는 여기서 더해야 순서에
    들어간다. 커서를 받은 시각 뒤의 이력은 빼고 본다 — 제외 규칙과 같게 해야 스크롤 중에 점수가
    바뀌어 목록이 밀리지 않는다.
    """
    recent = await history.category_scores_since(
        conn, req.user_id, profile.computed_at, page.issued_at
    )
    weights = dict(profile.tag_weights)
    for category, points in recent.items():
        weights[category] = weights.get(category, 0) + points
    return weights


async def feed(req: FeedRequest) -> FeedOutcome:
    """커서를 읽고 목록을 만든 뒤, 더 볼 것이 있으면 다음 커서를 붙인다."""
    page = cursor.read(req)

    async with db.get_pool().acquire() as conn:
        profile = await _profile(conn, req.user_id)
        if profile is None:
            cursor.check_mode(page, COLD_START)
            # 개인화를 끈 목록은 모두 0점이다. 하한이 1 이상이면 DB 를 더 볼 것 없이 0건이다
            # (명세: 필터로 0건이면 빈 목록으로 200, 필터를 임의로 풀지 않는다).
            if req.match_score_min:
                return FeedOutcome(items=[])
            items, has_more = await cold_start.fetch(conn, req, page)
            return _outcome(page, req, items, has_more, COLD_START, None)

        tag_weights = await _tag_weights(conn, req, page, profile)

        # 앞 페이지와 모드가 같은지는 목록을 만든 뒤에 본다(① 검색과 같은 순서). 먼저 보면
        # 벡터가 안 되는 동안 받은 커서를 "이번엔 개인화겠지"로 단정해 끊어서, 장애가 이어지는
        # 동안 첫 페이지만 되풀이하게 된다.
        try:
            items, has_more = await personalized.fetch(
                conn, req, page, profile.centroid, tag_weights
            )
        except Exception:
            # 벡터 조회가 어떤 이유로 안 되든 할 일은 같다 — 유사도를 빼고 규칙 점수만으로
            # 답하고 헤더로 알린다(명세 ④). 좁게 잡으면 빠지는 게 생긴다(① 과 같은 판단).
            logger.exception("취향 벡터 조회 실패. 규칙 점수만으로 응답한다")
            cursor.check_mode(page, RULE_ONLY)
            items, has_more = await rule_only.fetch(conn, req, page, tag_weights)
            return _outcome(
                page, req, items, has_more, RULE_ONLY, profile.version, RULE_ONLY
            )
        cursor.check_mode(page, PERSONALIZED)

    return _outcome(page, req, items, has_more, PERSONALIZED, profile.version)


def _outcome(
    page: cursor.Page,
    req: FeedRequest,
    items: list[dict[str, Any]],
    has_more: bool,
    mode: str,
    profile_version: int | None,
    degraded: str | None = None,
) -> FeedOutcome:
    next_cursor = cursor.issue(page, req, mode, profile_version) if has_more else None
    return FeedOutcome(
        items=items,
        next_cursor=next_cursor,
        degraded=degraded,
        cold_start=mode == COLD_START,
    )
