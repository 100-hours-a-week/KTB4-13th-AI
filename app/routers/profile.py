"""⑥ 취향 프로필 생성: POST /preferences/profile

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #profile
LLM을 쓰지 않는다. 지금은 뼈대만 있다. 실제 취향 프로필 계산(computed_at
포함)은 담당자가 후속 이슈에서 채운다.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.post("/profile")
async def profile():
    return {"message": "profile_success", "data": None}
