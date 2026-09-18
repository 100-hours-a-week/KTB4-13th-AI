"""② 텍스트 임베딩: POST /embeddings (내부 전용)

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #embeddings
LLM도 DB도 안 쓰는 유일한 엔드포인트라 독립적으로 완성 가능하다.
지금은 뼈대만 있다. 실제 임베딩 모델 호출은 담당자가 후속 이슈에서 채운다.
"""

from fastapi import APIRouter

router = APIRouter(tags=["embeddings"])


@router.post("/embeddings")
async def embeddings():
    return {"message": "embed_success", "data": None}
