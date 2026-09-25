"""LLM 게이트웨이 단위 테스트.

실제 LLM(Ollama/상용 API)을 호출하는 테스트는 넣지 않는다 — 네트워크·모델
상태에 따라 결과가 흔들려서 CI에 안 맞는다. parse_json_response·message_text 는
순수 로직, invoke_chain 은 가짜 부품(RunnableLambda)을 끼워 예외 변환만 검증한다.
complete()는 httpx.MockTransport로 응답 본문만 흉내 내 네트워크 없이 provider
예외 변환까지 끝단에서 검증하고, 그 이상의 실제 provider 동작은 수동으로 확인한다.
"""

import asyncio

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import OpenAIRefusalError

import app.gateway.llm as llm_gateway
from app.gateway.llm import (
    LLMUnavailableError,
    complete,
    invoke_chain,
    last_known_status,
    message_text,
    parse_json_response,
)


@pytest.fixture(autouse=True)
def _reset_llm_status() -> None:
    # last_known_status()는 모듈 전역이라 테스트끼리 값이 새면 순서에 따라
    # 결과가 흔들린다. 매 테스트를 ok로 시작해 서로 독립되게 한다.
    llm_gateway._llm_ok = True


def test_순수_json이면_그대로_파싱한다() -> None:
    assert parse_json_response('{"mood": "잔잔함"}') == {"mood": "잔잔함"}


def test_마크다운_코드블록으로_감싸도_벗겨서_파싱한다() -> None:
    # 소형 로컬 모델이 ```json ... ``` 형태로 감싸서 주는 경우가 있다.
    raw = '```json\n{"mood": "잔잔함"}\n```'
    assert parse_json_response(raw) == {"mood": "잔잔함"}


def test_앞뒤에_설명_문장이_붙어도_json만_뽑아낸다() -> None:
    raw = '물론이죠! 요청하신 결과는 다음과 같습니다:\n{"mood": "잔잔함"}\n감사합니다.'
    assert parse_json_response(raw) == {"mood": "잔잔함"}


def test_json이_없으면_LLMUnavailableError를_던진다() -> None:
    with pytest.raises(LLMUnavailableError):
        parse_json_response("죄송하지만 답변을 드릴 수 없습니다.")


def test_중괄호는_있지만_형식이_깨지면_LLMUnavailableError를_던진다() -> None:
    with pytest.raises(LLMUnavailableError):
        parse_json_response('{"mood": 잔잔함 (따옴표 없음)}')


def test_json이_없으면_예외_메시지에_원문이_안_실린다() -> None:
    """#202 — 모델이 JSON 대신 사용자 발화를 되풀이해 답할 때가 있다(실제 재현).

    이 예외는 chat.py의 logger.exception()이 그대로 ERROR 로그에 남기므로,
    메시지에 원문을 실으면 대화 원문이 로그에 남는 셈이 된다(명세: 로그에서 가림).
    """
    raw = "사용자가 물어본 비 오는 날 읽을 책 추천해달라는 그 말 그대로 되풀이함"
    with pytest.raises(LLMUnavailableError) as exc_info:
        parse_json_response(raw)
    assert raw not in str(exc_info.value)


def test_message_text는_문자열_content를_그대로_꺼낸다() -> None:
    assert message_text(AIMessage(content='{"mood": "잔잔함"}')) == '{"mood": "잔잔함"}'


def test_message_text는_문자열이_아닌_content면_LLMUnavailableError를_던진다() -> None:
    with pytest.raises(LLMUnavailableError):
        message_text(AIMessage(content=[{"type": "text", "text": "블록 형태"}]))


def _raising(exc: Exception) -> RunnableLambda:
    def _raise(_inputs) -> None:
        raise exc

    return RunnableLambda(_raise)


def test_invoke_chain은_정상이면_체인_결과를_그대로_돌려준다() -> None:
    chain = RunnableLambda(lambda x: x + 1)
    assert asyncio.run(invoke_chain(chain, 1)) == 2


def test_invoke_chain은_openai_APIError를_LLMUnavailableError로_바꾼다() -> None:
    request = httpx.Request("POST", "http://localhost:11434/v1")
    chain = _raising(openai.APIConnectionError(request=request))

    with pytest.raises(LLMUnavailableError):
        asyncio.run(invoke_chain(chain, None))


