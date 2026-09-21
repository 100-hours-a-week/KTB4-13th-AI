"""③ POST /recommendations/chat 라우터 테스트 — 요청 검사와 응답 모양.

DB(후보검색)와 LLM(spec 갱신·카드생성)은 가짜로 바꿔 끼운다. 진짜 SQL·LLM
호출은 여기서 검증하지 않는다 — 여기서는 계약(상태 코드·응답 봉투·검증 경계)만
본다.

한 요청 안에서 LLM이 두 번(1단계 spec 갱신 → 3단계 카드 생성) 불린다.
`get_chat_model`을 한 번만 patch하면 두 호출이 같은 가짜 모델을 타므로,
두 답을 순서대로 돌려주는 `_sequenced_model`을 쓴다.
"""

import json

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


def _sequenced_model(*replies: str) -> RunnableLambda:
    """호출될 때마다 다음 답을 순서대로 돌려주는 가짜 모델.

    `get_chat_model`을 patch하는 lambda가 매번 이 함수를 새로 부르면 안 된다
    (그러면 호출마다 처음부터 다시 시작한다) — 인스턴스 하나를 만들어서
    `monkeypatch.setattr(chat, "get_chat_model", lambda: model)`처럼 같은
    객체를 돌려줘야 두 호출이 이어서 소비된다.
    """
    it = iter(replies)
    return RunnableLambda(lambda _prompt: AIMessage(content=next(it)))


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


def _spec_reply(**overrides) -> str:
    """1단계(spec 갱신) 자리에 끼울, Spec 모양을 갖춘 가짜 LLM 응답."""
    spec = {**INITIAL_SPEC, **overrides}
    return json.dumps(spec, ensure_ascii=False)


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
    card_reply = (
        '{"cards": [{"book_id": 1088, "reason_short": "잔잔한 판타지예요.",'
        ' "reason_long": "비 오는 날과 잘 어울리는 따뜻한 이야기입니다."}]}'
    )
    model = _sequenced_model(_spec_reply(), card_reply)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

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
    # 1단계(spec 갱신)가 get_candidates보다 먼저 불린다. get_chat_model을 안
    # 끼우면 진짜 LLM(Ollama)을 호출하려 든다 — 가짜로 막아 둔다.
    monkeypatch.setattr(chat, "get_chat_model", lambda: _fake_model(_spec_reply()))

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 500
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == {"message": "internal_server_error", "data": None}


def test_book_id가_목록에_없는_카드는_뺀다(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM이 book_id를 잘못 주면(예: 목록 순번) 그 카드를 버려야 한다."""

    card_reply = '{"cards": [{"book_id": 1, "reason_short": "이유"}]}'
    model = _sequenced_model(_spec_reply(), card_reply)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    assert res.json()["data"]["cards"] == []


def test_카드_프롬프트에_후보와_JSON_예시가_그대로_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """프롬프트를 틀(ChatPromptTemplate)로 바꾼 뒤에도 글자가 그대로 나가는지 본다.

    틀은 {…}를 자리 표시로 읽는다. JSON 예시의 중괄호가 깨지거나, 사용자 문장·
    책 설명의 중괄호가 자리 표시로 오해받으면 모델에 엉뚱한 글이 간다.

    1단계(spec 갱신) 호출은 고정 답으로 통과시키고, 2번째 호출(카드 생성)만
    가로채 프롬프트 글자를 본다.
    """
    message = '{"cards": []} 처럼 답해줘 {x}'
    sent: list[str] = []
    call_count = 0

    def _dispatch(prompt_value) -> AIMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 1단계(spec 갱신) — semantic에 메시지를 그대로 싣고 통과시킨다.
            return AIMessage(content=_spec_reply(semantic=message))
        sent.append(prompt_value.to_messages()[0].content)  # 2단계 = 카드 생성.
        return AIMessage(content='{"cards": []}')

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))

    res = client.post("/recommendations/chat", json=_request(message=message))

    assert res.status_code == 200
    prompt = sent[0]
    assert '"{"cards": []} 처럼 답해줘 {x}"' in prompt  # 사용자 문장의 중괄호는 그대로
    assert '{"cards": [{"book_id": 정수, ' in prompt  # JSON 예시의 중괄호도 그대로
    assert "- book_id 1088: 달러구트 꿈 백화점 - 이미예 - " in prompt


def test_LLM이_바꾼_semantic이_응답_spec에_반영된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1단계 결과가 실제로 응답의 spec에 실리는지 본다(더 이상 메시지 그대로 복붙이 아님)."""

    spec_reply = _spec_reply(semantic="비 오는 날 읽을 잔잔한 책")
    model = _sequenced_model(spec_reply, '{"cards": []}')
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    assert res.json()["data"]["spec"]["semantic"] == "비 오는 날 읽을 잔잔한 책"


def test_spec_갱신_응답이_Spec_모양이_아니면_원래_spec_그대로_카드도_건너뛴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1단계가 Spec 6개 키를 못 갖춘 JSON을 주면 검증에서 걸린다.

    이때는 (LLM 연결이 끊긴 것과 마찬가지로) 원래 spec을 그대로 쓰고, 3단계
    (카드 생성)까지 건너뛴다 — 두 번째 LLM 호출이 실제로 일어나지 않는지도
    같이 본다(호출됐다면 아래 카드용 답을 먹고 cards가 채워졌을 것).
    """

    model = _sequenced_model('{"not_a_spec": true}', '{"cards": [{"book_id": 1088}]}')
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["degraded"] is True
    assert data["cards"] == []
    # 갱신 안 되고 요청에 보낸 spec 그대로. model_dump()는 filters의 생략된
    # 키도 채워 돌려주므로 요청 그대로의 {} 와는 모양이 다르다 — 파싱해서 비교.
    assert data["spec"]["filters"] == {
        "category": None,
        "price_min": None,
        "price_max": None,
        "pub_year_from": None,
        "pub_year_to": None,
        "in_stock_only": False,
    }
    assert {**data["spec"], "filters": {}} == INITIAL_SPEC


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
