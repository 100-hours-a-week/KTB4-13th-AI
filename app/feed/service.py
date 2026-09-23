"""④ 목록을 고르는 흐름. 지금은 모든 사용자에게 개인화를 끈 목록(cold_start)을 준다.

취향 프로필로 채점하는 목록은 #107 에서 붙인다. 그때 프로필이 없거나 cold_start 인 사용자만 여기로 온다.
"""

from typing import Any

from app.core import db
from app.feed import cold_start
from app.feed.schemas import FeedRequest


async def feed(req: FeedRequest) -> list[dict[str, Any]]:
    # cold_start 목록은 모두 0점이다. 하한이 1 이상이면 DB 를 볼 것 없이 0건이다(명세: 필터로 0건이면
    # 빈 목록으로 200, 필터를 임의로 풀지 않는다).
    if req.match_score_min:
        return []
    async with db.get_pool().acquire() as conn:
        return await cold_start.fetch(conn, req)
