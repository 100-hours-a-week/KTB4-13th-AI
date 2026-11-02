"""LLM 게이트웨이 단위 테스트.

실제 LLM(Ollama/상용 API)을 호출하는 테스트는 넣지 않는다 — 네트워크·모델
상태에 따라 결과가 흔들려서 CI에 안 맞는다. parse_json_response·message_text 는
순수 로직, invoke_chain 은 가짜 부품(RunnableLambda)을 끼워 예외 변환만 검증한다.
complete()는 httpx.MockTransport로 응답 본문만 흉내 내 네트워크 없이 provider
예외 변환까지 끝단에서 검증하고, 그 이상의 실제 provider 동작은 수동으로 확인한다.
"""

import asyncio
import itertools
import json
import logging
import time
from types import SimpleNamespace

import httpx
import openai
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langchain_openai.chat_models.base import OpenAIRefusalError

import app.gateway.llm as llm_gateway
from app import main
from app.gateway.llm import (
    LLMTimeoutError,
    LLMUnavailableError,
    complete,
    get_chat_model,
    invoke_chain,
    invoke_chain_with_fallback,
    last_known_status,
    message_text,
    parse_json_response,
    report_models,
    stream_json,
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


# --- get_chat_model(json_mode, model_id) — 도구 호출을 쓰는 ⑦용 (#345) ---


def _chat_openai_kwargs(monkeypatch, **call) -> dict:
    """get_chat_model()이 ChatOpenAI에 넘기는 인자를 가로챈다. 네트워크는 안 탄다."""
    captured: dict = {}

    def factory(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(llm_gateway, "ChatOpenAI", factory)
    # .env가 있는 로컬과 없는 CI에서 같은 값으로 시작하게 필요한 것을 전부 고정한다.
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_MODEL_ID", "openai/gpt-4o-mini")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30")
    get_chat_model(**call)
    return captured


def test_기본값이면_JSON_응답_고정이_실린다(monkeypatch) -> None:
    # ③⑤는 인자 없이 부른다. 이 동작이 바뀌면 안 된다.
    kwargs = _chat_openai_kwargs(monkeypatch)

    assert kwargs["model_kwargs"] == {"response_format": {"type": "json_object"}}


@pytest.mark.parametrize(
    ("call", "expected"),
    [
        pytest.param({}, None, id="안_주면_보내지_않아_모델_기본값"),
        pytest.param({"temperature": 0.0}, 0.0, id="⑤처럼_주면_그_값"),
    ],
)
def test_온도는_준_호출부만_정한다(monkeypatch, call: dict, expected) -> None:
    # ③⑦은 온도를 주지 않는다. ⑤만 낮춘다(#433)
    assert _chat_openai_kwargs(monkeypatch, **call)["temperature"] == expected


def test_json_mode를_끄면_response_format이_안_실린다(monkeypatch) -> None:
    kwargs = _chat_openai_kwargs(monkeypatch, json_mode=False)

    assert kwargs["model_kwargs"] == {}


@pytest.mark.parametrize("json_mode", [True, False])
def test_json_mode와_상관없이_ZDR_재시도_타임아웃은_그대로다(
    monkeypatch, json_mode
) -> None:
    kwargs = _chat_openai_kwargs(monkeypatch, json_mode=json_mode)

    assert kwargs["extra_body"] == {"provider": {"zdr": True}}
    assert kwargs["max_retries"] == 0
    assert kwargs["timeout"] == 30


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        pytest.param(None, "openai/gpt-4o-mini", id="안_주면_LLM_MODEL_ID"),
        pytest.param("openai/gpt-5.4-mini", "openai/gpt-5.4-mini", id="주면_그_모델"),
    ],
)
def test_model_id를_주면_그_모델로_만든다(monkeypatch, model_id, expected) -> None:
    kwargs = _chat_openai_kwargs(monkeypatch, model_id=model_id)

    assert kwargs["model"] == expected


@tool
def _get_cart() -> str:
    """장바구니에 담긴 책 목록을 조회한다."""
    return ""


