"""⑤ POST /preferences/extractions 라우터 테스트 — 요청 검사와 응답 모양(#339). DB·LLM 없이 돈다."""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.extraction.schemas import (
    MAX_CONVERSATION_TURNS,
    MAX_TURN_CHARS,
    parse_request,
)
from app.gateway.llm import LLMTimeoutError, LLMUnavailableError
from app.main import app
from app.routers import extractions

client = TestClient(app)

URL = "/preferences/extractions"


@pytest.fixture(autouse=True)
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> dict:
    """이 파일의 어떤 테스트도 진짜 LLM 과 임베딩 모델을 부르지 않게 한다.

    로컬 `.env` 에 실제 키가 있으면, 가짜로 바꾸지 않은 테스트 하나가 돈이 드는 호출을 한다.
    기본 답은 "뽑을 것 없음"이고, 테스트가 `fake_llm["reply"]` 를 바꿔 다른 답을 시킨다.
    """
    state = {"reply": '{"extractions": []}', "calls": 0}

    def _answer(_prompt) -> AIMessage:
        state["calls"] += 1
        return AIMessage(content=state["reply"])

    async def _embed(texts: list[str], purpose: str):
        return [[0.1, 0.2] for _ in texts], 2, "fake-model"

    monkeypatch.setattr(
        extractions.service,
        "get_chat_model",
        lambda **_kwargs: RunnableLambda(_answer),
    )
    monkeypatch.setattr(extractions.service.embedding, "embed", _embed)
    return state


def _request(**overrides) -> dict:
    """명세 ⑤의 입력 예시."""
    body = {
        "user_id": 123,
        "consented": True,
        "conversation": [
            {"role": "user", "text": "비 오는 날 읽을 책 추천해줘"},
            {"role": "assistant", "text": "잔잔한 소설 위주로 골라봤어요."},
            {"role": "user", "text": "이별 후에 위로가 될 만한 걸로"},
        ],
        "conversation_id": "cv_20260903_a1",
        "existing_preferences": [{"type": "mood", "value": "잔잔한 에세이를 선호함"}],
    }
    body.update(overrides)
    return body


def test_명세의_예시_요청이면_200과_응답_모양을_돌려준다() -> None:
    res = client.post(URL, json=_request())

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "extract_success"
    assert set(body["data"]) == {"extractions", "nothing_found"}
    assert isinstance(body["data"]["extractions"], list)


def test_동의하지_않은_사용자는_뽑지_않고_nothing_found로_답한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _must_not_run(req):
        raise AssertionError("동의하지 않은 사용자의 대화를 뽑으려 했다")

    monkeypatch.setattr(extractions.service, "extract", _must_not_run)

    res = client.post(URL, json=_request(consented=False))

    assert res.status_code == 200
    assert res.json()["data"] == {"extractions": [], "nothing_found": True}


def test_뽑은_것이_있으면_extractions에_싣고_nothing_found는_false다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    found = {
        "type": "mood",
        "value": "이별 후 위로되는 잔잔한 소설을 찾음",
        "confidence": 0.82,
        "vector": [0.01, -0.04],
        "dim": 2,
        "source_conversation_id": "cv_20260903_a1",
    }

    async def _extract(req):
        return [found]

    monkeypatch.setattr(extractions.service, "extract", _extract)

    res = client.post(URL, json=_request())

    assert res.json()["data"] == {"extractions": [found], "nothing_found": False}


def test_뽑은_것이_없으면_빈_목록과_nothing_found_true다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _extract(req):
        return []

    monkeypatch.setattr(extractions.service, "extract", _extract)

    res = client.post(URL, json=_request())

    assert res.json()["data"] == {"extractions": [], "nothing_found": True}


def test_뽑다가_실패하면_공통_형식의_500이다(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(req):
        raise RuntimeError("장애")

    monkeypatch.setattr(extractions.service, "extract", _boom)

    res = client.post(URL, json=_request())

    assert res.status_code == 500
    assert res.json() == {"message": "internal_server_error", "data": None}


# ---------------------------------------------------------------------------
# 요청 검사
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field", ["user_id", "consented", "conversation", "conversation_id"]
)
def test_필수_칸이_없으면_400이다(field: str) -> None:
    body = _request()
    del body[field]

    res = client.post(URL, json=body)

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


