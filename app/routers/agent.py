"""⑦ 쇼핑 에이전트: POST /agent/act (V2)

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #agent
지금은 뼈대만 있다. 실제 tool 루프·grounding은 후속 이슈에서 채운다.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/act")
async def act():
    return {"message": "agent_success", "data": None}