def test_json_mode를_끄면_도구를_바인딩해도_요청이_나가고_도구_호출을_받는다(
    monkeypatch,
) -> None:
    """실제 원인 재현: json_mode=True로는 이 호출이 요청을 보내기도 전에 ValueError였다.

    ChatOpenAI까지 실제로 거치되 응답만 MockTransport로 흉내 낸다. 요청 본문에
    response_format이 없고 tools가 실렸는지를 와이어에서 본다.
    """
    sent: list[dict] = []
    body = {
        **_OK_BODY,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "_get_cart", "arguments": "{}"},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    real = ChatOpenAI

    def factory(**kwargs):
        kwargs["http_async_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        return real(**kwargs)

    monkeypatch.setattr(llm_gateway, "ChatOpenAI", factory)
    monkeypatch.setenv("LLM_MOCK", "false")

    model = get_chat_model(json_mode=False).bind_tools([_get_cart])
    result = asyncio.run(model.ainvoke("장바구니 보여줘"))

    assert [tc["name"] for tc in result.tool_calls] == ["_get_cart"]
    assert "response_format" not in sent[0]
    assert sent[0]["tools"][0]["function"]["name"] == "_get_cart"


# --- 시간 초과는 다른 장애와 구분한다 — ⑤가 504로 답하는 데 쓴다 (#371) ---


def test_invoke_chain은_시간_초과를_LLMTimeoutError로_바꾼다() -> None:
    request = httpx.Request("POST", "http://localhost:11434/v1")
    chain = _raising(openai.APITimeoutError(request=request))

    with pytest.raises(LLMTimeoutError):
        asyncio.run(invoke_chain(chain, None))

    assert last_known_status() == "unavailable"


def test_시간_초과도_LLMUnavailableError로_잡힌다() -> None:
    # ③은 LLMUnavailableError만 잡아 축소 응답으로 내려간다. 시간 초과가 여기서 빠지면 500이 된다.
    request = httpx.Request("POST", "http://localhost:11434/v1")
    chain = _raising(openai.APITimeoutError(request=request))

    with pytest.raises(LLMUnavailableError):
        asyncio.run(invoke_chain(chain, None))


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            openai.APIConnectionError(request=httpx.Request("POST", "http://x/v1")),
            id="연결_실패",
        ),
        pytest.param(OpenAIRefusalError("거부"), id="거부"),
        pytest.param(IndexError("choices가 빔"), id="응답_형식"),
        pytest.param(LLMUnavailableError("JSON 없음"), id="체인이_직접_던짐"),
    ],
)
def test_시간_초과가_아닌_장애는_LLMTimeoutError가_아니다(exc) -> None:
    # openai의 시간 초과 예외는 연결 실패 예외의 하위 타입이다. 거꾸로 연결 실패까지 시간 초과로
    # 보면 ⑤가 503이어야 할 것을 504로 답한다.
    with pytest.raises(LLMUnavailableError) as exc_info:
        asyncio.run(invoke_chain(_raising(exc), None))

    assert not isinstance(exc_info.value, LLMTimeoutError)


