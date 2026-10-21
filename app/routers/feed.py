"""④ 개인화 추천 목록: GET /recommendations/feed, ⑨ 대체 자리 다시 뽑기: POST /recommendations/feed/refresh

명세: docs/wiki/ai/1-model-api/spec.md ④ GET /recommendations/feed
LLM을 쓰지 않는다. ⑦ agent.act의 "골라 담기"가 이 엔드포인트와 같은 취향
스코어링을 재사용한다(get_personalized_candidates, 개발 워크플로 위키 §9).
취향 프로필이 있으면 채점한 목록을, 없으면 개인화를 끈 목록(인기순, 신간순)을 준다.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core import categories, db, responses
from app.feed import cursor, flag, refresh, service
from app.feed.schemas import parse_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

# 개인화 목록이라 중간 캐시가 응답을 재사용하면 남의 목록이 다른 사용자에게 나간다(명세 ④).
_NO_STORE = {"Cache-Control": "private, no-store"}


@router.get("/feed")
async def feed(request: Request) -> JSONResponse:
    req = parse_query(request.query_params.multi_items())
    if req is None:
        return responses.error(400, "invalid_request")
    # category 는 온보딩 값만 받는다(#219). 앱에 없는 값은 잘못 보낸 것이라 조용히 0건을 주지 않는다.
    if req.category is not None and not categories.is_onboarding(req.category):
        return responses.error(400, "invalid_request")

    try:
        outcome = await service.feed(req)
    except cursor.CursorExpired:
        # 만료·위조·조건 변경 모두 클라이언트가 할 일은 같다: 첫 페이지부터 다시 요청(명세 ④).
        return responses.error(410, "cursor_expired")
    except Exception:
        # 그대로 두면 FastAPI 기본 500 {"detail": ...} 이 나가 공통 응답 형식이 깨진다(①③과 같음).
        logger.exception("피드 목록 조회 실패")
        return responses.error(500, "internal_server_error")

    headers = dict(_NO_STORE)
    if outcome.degraded:
        headers["X-Degraded"] = outcome.degraded
    return responses.success(
        "feed_success",
        {
            "items": outcome.items,
            "next_cursor": outcome.next_cursor,
            "cold_start": outcome.cold_start,
        },
        headers=headers,
    )


@router.post("/feed/refresh")
async def refresh_feed() -> JSONResponse:
    """⑨ 대체 자리 다시 뽑기(명세 ⑨, #456). 요청 본문은 없다({} 도 받는다).

    기능 플래그(interest_feed)가 꺼져 있으면 저장된 목록이 없으니 다시 뽑을 것도 없다. 새 칸을 읽지 않는다.
    """
    if not flag.on():
        return responses.success("refresh_success", {"refreshed": 0, "remaining": 0})
    try:
        async with db.get_pool().acquire() as conn:
            data = await refresh.refresh(conn)
    except Exception:
        # 명세: 500 이면 BE 는 그날 밤 멈추고 다음 밤에 다시 부른다
        logger.exception("대체 자리 다시 뽑기 실패")
        return responses.error(500, "internal_server_error")
    return responses.success("refresh_success", data)