@pytest.mark.parametrize("value", [None, []])
def test_existing_preferences는_없거나_null이거나_비어도_된다(value: object) -> None:
    body = _request(existing_preferences=value)

    assert client.post(URL, json=body).status_code == 200
    assert parse_request(body).existing_preferences == []

    del body["existing_preferences"]
    assert parse_request(body).existing_preferences == []


def test_메시지가_4000자면_통과하고_넘으면_400이다() -> None:
    def _post(chars: int) -> int:
        turn = {"role": "user", "text": "가" * chars}
        return client.post(URL, json=_request(conversation=[turn])).status_code

    assert _post(MAX_TURN_CHARS) == 200
    assert _post(MAX_TURN_CHARS + 1) == 400


def test_대화가_40턴을_넘으면_거절하지_않고_최근_40턴만_쓴다() -> None:
    turns = [{"role": "user", "text": f"말 {i}"} for i in range(55)]
    body = _request(conversation=turns)

    assert client.post(URL, json=body).status_code == 200

    used = parse_request(body).used_conversation()
    assert len(used) == MAX_CONVERSATION_TURNS
    assert (used[0].text, used[-1].text) == ("말 15", "말 54")


def test_대화가_비어_있으면_뽑을_것이_없다고_답한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _must_not_run(req):
        raise AssertionError("빈 대화를 뽑으려 했다")

    monkeypatch.setattr(extractions.service, "extract", _must_not_run)

    res = client.post(URL, json=_request(conversation=[]))

    assert res.status_code == 200
    assert res.json()["data"] == {"extractions": [], "nothing_found": True}


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_id": "123"},  # 문자열을 숫자로 바꿔 받지 않는다
        {"user_id": 2_147_483_648},
        {"consented": "true"},
        {"consented": None},
        {"conversation": None},
        {"conversation": {"role": "user", "text": "책"}},  # 목록이 아니다
        {"conversation": [{"role": "user"}]},
        {"conversation": [{"text": "책"}]},
        {"conversation": [{"role": "", "text": "책"}]},
        {"conversation": [{"role": "user", "text": None}]},
        {"conversation": [None]},
        {"conversation": [{"role": "user", "text": "책\x00"}]},
        {"conversation_id": ""},
        {"conversation_id": 123},
        {"conversation_id": "가" * 201},
        {"existing_preferences": [{"type": "mood"}]},
        {"existing_preferences": [{"value": "잔잔한 에세이"}]},
        {"existing_preferences": {"type": "mood", "value": "잔잔한 에세이"}},
    ],
)
def test_형식이_계약과_다르면_400이다(overrides: dict) -> None:
    res = client.post(URL, json=_request(**overrides))

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


@pytest.mark.parametrize("content", [b"not json", b"\xff\xfe{", b"[1, 2]"])
def test_본문이_JSON_객체가_아니면_400이다(content: bytes) -> None:
    res = client.post(
        URL, content=content, headers={"Content-Type": "application/json"}
    )

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_본문이_상한을_넘으면_413이다() -> None:
    body = json.dumps(_request()).encode() + b" " * extractions.MAX_BODY_BYTES

    res = client.post(URL, content=body, headers={"Content-Type": "application/json"})

    assert res.status_code == 413
    assert res.json() == {"message": "payload_too_large", "data": None}


def test_대화_원문을_로그에_남기지_않는다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 뽑다가 실패해도 대화 내용이 예외 메시지를 타고 로그로 새지 않는다(명세: 대화 원문은 남기지 않는다).
    async def _boom(req):
        raise RuntimeError("장애")

    monkeypatch.setattr(extractions.service, "extract", _boom)

    with caplog.at_level("DEBUG"):
        client.post(URL, json=_request())

    assert "비 오는 날" not in caplog.text
    assert "잔잔한 에세이" not in caplog.text


# --- LLM 장애와 시간 초과 (#371) ---


