"""④ 개인화 추천 목록: GET /recommendations/feed

명세: docs/wiki/ai/1-model-api/spec.md ④ GET /recommendations/feed
LLM을 쓰지 않는다. ⑦ agent.act의 "골라 담기"가 이 엔드포인트와 같은 취향
스코어링을 재사용한다(get_personalized_candidates, 개발 워크플로 위키 §9).
지금은 요청 검사와 응답 모양까지만 있고, 빈 목록을 cold_start 로 답한다.
목록은 후속 이슈에서 채운다(#104).
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core import responses
from app.feed.schemas import parse_query

router = APIRouter(prefix="/recommendations", tags=["recommendations"])

# 개인화 목록이라 중간 캐시가 응답을 재사용하면 남의 목록이 다른 사용자에게 나간다(명세 ④).
_NO_STORE = {"Cache-Control": "private, no-store"}


@router.get("/feed")
async def feed(request: Request) -> JSONResponse:
    req = parse_query(request.query_params.multi_items())
    if req is None:
        return responses.error(400, "invalid_request")

    # 목록이 붙기 전이라 추천할 책이 없다. 필터 없이 0건인 경우는 없다는 명세 규칙은
    # 목록을 채우는 #106 부터 지켜진다.
    return responses.success(
        "feed_success",
        {"items": [], "next_cursor": None, "cold_start": True},
        headers=_NO_STORE,
    )