def test_서버가_제한_시간_안에_답하지_않으면_LLMTimeoutError다(monkeypatch) -> None:
    """ChatOpenAI까지 실제로 거친다. httpx 시간 초과가 SDK와 langchain-openai를 지나 무엇으로 오는지 본다."""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("응답 없음", request=request)

    real = ChatOpenAI

    def factory(**kwargs):
        kwargs["http_async_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        return real(**kwargs)

    monkeypatch.setattr(llm_gateway, "ChatOpenAI", factory)
    monkeypatch.setenv("LLM_MOCK", "false")

    with pytest.raises(LLMTimeoutError):
        asyncio.run(complete("아무 프롬프트"))


# --- 서버가 뜰 때 기능별 모델 남기기(#525) ---

# 기능별 설정 이름. 비어 있으면 공용 LLM_MODEL_ID 로 돈다
_OWN_MODELS = ("EXTRACTION_LLM_MODEL_ID", "RELATION_LLM_MODEL_ID", "AGENT_LLM_MODEL_ID")


def _set_models(monkeypatch: pytest.MonkeyPatch, **own: str) -> None:
    """공용 모델은 shared-model, 기능별 설정은 준 것만 채우고 나머지는 비운다."""
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setenv("LLM_MODEL_ID", "shared-model")
    for name in _OWN_MODELS:
        monkeypatch.setenv(name, own.get(name, ""))


def _logged(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


@pytest.mark.parametrize("missing", _OWN_MODELS)
def test_기능별_모델이_비면_공용_모델로_돈다고_경고한다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, missing: str
) -> None:
    # dev·운영 설정에 AGENT_LLM_MODEL_ID 가 빠져 ⑦ 이 평가한 모델(gpt-5.4-mini) 대신 공용 gpt-4o 로
    # 돌았는데, 로그에 아무것도 없어 몰랐다. 빠진 설정 이름과 대신 쓰는 모델이 경고에 나와야 한다
    _set_models(monkeypatch, **{n: "own-model" for n in _OWN_MODELS if n != missing})

    with caplog.at_level(logging.INFO, logger="app.gateway.llm"):
        report_models()

    warnings = _logged(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert missing in warnings[0]
    assert "shared-model" in warnings[0]


def test_기능별_모델을_다_정하면_경고_없이_쓰는_모델을_한_줄로_남긴다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 설정을 넣은 뒤 배포 로그 한 줄로 실제 모델을 확인할 수 있어야 한다
    _set_models(
        monkeypatch,
        EXTRACTION_LLM_MODEL_ID="extraction-model",
        RELATION_LLM_MODEL_ID="relation-model",
        AGENT_LLM_MODEL_ID="agent-model",
    )

    with caplog.at_level(logging.INFO, logger="app.gateway.llm"):
        report_models()

    assert _logged(caplog, logging.WARNING) == []
    infos = _logged(caplog, logging.INFO)
    assert len(infos) == 1
    for model in ("shared-model", "extraction-model", "relation-model", "agent-model"):
        assert model in infos[0]


def test_가짜_LLM이면_모델을_남기지_않는다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # LLM_MOCK 이면 어느 모델도 부르지 않는다. 기능별 설정이 비어도 경고할 일이 아니다
    _set_models(monkeypatch)
    monkeypatch.setenv("LLM_MOCK", "true")

    with caplog.at_level(logging.INFO, logger="app.gateway.llm"):
        report_models()

    assert caplog.records == []


def test_서버가_뜰_때_DB에_붙기_전에_모델을_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 배포 로그에서 보려는 것이라 서버가 뜨는 길에서 불려야 한다. DB 연결에 실패해도 남도록 그 앞에서 부른다
    called: list[str] = []

    class _Stop(Exception):
        pass

    async def _connect() -> None:
        called.append("db")
        raise _Stop

    monkeypatch.setattr(main.cursor, "check_key", lambda: None)
    monkeypatch.setattr(main.llm, "report_models", lambda: called.append("models"))
    monkeypatch.setattr(main.db, "connect", _connect)

    async def _start() -> None:
        async with main.lifespan(main.app):
            pass

    with pytest.raises(_Stop):
        asyncio.run(_start())
    assert called == ["models", "db"]


# --- stream_json — ③ SSE 가 카드 생성 LLM 을 흘려 받는 길 (AI #236) ---


def _collect(chain, inputs=None) -> tuple[dict, list[str]]:
    seen: list[str] = []

    async def on_text(text: str) -> None:
        seen.append(text)

    return asyncio.run(stream_json(chain, inputs, on_text)), seen


def test_stream_json은_지금까지_온_글_전체를_넘기고_다_읽은_JSON을_돌려준다() -> None:
    model = GenericFakeChatModel(
        messages=iter([AIMessage(content='{"reply": "안녕 하세요"}')])
    )

    parsed, seen = _collect(model, "프롬프트")

    assert parsed == {"reply": "안녕 하세요"}
    assert len(seen) > 1
    assert all(later.startswith(earlier) for earlier, later in itertools.pairwise(seen))
    assert seen[-1] == '{"reply": "안녕 하세요"}'


class _Stream:
    """chain.astream 만 흉내 낸다. pieces 를 차례로 내다가 마지막에 error 를 던진다."""

    def __init__(
        self, *pieces: str, error: Exception | None = None, delay: float = 0
    ) -> None:
        self._pieces, self._error, self._delay = pieces, error, delay

    async def astream(self, _inputs):
        for piece in self._pieces:
            await asyncio.sleep(self._delay)
            yield AIMessageChunk(content=piece)
        if self._error is not None:
            raise self._error


def test_stream_json은_흘려_받다_끊기면_LLMUnavailableError고_status가_unavailable이다() -> (
    None
):
    request = httpx.Request("POST", "http://localhost:11434/v1")
    chain = _Stream('{"reply": "안', error=openai.APIConnectionError(request=request))

    with pytest.raises(LLMUnavailableError):
        _collect(chain)

    assert last_known_status() == "unavailable"


def test_stream_json은_다_받은_글이_JSON이_아니면_LLMUnavailableError다() -> None:
    with pytest.raises(LLMUnavailableError):
        _collect(_Stream('{"reply": "끊긴 글'))

    assert last_known_status() == "unavailable"


def test_stream_json은_조각이_계속_와도_전체가_상한을_넘으면_LLMTimeoutError다(
    monkeypatch,
) -> None:
    # SDK timeout 은 조각 사이 대기에만 걸린다. 조각이 0.03초마다 와서 그건 안 넘어도, 전체 0.1초 상한은 넘는다
    monkeypatch.setattr(
        llm_gateway, "get_settings", lambda: SimpleNamespace(llm_timeout_seconds=0.1)
    )
    chain = _Stream(*(["가"] * 10), delay=0.03)

    with pytest.raises(LLMTimeoutError):
        _collect(chain)


def test_stream_json은_ChatOpenAI의_JSON_모드_스트리밍을_끝단에서_읽는다(
    monkeypatch,
) -> None:
    """get_chat_model()이 만드는 ChatOpenAI(JSON 모드)가 실제로 stream 요청을 보내고 조각을 읽는지 본다."""
    pieces = ['{"reply": "비 오는', ' 날"', ', "cards": []}']
    chunks = [
        {
            **_OK_BODY,
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": p},
                    "finish_reason": None,
                }
            ],
        }
        for p in pieces
    ]
    stream_body = (
        "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks)
        + "data: [DONE]\n\n"
    )
    sent: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=stream_body.encode(),
        )

    real = ChatOpenAI

    def factory(**kwargs):
        kwargs["http_async_client"] = httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        )
        return real(**kwargs)

    monkeypatch.setattr(llm_gateway, "ChatOpenAI", factory)
    monkeypatch.setenv("LLM_MOCK", "false")

    parsed, seen = _collect(RunnableLambda(lambda x: x) | get_chat_model(), "프롬프트")

    assert parsed == {"reply": "비 오는 날", "cards": []}
    assert seen == [
        '{"reply": "비 오는',
        '{"reply": "비 오는 날"',
        '{"reply": "비 오는 날", "cards": []}',
    ]
    assert sent[0]["stream"] is True
    assert sent[0]["response_format"] == {"type": "json_object"}


