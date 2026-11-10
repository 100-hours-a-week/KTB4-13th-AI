"""판정 전용 모델 Jev 에 고르기 질문 하나를 묻는다(③ 문지기·새 요청 판정).

Jev(TypeSafe)는 글을 쓰지 않는다. 상태 글과 선택지 설명을 받아 고른 선택지와 확신(0~1)만 돌려준다. OpenRouter 의
`POST /api/alpha/decisions` 로 부르는데 채팅 API 와 요청·응답 모양이 달라 ChatOpenAI 로는 못 부른다. 그래서 채팅
게이트웨이(app/gateway/llm.py)와 나란히 둔다. 실패는 같은 LLMUnavailableError 로 바꿔 던져, 호출부가 판정 실패를
한 가지로 다루게 한다.

왜 바꾸나(2026-11-02, 61사례 × 3번, 같은 시간대 짝 비교). 지금 방식(애매한 말은 gpt-4o 판정, 새 요청은 gpt-4o 판정)
86.3% 대 두 판정 모두 Jev 89.6%. 해석 단계 p50 2.07 → 1.38초, 4o 호출 2.60 → 1.08번. 새 요청 판정은 1단계가 끝난 뒤
0.5초만 기다리는데(RELATION_GRACE_SECONDS) 4o 는 그 안에 못 와 19번 버려졌고 Jev 는 0번이었다. 갈래 판정만 따로
재면 정확도는 같다(4o 97.3% 대 Jev 97.0%) — 이득은 시간이다.

Jev 는 ZDR 을 지킨다는 문서가 없다. 채팅 게이트웨이는 LLM_PROVIDER=openrouter 면 ZDR 엔드포인트로만 보내는데(개인정보
국외이전 동의서의 "미저장" 근거), 그 설정에서 Jev 로 보내면 그 약속이 깨진다. 그래서 그때는 켜도 쓰지 않는다.

판정 실패는 ⑧ /health 의 llm 칸에 남기지 않는다. 판정은 실패해도 호출부가 기본값으로 이어 가고, 채팅 LLM 상태와
섞이면 health 가 카드 생성이 되는지를 못 알린다.
"""

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.core.config import Settings, get_settings
from app.gateway.llm import LLMTimeoutError, LLMUnavailableError

logger = logging.getLogger(__name__)

# 질문 하나만 묻는다. 이름은 답을 꺼낼 때만 쓴다
_QUESTION = "decision"


@dataclass(frozen=True)
class Choice:
    choice: str
    # Jev 가 준 확신(0~1). 안 주면 None
    confidence: float | None


def skip_reason(settings: Settings) -> str | None:
    """CHAT_JUDGE=jev 인데 Jev 를 쓰지 않는 까닭. 쓰면(또는 켜지 않았으면) None."""
    if settings.chat_judge != "jev" or settings.llm_mock:
        return None
    if settings.llm_provider == "openrouter":
        # app/gateway/llm.py get_chat_model 이 ZDR 을 켜는 조건과 같다
        return "LLM_PROVIDER=openrouter(ZDR 켬)라 ZDR 을 보장하지 못하는 Jev 로 보내지 않는다"
    if not settings.external_ai_api_key:
        return "EXTERNAL_AI_API_KEY 가 비어 있다"
    return None


def enabled() -> bool:
    """③ 판정 두 개를 Jev 로 물을지."""
    settings = get_settings()
    return (
        settings.chat_judge == "jev"
        and not settings.llm_mock
        and skip_reason(settings) is None
    )


_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _http() -> httpx.AsyncClient:
    """연결을 다시 쓰려고 클라이언트 하나를 둔다. 연결은 만든 이벤트 루프에서만 쓸 수 있어 루프가 바뀌면 새로 만든다."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient()
        _client_loop = loop
    return _client


async def aclose() -> None:
    """서버가 내려갈 때 연결을 닫는다."""
    global _client
    if _client is not None and _client_loop is asyncio.get_running_loop():
        await _client.aclose()
    _client = None


async def choose(
    *, state: str, instructions: str, criteria: dict[str, str], timeout: float
) -> Choice:
    """state 를 보고 criteria 의 선택지 하나를 고르게 한다. 실패·시간 초과·모르는 답이면 LLMUnavailableError.

    timeout 은 호출 전체의 상한이다(httpx 의 제한은 연결·읽기 단계마다 걸려 전체를 못 막는다).
    """
    settings = get_settings()
    body = {
        "model": settings.jev_model_id,
        "state": state,
        "questions": {
            _QUESTION: {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        },
    }
    headers = {"Authorization": f"Bearer {settings.external_ai_api_key}"}
    try:
        async with asyncio.timeout(timeout):
            response = await _http().post(settings.jev_url, json=body, headers=headers)
    except TimeoutError as e:
        raise LLMTimeoutError("Jev 가 제한 시간 안에 답하지 못했다") from e
    except httpx.HTTPError as e:
        # 예외 문구에 주소가 실릴 수 있어 종류만 남긴다
        raise LLMUnavailableError(f"Jev 호출 실패: {type(e).__name__}") from e
    if response.status_code != 200:
        # 응답 본문은 보낸 상태 글(사용자 말)을 되풀이할 수 있어 남기지 않는다(#202)
        raise LLMUnavailableError(f"Jev 가 HTTP {response.status_code} 로 답했다")
    try:
        answer = response.json()["answers"][_QUESTION]
        choice = answer["choice"]
    except (ValueError, KeyError, TypeError) as e:
        raise LLMUnavailableError("Jev 응답 형식이 잘못됨") from e
    if choice not in criteria:
        raise LLMUnavailableError("Jev 가 선택지에 없는 답을 했다")
    confidence = answer.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        confidence = None
    return Choice(choice, float(confidence) if confidence is not None else None)
