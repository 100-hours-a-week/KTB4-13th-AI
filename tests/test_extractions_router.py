"""⑤ POST /preferences/extractions 라우터 테스트 — 요청 검사와 응답 모양(#339). DB·LLM 없이 돈다."""

import json

import pytest
from fastapi.testclient import TestClient

from app.extraction.schemas import (
    MAX_CONVERSATION_TURNS,
    MAX_TURN_CHARS,
    parse_request,
)
from app.main import app
from app.routers import extractions

client = TestClient(app)

URL = "/preferences/extractions"


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
