"""③ 판정 두 개(문지기의 애매한 말 판정, 새 요청 판정)를 판정 전용 모델 Jev 로 묻는 시험.

CHAT_JUDGE=jev 면 두 판정이 LLM 대신 Jev(OpenRouter `POST /api/alpha/decisions`)에 같은 글로 묻는다. Jev 는 고른
선택지와 확신만 돌려준다(app/gateway/decision.py). 기본값(llm)이면 지금과 똑같다. 네트워크는 가짜 HTTP 로 끼운다.
"""

import asyncio
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat import shown, topic_switch
from app.core.config import get_settings
from app.gate import judge as gate_judge
from app.gateway import decision
from app.gateway.llm import LLMTimeoutError, LLMUnavailableError, report_models
from app.main import app
from app.routers import chat

client = TestClient(app)

JEV_URL = "https://openrouter.ai/api/alpha/decisions"
CRITERIA = {"left": "왼쪽", "right": "오른쪽"}


@pytest.fixture
def jev_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_JUDGE", "jev")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter-no-zdr")
    monkeypatch.setenv("EXTERNAL_AI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MOCK", "false")
    get_settings.cache_clear()


def _jev(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Jev 로 가는 HTTP 를 handler 로 받는다. 보낸 요청을 모아 돌려준다."""
    sent: list[httpx.Request] = []

    async def _record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return await handler(request)

    monkeypatch.setattr(
        decision,
        "_http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_record)),
    )
    return sent


def _answer(choice: object, confidence: object = 0.97) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "typesafe/jev-1.14-20261014",
            "answers": {"decision": {"choice": choice, "confidence": confidence}},
            "usage": {"input_tokens": 600},
        },
    )


def _choose(**overrides) -> decision.Choice:
    kwargs = {
        "state": "이번 말: 왼쪽",
        "instructions": "고르라",
        "criteria": CRITERIA,
        "timeout": 1.0,
    } | overrides
    return asyncio.run(decision.choose(**kwargs))


def _forbid_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_llm(**_kwargs):
        raise AssertionError("Jev 로 물으면 LLM 을 부르지 않는다")

    monkeypatch.setattr(chat, "get_chat_model", _no_llm)


# --- 켜는 조건 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "on"),
    [
        ({}, False),  # 기본값은 지금처럼 LLM
        ({"CHAT_JUDGE": "jev"}, True),
        # 채팅 게이트웨이가 ZDR 을 켜는 설정이면 ZDR 을 보장 못 하는 Jev 로 보내지 않는다
        ({"CHAT_JUDGE": "jev", "LLM_PROVIDER": "openrouter"}, False),
        ({"CHAT_JUDGE": "jev", "EXTERNAL_AI_API_KEY": ""}, False),
        ({"CHAT_JUDGE": "jev", "LLM_MOCK": "true"}, False),
    ],
)
def test_Jev는_켰고_ZDR을_요구하지_않고_키가_있을_때만_쓴다(
    monkeypatch: pytest.MonkeyPatch, env: dict, on: bool
) -> None:
    base = {
        "CHAT_JUDGE": "llm",
        "LLM_PROVIDER": "openrouter-no-zdr",
        "EXTERNAL_AI_API_KEY": "test-key",
        "LLM_MOCK": "false",
    }
    for name, value in (base | env).items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()

    assert decision.enabled() is on


# --- Jev 호출 ------------------------------------------------------------------


def test_고르기_질문_하나를_보내고_고른_답과_확신을_받는다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    async def _ok(_request: httpx.Request) -> httpx.Response:
        return _answer("left", 0.93)

    sent = _jev(monkeypatch, _ok)

    picked = _choose()

    assert picked == decision.Choice("left", 0.93)
    (request,) = sent
    assert str(request.url) == JEV_URL
    assert request.headers["authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "model": "typesafe/jev-1.14",
        "state": "이번 말: 왼쪽",
        "questions": {
            "decision": {
                "type": "choice",
                "instructions": "고르라",
                "criteria": CRITERIA,
            }
        },
    }


def test_확신이_없거나_숫자가_아니면_None이다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    async def _ok(_request: httpx.Request) -> httpx.Response:
        return _answer("right", "높음")

    _jev(monkeypatch, _ok)

    assert _choose() == decision.Choice("right", None)


@pytest.mark.parametrize(
    "response",
    [
        _answer("middle"),  # 선택지에 없는 답
        httpx.Response(200, json={"answers": {}}),
        httpx.Response(200, content=b"not json"),
    ],
)
def test_쓸_수_없는_답이면_LLMUnavailableError다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None, response: httpx.Response
) -> None:
    async def _bad(_request: httpx.Request) -> httpx.Response:
        return response

    _jev(monkeypatch, _bad)

    with pytest.raises(LLMUnavailableError):
        _choose()


def test_HTTP_오류면_상태만_남기고_본문은_싣지_않는다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    # 오류 본문이 보낸 상태 글(사용자 말)을 되풀이할 수 있다. 예외 문구는 로그에 남는다(#202)
    async def _err(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="이번 말: 사용자가 친 글")

    _jev(monkeypatch, _err)

    with pytest.raises(LLMUnavailableError) as exc_info:
        _choose()
    assert "429" in str(exc_info.value)
    assert "사용자가 친 글" not in str(exc_info.value)


def test_연결이_끊기면_LLMUnavailableError다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    async def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 거부", request=request)

    _jev(monkeypatch, _down)

    with pytest.raises(LLMUnavailableError):
        _choose()


def test_제한_시간을_넘기면_LLMTimeoutError다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _answer("left")

    _jev(monkeypatch, _slow)

    with pytest.raises(LLMTimeoutError):
        _choose(timeout=0.01)


# --- 문지기의 애매한 말 판정 ------------------------------------------------------


def _req(message: str, recent_turns: list[dict] | None = None):
    req = chat.parse_request(
        {
            "user_id": 123,
            "consented": True,
            "spec": {
                "intent": "semantic",
                "exact": {"title": None, "author": None, "publisher": None},
                "filters": {},
                "semantic": None,
                "anchor_book": None,
                "exclude": [],
            },
            "message": message,
            "recent_turns": recent_turns or [],
            "exclude_book_ids": [],
            "image_ref": None,
        }
    )
    assert not isinstance(req, tuple)
    return req


def test_문지기_판정은_LLM과_같은_글을_Jev에_묻는다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    async def _card(_request: httpx.Request) -> httpx.Response:
        return _answer("card")

    sent = _jev(monkeypatch, _card)
    _forbid_llm(monkeypatch)
    req = _req(
        "2번은 어떤 내용이야?",
        [
            {"role": "user", "text": "꿈 이야기 책"},
            {"role": "assistant", "text": "꿈을 다룬 세 권이에요."},
        ],
    )
    books = [shown.ShownBook(1, 1088, "달러구트 꿈 백화점", "이미예", "소개")]

    lane = asyncio.run(chat._judge_lane(req, [(1, "a"), (2, "b")], books))

    assert lane == "card"
    question = json.loads(sent[0].content)["questions"]["decision"]
    assert json.loads(sent[0].content)["state"] == (
        '직전 챗봇 말: "꿈을 다룬 세 권이에요."\n'
        "화면에 보이는 책 카드: 1번 『달러구트 꿈 백화점』, 2번\n"
        '이번 말: "2번은 어떤 내용이야?"'
    )
    assert question["instructions"] == gate_judge.INSTRUCTIONS
    assert question["criteria"] == gate_judge.CRITERIA


def test_LLM에_묻는_문지기_글은_Jev에_보내는_조각을_이은_것이다() -> None:
    text = gate_judge.LANE_PROMPT.format_messages(
        last_bot="(없음)", cards="(없음)", message="다른 거"
    )[0].content

    assert text.startswith(gate_judge.INSTRUCTIONS)
    assert (
        gate_judge.STATE.format(last_bot="(없음)", cards="(없음)", message="다른 거")
        in text
    )
    for lane, criterion in gate_judge.CRITERIA.items():
        assert f"- {lane}: {criterion}\n" in text


@pytest.mark.parametrize(
    "handler_answer",
    [httpx.Response(503), _answer("account")],
)
def test_Jev_문지기_판정이_실패하면_None이다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None, handler_answer: httpx.Response
) -> None:
    # None 이면 호출부는 규칙의 기본값으로 간다. LLM 판정이 실패했을 때와 같다
    async def _bad(_request: httpx.Request) -> httpx.Response:
        return handler_answer

    _jev(monkeypatch, _bad)
    _forbid_llm(monkeypatch)

    assert asyncio.run(chat._judge_lane(_req("다른 거"), [], [])) is None


def test_Jev_문지기_판정도_같은_시간_제한을_쓴다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    async def _slow(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _answer("shop")

    _jev(monkeypatch, _slow)
    monkeypatch.setattr(chat, "GATE_JUDGE_TIMEOUT_SECONDS", 0.01)

    assert asyncio.run(chat._judge_lane(_req("다른 거"), [], [])) is None


def test_기본값이면_문지기_판정은_Jev를_부르지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _never(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("기본값이면 Jev 를 부르지 않는다")

    _jev(monkeypatch, _never)
    model = RunnableLambda(lambda _prompt: AIMessage(content='{"lane": "shop"}'))
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    assert asyncio.run(chat._judge_lane(_req("다른 거"), [], [])) == "shop"


# --- 새 요청 판정 ---------------------------------------------------------------


@pytest.mark.parametrize(("choice", "new"), [("new", True), ("same", False)])
def test_새_요청_판정은_LLM과_같은_글을_Jev에_묻는다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None, choice: str, new: bool
) -> None:
    async def _ok(_request: httpx.Request) -> httpx.Response:
        return _answer(choice)

    sent = _jev(monkeypatch, _ok)
    _forbid_llm(monkeypatch)

    assert asyncio.run(chat._asks_new_request("요리책 보여줘", "찾는 것: 소설")) is new
    body = json.loads(sent[0].content)
    assert body["state"] == '이전 요청: 찾는 것: 소설\n이번 메시지: "요리책 보여줘"'
    question = body["questions"]["decision"]
    assert question["instructions"] == topic_switch.RELATION_INSTRUCTIONS
    assert question["criteria"] == topic_switch.RELATION_CRITERIA


def test_LLM에_묻는_새_요청_글은_Jev에_보내는_조각을_이은_것이다() -> None:
    text = topic_switch.RELATION_PROMPT.format_messages(
        previous="찾는 것: 소설", message="요리책"
    )[0].content

    assert text.startswith(topic_switch.RELATION_INSTRUCTIONS)
    assert (
        topic_switch.RELATION_STATE.format(previous="찾는 것: 소설", message="요리책")
        in text
    )
    for name, criterion in topic_switch.RELATION_CRITERIA.items():
        assert f"- {name}: {criterion}\n" in text


def test_Jev_새_요청_판정이_실패하면_이어가는_말로_본다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    async def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 거부", request=request)

    _jev(monkeypatch, _down)
    _forbid_llm(monkeypatch)

    assert (
        asyncio.run(chat._asks_new_request("요리책 보여줘", "찾는 것: 소설")) is False
    )


def test_Jev가_새_요청이라_하면_한_턴_안에서_앞_조건이_풀린다(
    monkeypatch: pytest.MonkeyPatch, jev_on: None
) -> None:
    """요청 하나를 끝까지 돈다. 판정은 Jev, 1단계·다시 뽑기·카드는 LLM 이다."""

    async def _new(_request: httpx.Request) -> httpx.Response:
        return _answer("new")

    sent = _jev(monkeypatch, _new)
    prompts: list[str] = []

    def _llm(prompt) -> AIMessage:
        text = prompt.to_string()
        prompts.append(text)
        if "대화 분석기" in text:
            raise AssertionError("새 요청 판정은 Jev 가 한다")
        if "조건(spec) 갱신기" in text:
            return AIMessage(content=json.dumps({"semantic": "요리책"}))
        return AIMessage(content='{"cards": []}')

    model = RunnableLambda(_llm)
    monkeypatch.setattr(chat, "get_chat_model", lambda **_kwargs: model)

    async def _candidates(*_args, **_kwargs) -> list[dict]:
        return []

    monkeypatch.setattr(chat, "get_candidates", _candidates)

    res = client.post(
        "/recommendations/chat",
        json={
            "user_id": 123,
            "consented": True,
            "spec": {
                "intent": "semantic",
                "exact": {"title": None, "author": None, "publisher": None},
                "filters": {"price_max": 17500},
                "semantic": "소설",
                "anchor_book": None,
                "exclude": [],
            },
            "message": "요리책 보여줘",
            "recent_turns": [],
            "exclude_book_ids": [],
            "image_ref": None,
        },
    )

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["spec"]["filters"]["price_max"] is None
    assert data["spec"]["semantic"] == "요리책"
    assert len(sent) == 1
    # 1단계와 빈 조건에서 다시 뽑기, 두 번
    assert sum("조건(spec) 갱신기" in p for p in prompts) == 2


# --- 서버가 뜰 때 남기는 줄 ------------------------------------------------------


def _models_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL_ID", "shared-model")
    monkeypatch.setenv("EXTRACTION_LLM_MODEL_ID", "extraction-model")
    monkeypatch.setenv("AGENT_LLM_MODEL_ID", "agent-model")
    monkeypatch.setenv("RELATION_LLM_MODEL_ID", "")


def _logged(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


def test_Jev로_물으면_그_모델을_남기고_새_요청_판정_모델은_경고하지_않는다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, jev_on: None
) -> None:
    _models_env(monkeypatch)

    with caplog.at_level(logging.INFO, logger="app.gateway.llm"):
        report_models()

    assert _logged(caplog, logging.WARNING) == []
    (info,) = _logged(caplog, logging.INFO)
    assert "typesafe/jev-1.14" in info


def test_Jev를_켰는데_ZDR_설정이면_LLM으로_묻는다고_경고한다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, jev_on: None
) -> None:
    _models_env(monkeypatch)
    monkeypatch.setenv("RELATION_LLM_MODEL_ID", "relation-model")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    with caplog.at_level(logging.INFO, logger="app.gateway.llm"):
        report_models()

    (warning,) = _logged(caplog, logging.WARNING)
    assert "CHAT_JUDGE" in warning
    assert "ZDR" in warning
    (info,) = _logged(caplog, logging.INFO)
    assert "jev" not in info
