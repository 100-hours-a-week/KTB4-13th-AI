"""챗봇 입구의 문지기 시험(#413): 쇼핑 칸이 있으면 ③과 ⑦ 중 어디로 보낼지 정하고, ⑦이 거절하면 ③으로 되돌린다.

③과 ⑦ 모두 가짜 LLM 과 가짜 BE 로 끝까지 돈다. "어느 쪽이 불리지 않았는가"는 불리면 실패하는 가짜로 확인한다.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.agent.fake_llm import ScriptedModel, ai_calls, ai_text
from app.agent.prompts import NOT_SHOPPING
from app.agent.schemas import ToolName
from app.gateway import commerce_provider
from app.gateway.commerce_fake import FakeBook, FakeCommerce
from app.gateway.llm import LLMUnavailableError
from app.main import app
from app.routers import agent, chat

client = TestClient(app)

URL = "/recommendations/chat"
UID = 123
A, B, C = "9788900000011", "9788900000028", "9788900000035"

INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}
CARDS = [
    {"isbn": A, "rank": 1, "match_score": 90},
    {"isbn": B, "rank": 2, "match_score": 80},
    {"isbn": C, "rank": 3, "match_score": 70},
]
SHOP_FIELDS = {
    "idempotency_key": "idem_gate",
    "user_context": {"logged_in": True},
    "context_cards": CARDS,
}
RECOMMEND_MESSAGE = "비 오는 날 읽을 책 추천해줘"
CARD_REPLY = (
    '{"reply": "골라봤어요.", "cards": [{"book_id": 1088, '
    '"reason_short": "잔잔해요.", "reason_long": "비 오는 날과 어울려요."}]}'
)
CHAT_KEYS = {"reply", "spec", "recognition", "cards", "followup", "buttons", "degraded"}
SHOP_KEYS = {
    "reply",
    "not_shopping",
    "tool_calls",
    "resolved_reference",
    "selection",
    "buttons",
}


@pytest.fixture(autouse=True)
def _fresh_dev_commerce():
    commerce_provider.reset_dev_commerce()
    yield
    commerce_provider.reset_dev_commerce()


@pytest.fixture(autouse=True)
def _candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(spec, exclude_book_ids, user_id, owned_book_ids=None) -> list[dict]:
        return [
            {
                "book_id": 1088,
                "title": "달러구트 꿈 백화점",
                "author": "이미예",
                "description": "잠든 사이 꿈을 사고파는 상점 이야기.",
                "match_score": 25,
                "popularity": 3.0,
            }
        ]

    monkeypatch.setattr(chat, "get_candidates", _fake)


def _body(message: str, **overrides) -> dict:
    body = {
        "user_id": UID,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": message,
        "recent_turns": [],
        "exclude_book_ids": [],
        "image_ref": None,
        **SHOP_FIELDS,
    }
    body.update(overrides)
    return body


def _without_shop_fields(body: dict) -> dict:
    return {k: v for k, v in body.items() if k not in SHOP_FIELDS}


def _wire_recommend(monkeypatch) -> None:
    """③ 이 쓰는 LLM(조건 갱신 → 카드 생성)을 가짜로 바꾼다."""
    replies = iter(["{}", CARD_REPLY])
    model = RunnableLambda(lambda _prompt: AIMessage(content=next(replies)))
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)


def _wire_shop(monkeypatch, replies):
    """⑦ 이 쓰는 LLM 과 장바구니를 가짜로 바꾼다."""
    fake = FakeCommerce(
        [
            FakeBook(A, "책1", 15000, 50),
            FakeBook(B, "책2", 22000, 50),
            FakeBook(C, "책3", 28000, 50),
        ],
        users=[UID],
    )
    model = ScriptedModel(replies)
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    monkeypatch.setattr(agent, "get_commerce", lambda user_id: fake)
    monkeypatch.setattr(agent, "get_catalog", lambda: fake)
    return model, fake


def _forbid(monkeypatch, module, name: str) -> None:
    """불리면 실패하는 가짜로 바꾼다."""

    async def _boom(*_a, **_k):
        raise AssertionError(f"{name} 이 불리면 안 된다")

    monkeypatch.setattr(module, name, _boom)


def _cart(fake) -> list[tuple[str, int]]:
    lines = asyncio.run(fake.get_cart(UID))
    return [(line.isbn, line.quantity) for line in lines]


# --- 쇼핑 칸이 없는 요청: 이 변경 전과 같다 ---


def test_쇼핑_칸이_없으면_장바구니_말이어도_추천만_하고_route도_없다(
    monkeypatch,
) -> None:
    _forbid(monkeypatch, agent, "shop_turn")
    _wire_recommend(monkeypatch)

    res = client.post(URL, json=_without_shop_fields(_body("1번 담아줘")))

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "recommend_success"
    assert set(body["data"]) == CHAT_KEYS  # route 가 없다


# --- 쇼핑 칸이 있는 요청: 문지기가 가른다 ---


def test_추천_말이면_쇼핑을_부르지_않고_추천으로_처리하며_route는_recommend다(
    monkeypatch,
) -> None:
    _forbid(monkeypatch, agent, "shop_turn")
    _wire_recommend(monkeypatch)

    res = client.post(URL, json=_body(RECOMMEND_MESSAGE))

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "recommend_success"
    assert set(body["data"]) == CHAT_KEYS | {"route"}
    assert body["data"]["route"] == "recommend"
    assert body["data"]["cards"]


def test_직전_쇼핑_답이_200자를_넘어도_다음_턴은_400이_아니다(monkeypatch) -> None:
    # 쇼핑 답에는 길이 상한이 없다. ③ 검사가 먼저라 400 이면 갈래와 상관없이 다음 턴 전체가 실패한다(#490)
    _forbid(monkeypatch, agent, "shop_turn")
    _wire_recommend(monkeypatch)
    long_reply = "《책1》과 《책2》를 담았어요. " + "합계는 37,000원이에요. " * 20

    res = client.post(
        URL,
        json=_body(
            RECOMMEND_MESSAGE,
            recent_turns=[
                {"role": "user", "text": "1번이랑 2번 담아줘"},
                {"role": "assistant", "text": long_reply},
            ],
        ),
    )

    assert len(long_reply) > 200
    assert res.status_code == 200
    assert res.json()["data"]["route"] == "recommend"


def test_장바구니_말이면_추천을_부르지_않고_쇼핑으로_처리하며_route는_shop이다(
    monkeypatch,
) -> None:
    _forbid(monkeypatch, chat, "update_spec")
    _model, fake = _wire_shop(
        monkeypatch,
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": 1}})),
            ai_text("담았어요."),
        ],
    )

    res = client.post(URL, json=_body("1번 담아줘"))

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "agent_success"
    assert set(body["data"]) == SHOP_KEYS | {"route"}
    assert body["data"]["route"] == "shop"
    assert _cart(fake) == [(A, 1)]


def test_쇼핑_에이전트가_쇼핑이_아니라고_하면_추천으로_되돌리고_route는_recommend다(
    monkeypatch,
) -> None:
    model, fake = _wire_shop(monkeypatch, [ai_text(NOT_SHOPPING)])
    _wire_recommend(monkeypatch)

    res = client.post(URL, json=_body("안녕"))

    assert res.status_code == 200
    body = res.json()
    assert len(model.calls) == 1  # ⑦ 을 거쳤다
    assert body["message"] == "recommend_success"
    assert body["data"]["route"] == "recommend"
    assert body["data"]["cards"]
    assert _cart(fake) == []


def test_쇼핑_에이전트가_실패하면_추천으로_덮지_않고_그_오류를_돌려준다(
    monkeypatch,
) -> None:
    _forbid(monkeypatch, chat, "update_spec")

    def _down():
        raise LLMUnavailableError("연결 실패")

    monkeypatch.setattr(agent, "agent_model", _down)
    monkeypatch.setattr(agent, "get_commerce", lambda user_id: object())
    monkeypatch.setattr(agent, "get_catalog", lambda: object())

    res = client.post(URL, json=_body("장바구니 보여줘"))

    assert res.status_code == 503
    assert res.json() == {"message": "upstream_unavailable", "data": None}
    assert res.headers["Retry-After"] == "5"


# --- 쇼핑 칸 검사는 갈래가 shop 일 때만 ---


def test_갈래가_쇼핑인데_쇼핑_칸이_틀리면_400이다(monkeypatch) -> None:
    _forbid(monkeypatch, agent, "shop_turn")
    _forbid(monkeypatch, chat, "update_spec")
    body = _body("1번 담아줘")
    del body["user_context"]

    res = client.post(URL, json=body)

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_갈래가_추천이면_쇼핑_칸이_틀려도_추천은_처리한다(monkeypatch) -> None:
    _forbid(monkeypatch, agent, "shop_turn")
    _wire_recommend(monkeypatch)

    res = client.post(
        URL, json=_body(RECOMMEND_MESSAGE, context_cards="엉망", user_context=None)
    )

    assert res.status_code == 200
    assert res.json()["data"]["route"] == "recommend"


def test_추천_요청_검사는_문지기보다_먼저다(monkeypatch) -> None:
    _forbid(monkeypatch, agent, "shop_turn")

    res = client.post(URL, json=_body("1번 담아줘", spec={"intent": "semantic"}))

    assert res.status_code == 422
    assert res.json() == {"message": "spec_schema_violation", "data": None}


# --- 같은 키의 재시도 ---


def test_같은_키로_다시_보내면_저장한_응답을_route와_함께_돌려준다(
    monkeypatch, idempotency_rows
) -> None:
    model, fake = _wire_shop(
        monkeypatch,
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": 1}})),
            ai_text("담았어요."),
        ],
    )
    body = _body("1번 담아줘")

    first = client.post(URL, json=body)
    llm_calls = len(model.calls)
    second = client.post(URL, json=body)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["data"]["route"] == "shop"
    assert len(model.calls) == llm_calls  # 다시 처리하지 않았다
    assert _cart(fake) == [(A, 1)]
    # 저장한 응답에는 route 가 없다. /agent/act 의 재시도도 같은 응답을 받는다
    (stored,) = idempotency_rows.values()
    assert "route" not in stored[1]["data"]


# --- /agent/act 는 그대로 ---


def test_agent_act_직접_호출의_응답에는_route가_없다(monkeypatch) -> None:
    _wire_shop(monkeypatch, [ai_text(NOT_SHOPPING)])
    payload = json.loads(json.dumps(_without_shop_fields(_body("안녕"))))
    act_body = {
        "user_id": UID,
        "message": "안녕",
        "user_context": {"logged_in": True},
        "idempotency_key": "idem_act",
    }
    assert payload["message"] == "안녕"

    res = client.post("/agent/act", json=act_body)

    assert res.status_code == 200
    assert set(res.json()["data"]) == SHOP_KEYS