def test_invoke_chain은_OpenAIRefusalError도_LLMUnavailableError로_바꾼다() -> None:
    # APIError를 상속하지 않아 except openai.APIError만으로는 새어나가던 예외(PR #68 리뷰).
    chain = _raising(OpenAIRefusalError("요청을 도와드릴 수 없습니다."))

    with pytest.raises(LLMUnavailableError):
        asyncio.run(invoke_chain(chain, None))


def test_invoke_chain은_choices가_비정상이면_LLMUnavailableError로_바꾼다() -> None:
    # choices가 빈 목록·null이면 langchain-openai가 IndexError·TypeError를
    # 감싸지 않고 그대로 던진다(#68에서 complete()에 먼저 고친 것과 같은 증상.
    # invoke_chain은 그보다 먼저 갈라져 나온 채로 만들어져 못 물려받았다 — PR #74 리뷰).
    chain = _raising(IndexError("list index out of range"))

    with pytest.raises(LLMUnavailableError):
        asyncio.run(invoke_chain(chain, None))


def test_invoke_chain은_모르는_예외는_그대로_던진다() -> None:
    # 설정 오류·코드 버그까지 "LLM 장애"로 삼켜 degraded 처리하면 원인이 가려진다.
    chain = _raising(ValueError("설정 오류"))

    with pytest.raises(ValueError):
        asyncio.run(invoke_chain(chain, None))


def test_invoke_chain_성공하면_last_known_status가_ok다() -> None:
    llm_gateway._llm_ok = False  # 직전 실패가 있었다고 가정하고 시작.
    chain = RunnableLambda(lambda x: x)

    asyncio.run(invoke_chain(chain, None))

    assert last_known_status() == "ok"


def test_invoke_chain_실패하면_last_known_status가_unavailable이다() -> None:
    request = httpx.Request("POST", "http://localhost:11434/v1")
    chain = _raising(openai.APIConnectionError(request=request))

    with pytest.raises(LLMUnavailableError):
        asyncio.run(invoke_chain(chain, None))

    assert last_known_status() == "unavailable"


def test_체인이_LLMUnavailableError를_직접_던져도_status가_unavailable이다() -> None:
    # message_text·parse_json_response는 위 두 except에 안 걸리고 이 경로로 온다.
    chain = _raising(LLMUnavailableError("LLM 응답에서 JSON을 찾을 수 없음"))

    with pytest.raises(LLMUnavailableError):
        asyncio.run(invoke_chain(chain, None))

    assert last_known_status() == "unavailable"


def test_모르는_예외는_last_known_status를_안_바꾼다() -> None:
    # 설정 오류·코드 버그는 LLM 가용성 문제가 아니므로 health 판정에 안 섞는다.
    chain = _raising(ValueError("설정 오류"))

    with pytest.raises(ValueError):
        asyncio.run(invoke_chain(chain, None))

    assert last_known_status() == "ok"


_OK_BODY = {"id": "1", "object": "chat.completion", "created": 1, "model": "m"}


def _call_with_response(monkeypatch, body: dict) -> str:
    """LLM 서버가 body를 돌려준다고 가정하고 complete()를 부른다.

    invoke_chain 테스트는 가짜 체인으로 예외 변환만 보지만, 이 테스트는
    get_chat_model()이 실제로 만드는 ChatOpenAI까지 거쳐 provider 응답 형식
    (choices 등)을 langchain-openai가 어떻게 다루는지까지 끝단에서 본다.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    real = ChatOpenAI

    def factory(**kwargs):
        kwargs["http_async_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        return real(**kwargs)

    monkeypatch.setattr(llm_gateway, "ChatOpenAI", factory)
    monkeypatch.setenv("LLM_MOCK", "false")
    return asyncio.run(complete("아무 프롬프트"))


def test_정상_응답은_content를_그대로_돌려준다(monkeypatch) -> None:
    body = {
        **_OK_BODY,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": '{"a": 1}'},
                "finish_reason": "stop",
            }
        ],
    }
    assert _call_with_response(monkeypatch, body) == '{"a": 1}'


@pytest.mark.parametrize(
    "choices",
    [
        pytest.param([], id="빈_목록"),
        pytest.param(None, id="null"),
    ],
)
def test_choices가_비정상이면_LLMUnavailableError(monkeypatch, choices) -> None:
    # OpenAI 호환 서버가 형식이 깨진 응답을 주는 경우.
    # LLMUnavailableError로 통일돼야 ③이 degraded로 내려간다.
    with pytest.raises(LLMUnavailableError):
        _call_with_response(monkeypatch, {**_OK_BODY, "choices": choices})
