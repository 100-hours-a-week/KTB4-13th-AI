"""⑤ 취향 기억 추출: POST /preferences/extractions (V2)

명세: docs/wiki/ai/1-model-api/spec.md ⑤ POST /preferences/extractions
지금은 뼈대만 있다. 실제 LLM 추출 로직은 후속 이슈에서 채운다.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.post("/extractions")
async def extractions():
    return {"message": "extract_success", "data": None}