# --- 자체 서빙 LLM(GPU 서버) 주소와, 실패하면 받쳐 둔 LLM 으로 다시 부르기 (#569) ---


def test_주소를_주면_그_서버로_보내고_외부_키와_ZDR은_싣지_않는다(monkeypatch) -> None:
    # GPU 서버의 vLLM 은 OpenRouter 가 아니다. 외부 API 키를 다른 서버로 보내지 않고, OpenRouter 전용 설정도 뺀다
    monkeypatch.setenv("EXTERNAL_AI_API_KEY", "external-key")

    kwargs = _chat_openai_kwargs(
        monkeypatch,
        model_id="gpu-model",
        base_url="http://gpu:8000/v1",
        timeout_seconds=10,
    )

    assert kwargs["base_url"] == "http://gpu:8000/v1"
    assert kwargs["model"] == "gpu-model"
    assert kwargs["api_key"] != "external-key"
    assert kwargs["extra_body"] is None
    assert kwargs["timeout"] == 10
    assert kwargs["max_retries"] == 0


def test_자체_서빙_서버가_키를_검사하면_준_키를_보낸다(monkeypatch) -> None:
    kwargs = _chat_openai_kwargs(
        monkeypatch, base_url="http://gpu:8000/v1", api_key="gpu-key"
    )

    assert kwargs["api_key"] == "gpu-key"


