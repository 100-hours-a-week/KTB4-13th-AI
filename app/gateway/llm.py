import json
from typing import Any

import openai
from langchain_core.language_models import BaseChatModel, FakeListChatModel
from langchain_core.messages import BaseMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import OpenAIRefusalError

from app.core.config import get_settings


class LLMUnavailableError(Exception):
    """LLM 호출이 실패했을 때. 호출자(예: chat.py)가 degraded 처리 여부를 결정한다.

    provider 전용 예외(openai.APIError 등)를 여기서 이 타입으로 바꿔 던져서,
    호출하는 쪽은 어떤 provider를 쓰는지 몰라도 되게 한다(게이트웨이 원칙).
    """


def get_chat_model() -> BaseChatModel:
    """체인(`프롬프트 | 모델 | 파서`)에 끼울 채팅 모델을 만든다.

    타임아웃·재시도·JSON 모드 같은 설정을 이 함수 한 곳에만 둬서, 체인을 쓰는
    호출부(③⑤⑦)가 몇 개가 되든 이 설정이 갈라지지 않게 한다.
    """
    settings = get_settings()

    if settings.llm_mock:
        return FakeListChatModel(responses=['{"mock": true}'])  # 개발 중 가짜 응답

    return ChatOpenAI(
        model=settings.llm_model_id,
        base_url=settings.llm_base_url,
        api_key="ollama",  # Ollama는 검사 안 하지만 SDK가 값을 요구함
        timeout=settings.llm_timeout_seconds,
        # 명세: 생성 경로 상한 30초, 넘으면 504. max_retries 기본값은 None이라
        # OpenAI SDK 기본 재시도(2회)가 살아나고, 재시도 사이 대기시간까지 더해져
        # timeout 값만으로는 상한을 보장 못 한다. 재시도는 끄고 timeout 하나로
        # 상한을 정확히 맞춘다.
        max_retries=0,
        model_kwargs={"response_format": {"type": "json_object"}},
    )


def message_text(message: BaseMessage) -> str:
    """모델이 돌려준 메시지에서 글자만 꺼낸다. 체인에서 모델 다음 칸에 끼운다."""
    if not isinstance(message.content, str):
        # AIMessage.content 타입은 str | list. JSON 모드에선 str이 정상이고,
        # 그 밖의 형태는 쓸 수 없는 응답이라 LLMUnavailableError로 통일해
        # degraded 처리 경로를 하나로 유지한다.
        raise LLMUnavailableError("LLM 응답에 문자열 content가 없음")
    return message.content


async def invoke_chain(chain: Runnable, inputs: Any) -> Any:
    """체인을 실행하고, provider 예외를 LLMUnavailableError로 바꿔 던진다.

    체인을 직접 ainvoke 하면 호출부가 openai 예외를 알아야 한다. 체인은 반드시
    이 함수로 실행해서 게이트웨이 원칙(호출부는 provider를 모른다)을 지킨다.
    """
    try:
        return await chain.ainvoke(inputs)
    except (openai.APIError, OpenAIRefusalError) as e:
        # 연결 끊김·타임아웃·rate limit·5xx 전부 APIError 하위. langchain-openai는
        # 이걸 OpenAIConnectionError·OpenAITimeoutError 등으로 감싸 던지는데,
        # 그중 OpenAIRefusalError만 Exception을 직접 상속해서 APIError로는 안
        # 잡히므로 따로 적는다. 이건 with_structured_output 경로에서 던져진다
        # (지금 json_object 경로에선 거부가 빈 content로 와서 parse 단계에서 걸림).
        raise LLMUnavailableError(str(e)) from e
    except (IndexError, KeyError, TypeError) as e:
        # OpenAI 호환 서버가 choices를 빈 목록·null로 줄 때,
        # langchain-openai는 이걸 감싸지 않고 그대로 던진다.
        raise LLMUnavailableError(f"LLM 응답 형식이 잘못됨: {e}") from e


async def complete(prompt: str) -> str:
    """프롬프트 문자열 하나를 보내 답 문자열을 받는다. 체인이 필요 없는 호출용."""
    return await invoke_chain(get_chat_model() | message_text, prompt)


def parse_json_response(raw: str) -> dict:
    """모델이 준 문자열(complete() 결과, 또는 체인의 message_text 다음 칸)에서 JSON을 꺼내 파싱한다.

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