def _extract_raises(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    async def _raise(req):
        raise exc

    monkeypatch.setattr(extractions.service, "extract", _raise)


def test_LLM_장애면_503이다(monkeypatch: pytest.MonkeyPatch) -> None:
    _extract_raises(monkeypatch, LLMUnavailableError("연결 실패"))

    res = client.post(URL, json=_request())

    assert res.status_code == 503
    assert res.json() == {"message": "upstream_unavailable", "data": None}


def test_LLM이_제한_시간_안에_답하지_않으면_504다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _extract_raises(monkeypatch, LLMTimeoutError("30초 초과"))

    res = client.post(URL, json=_request())

    assert res.status_code == 504
    assert res.json() == {"message": "generation_timeout", "data": None}


def test_LLM_장애가_아닌_오류는_여전히_500이다(monkeypatch: pytest.MonkeyPatch) -> None:
    # 코드 버그까지 503으로 답하면 BE가 "다시 부르면 된다"고 읽어 버그가 가려진다
    _extract_raises(monkeypatch, RuntimeError("버그"))

    res = client.post(URL, json=_request())

    assert res.status_code == 500


@pytest.mark.parametrize("exc_type", [LLMUnavailableError, LLMTimeoutError])
def test_LLM_장애의_예외_메시지는_로그에_남기지_않는다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, exc_type
) -> None:
    # 공급자의 오류 메시지나 LLM의 답이 예외 메시지에 실려 올 수 있다. 종류만 남긴다
    _extract_raises(monkeypatch, exc_type("비 오는 날 읽을 책이라는 말이 든 메시지"))

    with caplog.at_level("DEBUG"):
        client.post(URL, json=_request())

    assert "비 오는 날" not in caplog.text
    assert exc_type.__name__ in caplog.text


# --- 실제로 뽑기 (#373). LLM 과 임베딩은 위의 가짜다 ---


def test_LLM이_뽑은_취향이_명세의_모양으로_나간다(fake_llm) -> None:
    fake_llm["reply"] = json.dumps(
        {
            "extractions": [
                {
                    "type": "mood",
                    "value": "이별 후 위로되는 소설을 찾음",
                    "confidence": 0.7,
                    "evidence": "이별 후에 위로가 될 만한 걸로",
                    "owner": "user",
                    "basis": "requested",
                }
            ]
        },
        ensure_ascii=False,
    )

    res = client.post(URL, json=_request())

    assert res.status_code == 200
    assert res.json() == {
        "message": "extract_success",
        "data": {
            "extractions": [
                {
                    "type": "mood",
                    "value": "이별 후 위로되는 소설을 찾음",
                    "confidence": 0.7,
                    "vector": [0.1, 0.2],
                    "dim": 2,
                    "source_conversation_id": "cv_20260903_a1",
                }
            ],
            "nothing_found": False,
        },
    }


def test_LLM이_뽑을_것이_없다고_하면_nothing_found다(fake_llm) -> None:
    res = client.post(URL, json=_request())

    assert res.json()["data"] == {"extractions": [], "nothing_found": True}
    assert fake_llm["calls"] == 1


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"consented": False}, id="동의하지_않음"),
        pytest.param({"conversation": []}, id="빈_대화"),
    ],
)
def test_뽑지_않는_요청은_LLM을_부르지_않는다(fake_llm, overrides) -> None:
    # 동의하지 않은 사용자의 대화가 외부 LLM 으로 나가면 안 된다. 빈 대화에 돈을 쓸 이유도 없다
    res = client.post(URL, json=_request(**overrides))

    assert res.json()["data"] == {"extractions": [], "nothing_found": True}
    assert fake_llm["calls"] == 0


def test_형식이_틀린_요청은_LLM을_부르지_않는다(fake_llm) -> None:
    res = client.post(URL, json=_request(user_id="123"))

    assert res.status_code == 400
    assert fake_llm["calls"] == 0


def test_LLM의_답이_약속한_모양이_아니면_503이다(fake_llm) -> None:
    fake_llm["reply"] = '{"mock": true}'

    res = client.post(URL, json=_request())

    assert res.status_code == 503
    assert res.json() == {"message": "upstream_unavailable", "data": None}


def test_임베딩이_실패하면_503이다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fake_llm
) -> None:
    fake_llm["reply"] = json.dumps(
        {
            "extractions": [
                {
                    "type": "mood",
                    "value": "위로되는 소설을 찾음",
                    "confidence": 0.7,
                    "evidence": "이별 후에 위로가 될 만한 걸로",
                    "owner": "user",
                    "basis": "requested",
                }
            ]
        },
        ensure_ascii=False,
    )

    async def _fail(texts, purpose):
        raise RuntimeError("위로되는 소설을 찾음 을 벡터로 못 바꿈")

    monkeypatch.setattr(extractions.service.embedding, "embed", _fail)

    with caplog.at_level("DEBUG"):
        res = client.post(URL, json=_request())

    assert res.status_code == 503
    assert res.json() == {"message": "upstream_unavailable", "data": None}
    assert "위로되는 소설" not in caplog.text
