import json

import openai
from openai import AsyncOpenAI

from app.core.config import get_settings


class LLMUnavailableError(Exception):
    """LLM 호출이 실패했을 때. 호출자(예: chat.py)가 degraded 처리 여부를 결정한다.

    provider 전용 예외(openai.APIError 등)를 여기서 이 타입으로 바꿔 던져서,
    호출하는 쪽은 어떤 provider를 쓰는지 몰라도 되게 한다(게이트웨이 원칙).
    """


async def complete(prompt: str) -> str:
    settings = get_settings()

    if settings.llm_mock:
        return '{"mock": true}'  # 개발 중 가짜 응답

    client = AsyncOpenAI(
        base_url=settings.llm_base_url,
        api_key="ollama",  # Ollama는 검사 안 하지만 SDK가 값을 요구함
        # 명세: 생성 경로 상한 30초, 넘으면 504. SDK 기본 재시도(2회)를 살려두면
        # 재시도 사이 대기시간까지 더해져 timeout 값만으로는 상한을 보장 못 한다.
        # 재시도는 끄고 timeout 하나로 상한을 정확히 맞춘다.
        max_retries=0,
    )
    try:
        response = await client.chat.completions.create(
            model=settings.llm_model_id,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            timeout=settings.llm_timeout_seconds,
        )
    except openai.APIError as e:
        # 연결 끊김·타임아웃·rate limit·5xx 전부 APIError 하위.
        raise LLMUnavailableError(str(e)) from e

    if not response.choices or response.choices[0].message.content is None:
        # SDK 타입상 content는 Optional[str]. 비워서 오면 여기서도
        # LLMUnavailableError로 통일해 degraded 처리 경로를 하나로 유지한다.
        raise LLMUnavailableError("LLM 응답에 content가 없음")
    return response.choices[0].message.content


def parse_json_response(raw: str) -> dict:
    """complete()가 준 문자열에서 JSON을 꺼내 파싱한다.

    로컬 소형 모델은 종종 ```json ... ``` 코드블록이나 설명 문장으로 JSON을
    감싸서 준다. 첫 `{`부터 마지막 `}`까지만 잘라내면 이런 겉치레를 대부분
    걷어낼 수 있다. 그래도 JSON이 아니면 LLMUnavailableError로 통일한다 —
    호출하는 쪽 입장에선 "연결 실패"든 "이상한 응답"이든 결국 "쓸 수 있는
    답을 못 받았다"는 같은 상황이라 degraded 처리 경로를 하나로 합칠 수 있다.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise LLMUnavailableError(f"LLM 응답에서 JSON을 찾을 수 없음: {raw[:200]!r}")

    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMUnavailableError(f"LLM 응답이 유효한 JSON이 아님: {e}") from e
