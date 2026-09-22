"""④ 개인화 추천 목록: GET /recommendations/feed

명세: docs/wiki/ai/1-model-api/spec.md ④ GET /recommendations/feed
LLM을 쓰지 않는다. ⑦ agent.act의 "골라 담기"가 이 엔드포인트와 같은 취향
스코어링을 재사용한다(get_personalized_candidates, 개발 워크플로 위키 §9).
지금은 뼈대만 있다. 실제 규칙 기반 점수·취향 벡터 유사도 계산은 담당자가
후속 이슈에서 채운다.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.get("/feed")
async def feed():
    return {"message": "feed_success", "data": None}
