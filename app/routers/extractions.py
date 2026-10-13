"""⑤ 취향 기억 추출: POST /preferences/extractions (V2)

명세: docs/wiki/ai/1-model-api/spec.md ⑤ POST /preferences/extractions
BE 가 대화 세션 하나를 실어 보내면 취향 사실을 뽑아 돌려준다. AI 는 입력도 결과도 저장하지 않고
BE 가 저장한다. 지금은 요청 검사와 응답 모양까지만 있고, 뽑는 로직은 #299 의 뒤 단계에서 채운다.
"""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core import body, responses
from app.extraction import service
from app.extraction.schemas import parse_request
from app.gateway.llm import LLMTimeoutError, LLMUnavailableError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/preferences", tags=["preferences"])

# 명세 에러표는 숫자 없이 "상한 초과"라고만 적혀 있다(#99). 쓰는 것은 최근 40턴이지만 BE 는 세션
# 전체를 보낼 수 있다. 40턴이 모두 4,000자(한글 3바이트)여도 0.5MB 라, 그 네 배쯤으로 잡는다.
MAX_BODY_BYTES = 2 * 1024 * 1024


@router.post("/extractions")
async def extract_preferences(request: Request) -> JSONResponse:
    raw = await body.read_limited(request, MAX_BODY_BYTES)
    if raw is None:
        return responses.error(413, "payload_too_large")

    try:
        payload = json.loads(raw)
    except ValueError:
        # JSON 문법 오류와, UTF-8 이 아닌 본문(UnicodeDecodeError) 둘 다 ValueError 다.
        return responses.error(400, "invalid_request")

    req = parse_request(payload)
    if req is None:
        return responses.error(400, "invalid_request")

    extractions: list = []
    # 동의하지 않은 사용자의 대화는 뽑지 않는다(명세 ⑤). 빈 대화는 뽑을 것이 없다.
    if req.consented and req.conversation:
        try:
            extractions = await service.extract(req)
        except LLMUnavailableError as exc:
            # 외부 LLM 이 안 되는 것은 서버 오류(500)가 아니다. BE 가 "나중에 다시 부르면 되는 것"으로
            # 읽게 나눠 답한다(명세 ⑤). 예외 메시지에는 공급자의 오류 문구나 LLM 의 답이 실려 올 수
            # 있어 로그에는 종류만 남긴다.
            logger.warning("취향 기억 추출: LLM 장애 (%s)", type(exc).__name__)
            if isinstance(exc, LLMTimeoutError):
                return responses.error(504, "generation_timeout")
            return responses.error(503, "upstream_unavailable")
        except Exception:
            # 그대로 두면 FastAPI 기본 500 {"detail": ...} 이 나가 공통 응답 형식이 깨진다.
            # 대화 원문은 로그에 남기지 않는다.
            logger.exception("취향 기억 추출 실패")
            return responses.error(500, "internal_server_error")

    return responses.success(
        "extract_success",
        {"extractions": extractions, "nothing_found": not extractions},
    )
