"""④ 목록을 고르는 흐름 — 프로필이 있으면 채점한 목록, 없으면 개인화를 끈 목록.

취향 벡터를 만들지 못한 사용자(cold_start)와 프로필이 없는 사용자(개인화 추천에 동의하지 않은 사용자)는
같은 길로 간다. 구매·리뷰 이력이 있어도 저장된 취향 벡터가 없으면 개인화하지 않는다(#266).
벡터 조회가 안 될 때는 취향 유사도를 빼고 규칙 점수만으로 답한다(명세 ④: 축소 응답).
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg

from app.core import db, history, isbn
from app.core.config import get_settings
from app.feed import (
    cold_start,
    cursor,
    exclusion,
    interest_page,
    personalized,
    rule_only,
)
from app.feed.schemas import FeedRequest
from app.search import vector

logger = logging.getLogger(__name__)

# 응답 모드. 페이지 사이에 모드가 바뀌면 순서가 아예 달라 커서를 이어 쓸 수 없다.
PERSONALIZED = "personalized"
COLD_START = "cold-start"
RULE_ONLY = "rule-only"
# 저장된 관심사별 목록(#454, #455). 기능 플래그(interest_feed)가 켜져 있고 목록이 있을 때 홈과 더 보기가 읽는다.
INTEREST = "interest"

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
    """저장된 취향 벡터와 태그 가중치. 저장된 벡터가 없으면 None 이다.

    구매·리뷰 이력만으로 임시 취향을 만들던 경로(#246)는 뺐다(#266). BE 는 개인화 추천에 동의한
    사용자만 ⑥ 을 부르므로, 프로필이 없는 사용자는 동의하지 않은 사용자다. 이력이 있다고 개인화하면
    동의를 거스른다.
    """
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


async def _excluded(
    conn: asyncpg.Connection, req: FeedRequest
) -> exclusion.Exclusion | None:
    """목록에서 뺄 책의 조건. 요청에 가진 책이 실려 왔으면 그것으로 뺀다(#330).

    안 실려 왔으면 None 이다. BE 가 아직 보내지 않는 것이라, 각 목록이 지금처럼 복제 표로 뺀다.
    복제를 멈출 때 그 길을 빼고, 그때부터 안 실려 오면 빼는 책이 없는 것이 된다(#307).
    책 표에 없는 ISBN 은 건너뛴다(app.core.isbn).
    """
    owned = req.owned_isbns()
    if owned is None:
        return None
    return exclusion.owned(await isbn.to_book_ids(conn, owned))


async def feed(req: FeedRequest) -> FeedOutcome:
    """커서를 읽고 목록을 만든 뒤, 더 볼 것이 있으면 다음 커서를 붙인다."""
    page = cursor.read(req)

    async with db.get_pool().acquire() as conn:
        excluded = await _excluded(conn, req)
        profile = await _profile(conn, req.user_id)
        if profile is None:
            cursor.check_mode(page, COLD_START)
            # 개인화를 끈 목록은 모두 0점이다. 하한이 1 이상이면 DB 를 더 볼 것 없이 0건이다
            # (명세: 필터로 0건이면 빈 목록으로 200, 필터를 임의로 풀지 않는다).
            if req.match_score_min:
                return FeedOutcome(items=[])
            items, has_more = await cold_start.fetch(conn, req, page, excluded)
            return _outcome(page, req, items, has_more, COLD_START, None)

        # 지금 방식으로 시작한 스크롤은 지금 방식으로 잇는다. 도중에 목록이 생겨도 끊지 않는다
        if get_settings().interest_feed and page.mode in (None, INTEREST):
            saved = await interest_page.stored(conn, req.user_id)
            if saved is not None:
                outcome = await _from_saved(conn, req, page, excluded, saved, profile)
                if outcome is not None:
                    return outcome

        tag_weights = await _tag_weights(conn, req, page, profile)

        # 앞 페이지와 모드가 같은지는 목록을 만든 뒤에 본다(① 검색과 같은 순서). 먼저 보면
        # 벡터가 안 되는 동안 받은 커서를 "이번엔 개인화겠지"로 단정해 끊어서, 장애가 이어지는
        # 동안 첫 페이지만 되풀이하게 된다.
        try:
            items, has_more = await personalized.fetch(
                conn, req, page, profile.centroid, tag_weights, excluded
            )
            # 모델을 바꾸며 책 벡터를 다시 채우는 동안에는 에러 없이 0건이 온다. 필터로 0건인
            # 것과 가르려고, 0건일 때만 책 벡터가 있는지 본다(① 검색의 초기 적재와 같은 판단).
            vectors_ok = bool(items) or await vector.has_any(conn)
            if not vectors_ok:
                logger.warning("책 벡터가 한 권도 없다. 규칙 점수만으로 응답한다")
        except Exception:
            # 벡터 조회가 어떤 이유로 안 되든 할 일은 같다 — 유사도를 빼고 규칙 점수만으로
            # 답하고 헤더로 알린다(명세 ④). 좁게 잡으면 빠지는 게 생긴다(① 과 같은 판단).
            logger.exception("취향 벡터 조회 실패. 규칙 점수만으로 응답한다")
            vectors_ok = False
        if not vectors_ok:
            cursor.check_mode(page, RULE_ONLY)
            items, has_more = await rule_only.fetch(
                conn, req, page, tag_weights, excluded
            )
            return _outcome(
                page, req, items, has_more, RULE_ONLY, profile.version, RULE_ONLY
            )
        cursor.check_mode(page, PERSONALIZED)

    return _outcome(page, req, items, has_more, PERSONALIZED, profile.version)


async def _from_saved(
    conn: asyncpg.Connection,
    req: FeedRequest,
    page: cursor.Page,
    excluded: exclusion.Exclusion | None,
    saved: interest_page.Stored,
    profile: _Profile,
) -> FeedOutcome | None:
    """저장된 관심사별 목록에서 읽는다(#454, #455). 요청 때 벡터를 조회하지 않는다.

    가진 책이 요청에 안 실려 왔으면 다른 목록처럼 복제 표로 뺀다. 더 보기에서 거르고 남은 책이 첫 쪽도
    못 채우면 None 이고, 지금 방식으로 그 자리에서 조회한다(#395 의 (안)). 지금 방식은 벡터 조회에 2초를
    두고, 넘으면 규칙 점수 목록으로 답한다.
    """
    books, version = saved.at(page.feed_version)
    if excluded is None:
        excluded = exclusion.replicated(req.user_id, page.issued_at)
    items = await interest_page.books(conn, req, books, excluded)
    if req.surface == "recommend_more" and page.mode is None and len(items) < req.size:
        return None
    shown, has_more = interest_page.page_of(items, req, page)
    return _outcome(
        page, req, shown, has_more, INTEREST, profile.version, None, version
    )


def _outcome(
    page: cursor.Page,
    req: FeedRequest,
    items: list[dict[str, Any]],
    has_more: bool,
    mode: str,
    profile_version: int | None,
    degraded: str | None = None,
    feed_version: int | None = None,
) -> FeedOutcome:
    next_cursor = (
        cursor.issue(page, req, mode, profile_version, feed_version)
        if has_more
        else None
    )
    return FeedOutcome(
        items=items,
        next_cursor=next_cursor,
        degraded=degraded,
        cold_start=mode == COLD_START,
    )
