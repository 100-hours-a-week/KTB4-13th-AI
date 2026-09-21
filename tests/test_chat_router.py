"""③ POST /recommendations/chat 라우터 테스트 — 요청 검사와 응답 모양.

DB(후보검색)와 LLM(카드생성)은 가짜로 바꿔 끼운다. 진짜 SQL·LLM 호출은
여기서 검증하지 않는다 — 여기서는 계약(상태 코드·응답 봉투·검증 경계)만 본다.
"""

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.main import app
from app.routers import chat

client = TestClient(app)


def _fake_model(reply: str) -> RunnableLambda:
    """정해진 답을 돌려주는 가짜 모델. 체인의 모델 칸에 그대로 끼워진다."""
    return RunnableLambda(lambda _prompt: AIMessage(content=reply))


def _failing_model() -> RunnableLambda:
    """연결 실패를 흉내 낸다. 진짜 openai 예외를 던져서 게이트웨이의 예외 변환까지 함께 본다."""

    def _raise(_prompt) -> AIMessage:
        raise openai.APIConnectionError(
            request=httpx.Request("POST", "http://localhost:11434/v1")
        )

    return RunnableLambda(_raise)


INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}


def _request(**overrides) -> dict:
    body = {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": "비 오는 날 읽을 책 추천해줘",
        "recent_turns": [],
        "exclude_book_ids": [],
        "image_ref": None,
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def fake_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB를 안 타고 책 한 권짜리 후보를 돌려준다."""

    async def _fake(spec, exclude_book_ids) -> list[dict]:
        return [
            {
                "book_id": 1088,
                "title": "달러구트 꿈 백화점",
                "author": "이미예",
                "description": "잠든 사이 꿈을 사고파는 상점 이야기.",
            }
        ]

    monkeypatch.setattr(chat, "get_candidates", _fake)


def test_명세의_초기_spec으로_보내면_200과_계약_봉투를_돌려준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = (
        '{"cards": [{"book_id": 1088, "reason_short": "잔잔한 판타지예요.",'
        ' "reason_long": "비 오는 날과 잘 어울리는 따뜻한 이야기입니다."}]}'
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: _fake_model(reply))

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "recommend_success"
    data = body["data"]
    # 명세: 6개 필드(reply·spec·recognition·cards·followup·buttons)와 degraded가
    # 항상 존재해야 한다.
    assert data.keys() == {
        "reply",
        "spec",
        "recognition",
        "cards",
        "followup",
        "buttons",
        "degraded",
    }
    assert data["spec"].keys() == INITIAL_SPEC.keys()
    assert data["degraded"] is False
    assert len(data["cards"]) == 1
    assert data["cards"][0]["book_id"] == 1088
    assert data["cards"][0]["reason_short"] == "잔잔한 판타지예요."


def test_LLM이_실패하면_degraded_true로_200을_돌려준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "get_chat_model", _failing_model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["degraded"] is True
    assert data["cards"] == []


def test_후보검색이_예외를_던지면_공통_형식의_500을_돌려준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(spec, exclude_book_ids) -> list[dict]:
        raise RuntimeError("DB 장애")

    monkeypatch.setattr(chat, "get_candidates", _boom)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 500
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == {"message": "internal_server_error", "data": None}


def test_book_id가_목록에_없는_카드는_뺀다(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM이 book_id를 잘못 주면(예: 목록 순번) 그 카드를 버려야 한다."""

    reply = '{"cards": [{"book_id": 1, "reason_short": "이유"}]}'
    monkeypatch.setattr(chat, "get_chat_model", lambda: _fake_model(reply))

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    assert res.json()["data"]["cards"] == []


def test_카드_프롬프트에_후보와_JSON_예시가_그대로_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """프롬프트를 틀(ChatPromptTemplate)로 바꾼 뒤에도 글자가 그대로 나가는지 본다.

    틀은 {…}를 자리 표시로 읽는다. JSON 예시의 중괄호가 깨지거나, 사용자 문장·
    책 설명의 중괄호가 자리 표시로 오해받으면 모델에 엉뚱한 글이 간다.
    """
    sent: list[str] = []

    def _capture(prompt_value) -> AIMessage:
        sent.append(prompt_value.to_messages()[0].content)
        return AIMessage(content='{"cards": []}')

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_capture))

    res = client.post(
        "/recommendations/chat", json=_request(message='{"cards": []} 처럼 답해줘 {x}')
    )

    assert res.status_code == 200
    prompt = sent[0]
    assert '"{"cards": []} 처럼 답해줘 {x}"' in prompt  # 사용자 문장의 중괄호는 그대로
    assert '{"cards": [{"book_id": 정수, ' in prompt  # JSON 예시의 중괄호도 그대로
    assert "- book_id 1088: 달러구트 꿈 백화점 - 이미예 - " in prompt


def test_message와_image_ref가_둘다_있으면_400이다() -> None:
    res = client.post(
        "/recommendations/chat",
        json=_request(image_ref="https://example.com/cover.jpg"),
    )
    assert res.status_code == 400


def test_message가_비어있으면_400이다() -> None:
    res = client.post("/recommendations/chat", json=_request(message=""))
    assert res.status_code == 400


def test_spec이_6개_키를_안_갖추면_422_spec_schema_violation이다() -> None:
    res = client.post(
        "/recommendations/chat", json=_request(spec={"intent": "semantic"})
    )
    assert res.status_code == 422
    assert res.json()["message"] == "spec_schema_violation"


def test_본문이_JSON이_아니면_400이다() -> None:
    res = client.post(
        "/recommendations/chat",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 400