def test_주소를_안_주면_지금처럼_공용_주소와_외부_키로_보낸다(monkeypatch) -> None:
    monkeypatch.setenv("EXTERNAL_AI_API_KEY", "external-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://openrouter.ai/api/v1")

    kwargs = _chat_openai_kwargs(monkeypatch)

    assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
    assert kwargs["api_key"] == "external-key"
    assert kwargs["timeout"] == 30


def _answering(value) -> RunnableLambda:
    return RunnableLambda(lambda _inputs: value)


def _counting(value, calls: list) -> RunnableLambda:
    def _answer(_inputs):
        calls.append(1)
        return value

    return RunnableLambda(_answer)


def test_먼저_부른_LLM이_답하면_받쳐_둔_LLM은_부르지_않는다() -> None:
    calls: list = []

    result = asyncio.run(
        invoke_chain_with_fallback(_answering("GPU"), _counting("API", calls), None)
    )

    assert result == "GPU"
    assert calls == []


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            openai.APIConnectionError(request=httpx.Request("POST", "http://gpu/v1")),
            id="연결_실패",
        ),
        pytest.param(
            openai.APITimeoutError(request=httpx.Request("POST", "http://gpu/v1")),
            id="시간_초과",
        ),
        pytest.param(LLMUnavailableError("JSON 없음"), id="쓸_수_없는_답"),
    ],
)
def test_먼저_부른_LLM이_실패하면_받쳐_둔_LLM으로_다시_부른다(exc) -> None:
    # 스팟 GPU 는 밤 배치 중에 회수될 수 있다. 그래도 그 호출은 API 로 끝나야 한다
    result = asyncio.run(
        invoke_chain_with_fallback(_raising(exc), _answering("API"), None)
    )

    assert result == "API"
    assert last_known_status() == "ok"


def test_둘_다_실패하면_받쳐_둔_LLM의_장애로_끝난다() -> None:
    request = httpx.Request("POST", "http://x/v1")

    with pytest.raises(LLMTimeoutError):
        asyncio.run(
            invoke_chain_with_fallback(
                _raising(openai.APIConnectionError(request=request)),
                _raising(openai.APITimeoutError(request=request)),
                None,
            )
        )

    assert last_known_status() == "unavailable"


def test_LLM_장애가_아닌_예외는_다시_부르지_않고_그대로_던진다() -> None:
    # 코드 결함을 API 로 다시 불러 덮으면 GPU 쪽 결함이 안 보인다
    calls: list = []

    with pytest.raises(ValueError):
        asyncio.run(
            invoke_chain_with_fallback(
                _raising(ValueError("코드 결함")), _counting("API", calls), None
            )
        )

    assert calls == []


def test_다시_부른_시간까지_합쳐_제한_시간을_넘기면_LLMTimeoutError다(
    monkeypatch,
) -> None:
    # 명세는 생성 30초를 넘기면 504 로 답하게 한다. GPU 를 기다린 시간에 API 시간을 다 더하면 그 상한을 넘는다
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "1")

    async def _slow_fail(_inputs):
        await asyncio.sleep(0.6)
        raise LLMUnavailableError("GPU 가 늦게 실패")

    async def _slow_answer(_inputs):
        await asyncio.sleep(0.6)
        return "API"

    started = time.perf_counter()
    with pytest.raises(LLMTimeoutError):
        asyncio.run(
            invoke_chain_with_fallback(
                RunnableLambda(_slow_fail), RunnableLambda(_slow_answer), None
            )
        )

    assert time.perf_counter() - started < 1.5


def test_다시_부를_때_기록에는_장애_종류만_남는다(caplog) -> None:
    # 예외 메시지에는 모델이 쓴 글이 실릴 수 있고, 그 글이 사용자 말을 되풀이할 때가 있다(#202)
    failing = _raising(LLMUnavailableError("사용자가 한 말 그대로"))

    with caplog.at_level(logging.WARNING, logger="app.gateway.llm"):
        asyncio.run(invoke_chain_with_fallback(failing, _answering("API"), None))

    warnings = _logged(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "LLMUnavailableError" in warnings[0]
    assert "사용자가 한 말" not in warnings[0]
