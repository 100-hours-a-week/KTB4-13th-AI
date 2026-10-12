"""⑦ 쇼핑 에이전트: POST /agent/act (V2)

명세: 위키 저장소 docs/ai/1-model-api/spec.md ⑦ POST /agent/act (PR #225)
지금은 요청 검사와 응답 모양까지만 있다. tool 반복·grounding 은 #344 의 뒤 단계에서 채운다.
"""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agent.schemas import AgentActData, parse_request
from app.core import body, responses

router = APIRouter(prefix="/agent", tags=["agent"])

# 명세 ⑦ 에러 표에는 413 이 없고 "공통 에러"에만 있다. 입력이 작게 묶여 있어서(메시지 200자,
# 카드 10개) 64KB 면 넉넉하다. ③ 과 같은 값이다.
MAX_BODY_BYTES = 64 * 1024


@router.post("/act")
async def act(request: Request) -> JSONResponse:
    raw = await body.read_limited(request, MAX_BODY_BYTES)
    if raw is None:
        return responses.error(413, "payload_too_large")

    try:
        payload = json.loads(raw)
    except ValueError:
        # JSON 문법 오류와, UTF-8 이 아닌 본문(UnicodeDecodeError) 둘 다 ValueError 다.
        return responses.error(400, "invalid_request")

    if parse_request(payload) is None:
        return responses.error(400, "invalid_request")

    # 처리 로직이 들어오기 전까지는 검사를 통과한 요청에 "쇼핑 요청이 아님"으로 답한다. 호출자(BE)는
    # 이 신호를 일반 챗봇 흐름으로 처리하므로, 먼저 연결해도 사용자가 빈 화면을 보지 않는다.
    data = AgentActData.not_shopping_reply().model_dump(mode="json")
    return responses.success("agent_success", data)
