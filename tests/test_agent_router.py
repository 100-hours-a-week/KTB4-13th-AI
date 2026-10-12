"""⑦ POST /agent/act 라우터 테스트 — 요청 검사와 응답 모양(#350). DB·LLM 없이 돈다."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import agent

client = TestClient(app)

URL = "/agent/act"
EXAMPLES = json.loads(
    (Path(__file__).parent / "data" / "agent_act_examples.json").read_text(
        encoding="utf-8"
    )
)
INVALID = {"message": "invalid_request", "data": None}


def _request(**overrides) -> dict:
    body = json.loads(json.dumps(EXAMPLES["request"]))
    body.update(overrides)
    return body


def test_명세의_예시_요청이면_200과_응답_모양을_돌려준다() -> None:
    res = client.post(URL, json=EXAMPLES["request"])

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "agent_success"
    assert set(body["data"]) == {
        "reply",
        "not_shopping",
        "tool_calls",
        "resolved_reference",
        "selection",
        "buttons",
    }


def test_처리_로직이_들어오기_전에는_쇼핑_요청이_아님으로_답한다() -> None:
    # 호출자(BE)가 이 신호를 일반 챗봇 흐름으로 처리하므로, 먼저 연결해도 빈 화면이 나가지 않는다
    res = client.post(URL, json=_request())

    assert res.json()["data"] == EXAMPLES["responses"]["not_shopping"]["data"]


def test_로그인하지_않은_사용자의_요청도_이_단계에서는_같은_응답이다() -> None:
    # 쓰기를 막는 것은 tool 을 고르고 검사하는 단계(#344 4단계)의 일이다
    res = client.post(URL, json=_request(user_context={"logged_in": False}))

    assert res.status_code == 200
    assert res.json()["data"]["not_shopping"] is True


def test_옛_명세의_칸을_보내도_400이_아니다() -> None:
    body = _request(book_id=1088, focused_book_id=1088)
    body["user_context"]["has_default_address"] = True

    assert client.post(URL, json=body).status_code == 200


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"message": ""}, id="빈_메시지"),
        pytest.param({"message": "가" * 201}, id="메시지_201자"),
        pytest.param({"user_id": "123"}, id="user_id_문자열"),
        pytest.param({"user_context": {"logged_in": "true"}}, id="logged_in_문자열"),
        pytest.param({"idempotency_key": ""}, id="빈_멱등_키"),
        pytest.param({"focused_isbn": "978-8-9000-0001-1"}, id="isbn_하이픈"),
        pytest.param({"allow_tools": ["order.preview"]}, id="없앤_tool"),
        pytest.param(
            {
                "context_cards": [
                    {"isbn": "9788900000011", "rank": 1, "match_score": 80}
                ]
                * 11
            },
            id="카드_11개",
        ),
        pytest.param({"message": "앞\x00뒤"}, id="NUL_문자"),
    ],
)
def test_계약에_안_맞는_요청은_400이다(overrides) -> None:
    res = client.post(URL, json=_request(**overrides))

    assert res.status_code == 400
    assert res.json() == INVALID


@pytest.mark.parametrize(
    "key", ["user_id", "message", "user_context", "idempotency_key"]
)
def test_필수_칸이_없으면_400이다(key) -> None:
    body = {k: v for k, v in _request().items() if k != key}

    res = client.post(URL, json=body)

    assert res.status_code == 400
    assert res.json() == INVALID


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b'{"message": "broken', id="깨진_JSON"),
        pytest.param(b"", id="빈_본문"),
        pytest.param(b"[]", id="목록_본문"),
        pytest.param(b"null", id="null_본문"),
        pytest.param(b'{"message": "\xff\xfe"}', id="UTF-8이_아님"),
    ],
)
def test_본문이_JSON_객체가_아니면_400이다(raw) -> None:
    res = client.post(URL, content=raw, headers={"content-type": "application/json"})

    assert res.status_code == 400
    assert res.json() == INVALID


def test_본문이_상한을_넘으면_413이다() -> None:
    # 검사에 걸리기 전에 크기부터 본다. 상한을 넘는 본문은 읽다가 멈춘다
    huge = _request(conversation_id="c" * (agent.MAX_BODY_BYTES + 1))

    res = client.post(URL, json=huge)

    assert res.status_code == 413
    assert res.json() == {"message": "payload_too_large", "data": None}


def test_상한_안의_본문은_413이_아니다() -> None:
    body = json.dumps(_request()).encode()
    assert len(body) < agent.MAX_BODY_BYTES

    res = client.post(URL, content=body, headers={"content-type": "application/json"})

    assert res.status_code == 200


def test_오류_응답에_사용자_메시지가_실리지_않는다() -> None:
    # 400 응답과 로그에 대화 원문을 남기지 않는다
    secret = "비밀스러운_사용자_발화"

    res = client.post(URL, json=_request(message=secret, idempotency_key=""))

    assert res.status_code == 400
    assert secret not in res.text
