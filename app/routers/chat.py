"""③ 대화형 도서 추천: POST /recommendations/chat

명세: docs/design/과제 1 ai-server-api-spec-final copy.md #chat
텍스트 턴과 이미지 턴을 여기서 함께 다룬다 — 같은 핸들러가 후보검색·카드생성
파이프라인을 공유하므로 나누지 않는다(개발 워크플로 위키 §9 참고).
지금은 뼈대만 있다. 실제 spec 갱신·후보검색·LLM 카드 생성은 후속 이슈에서 채운다.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/recommendations", tags=["recommendations"])


@router.post("/chat")
async def chat():
    return {"message": "recommend_success", "data": None}
