"""⑦ 쇼핑 에이전트: POST /agent/act (V2)

명세: 위키 저장소 docs/ai/1-model-api/spec.md ⑦ POST /agent/act (PR #225)
요청 검사 → 그래프(LLM 이 도구를 고르고 코드가 검사·실행) → 답변 검사 → 응답. 대화 원문은 로그에 남기지
않고, 로그에는 호출 수와 지연 같은 숫자만 남긴다.
"""

import json
import logging

import openai
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agent.compare import LlmComparer
from app.agent.graph import run_agent
from app.agent.reply import GroundingFailed, finish
from app.agent.schemas import parse_request
from app.core import body, responses
from app.core.config import get_settings
from app.gateway.commerce_provider import (
    CommerceNotConfigured,
    get_catalog,
    get_commerce,
)
from app.gateway.llm import LLMUnavailableError, get_chat_model

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

# 명세 ⑦ 에러 표에는 413 이 없고 "공통 에러"에만 있다. 입력이 작게 묶여 있어서(메시지 200자,
# 카드 10개) 64KB 면 넉넉하다. ③ 과 같은 값이다.
MAX_BODY_BYTES = 64 * 1024
# 명세: 503 은 Retry-After(대기 초) 뒤에 다시 시도한다
RETRY_AFTER_SECONDS = "5"


def agent_model():
    """⑦ 이 쓰는 LLM. 도구를 쓰므로 JSON 모드를 끄고, AGENT_LLM_MODEL_ID 가 있으면 그 모델을 쓴다."""
    settings = get_settings()
    if settings.llm_mock:
        # llm_mock 의 가짜 모델은 도구 바인딩이 안 된다
        raise LLMUnavailableError("llm_mock 에서는 ⑦ 을 쓸 수 없다")
    return get_chat_model(json_mode=False, model_id=settings.agent_llm_model_id or None)


def _is_timeout(exc: BaseException) -> bool:
    """LLM 장애의 원인이 시간 초과인가. invoke_chain 이 provider 예외를 감싸 던지므로 원인을 따라간다."""
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if (
            isinstance(cause, (TimeoutError, openai.APITimeoutError))
            or "Timeout" in type(cause).__name__
        ):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def _unavailable() -> JSONResponse:
    return responses.error(
        503, "upstream_unavailable", {"Retry-After": RETRY_AFTER_SECONDS}
    )


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

    req = parse_request(payload)
    if req is None:
        return responses.error(400, "invalid_request")

    try:
        gateway = get_commerce(req.user_id)
        catalog = get_catalog()
        model = agent_model()
        # 비교는 소개글을 읽는 별도 호출이다. 도구를 고르는 호출과 메시지를 섞지 않는다
        comparer = LlmComparer(catalog, model)
        run = await run_agent(req, model=model, gateway=gateway, comparer=comparer)
        finished = finish(run)
    except CommerceNotConfigured:
        # 진짜 BE 장바구니는 11단계에서 연결한다. 그 전에 가짜로 답하지 않는다
        logger.warning("⑦ 장바구니가 연결되지 않았다(COMMERCE_MOCK 꺼짐)")
        return _unavailable()
    except GroundingFailed as e:
        logger.warning("⑦ 답변 검사로 답이 남지 않았다 removed=%s", e.removed)
        return responses.error(422, "grounding_failed")
    except LLMUnavailableError as e:
        if _is_timeout(e):
            return responses.error(504, "generation_timeout")
        return _unavailable()
    except Exception as e:  # noqa: BLE001 - 예외 메시지에 대화 원문이 들어갈 수 있어 종류만 남긴다
        # 그대로 두면 FastAPI 기본 500 {"detail": ...} 이 나가 공통 응답 형식이 깨진다. 예외 메시지에
        # 검증 실패한 입력(대화 원문)이 들어갈 수 있어서 종류만 남긴다.
        logger.error("⑦ 처리 실패(%s)", type(e).__name__)
        return responses.error(500, "internal_server_error")

    logger.info(
        "⑦ 완료 llm_calls=%d iterations=%d hit_cap=%s tool_calls=%d removed=%s ms=%.0f",
        run.llm_calls,
        run.iterations,
        run.hit_cap,
        len(run.tool_calls),
        finished.removed,
        sum(t.ms for t in run.timings),
    )
    return responses.success("agent_success", finished.data.model_dump(mode="json"))
