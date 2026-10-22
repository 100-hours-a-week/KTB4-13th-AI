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
from app.chat import asking, shown
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
    "actions",
    "reply",
    "not_shopping",
    "tool_calls",
    "resolved_reference",
    "selection",
    "buttons",
}


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


def test_묻기에_답하면_쇼핑을_거치지_않고_추천으로_간다(monkeypatch) -> None:
    # ③이 넓은 첫 요청에 원하는 것을 물은 뒤의 답("요즘 너무 지쳐서")에는 추천 낱말이 없다.
    # v1 은 ⑦에 들렀다가 되돌아왔다(#530)
    _forbid(monkeypatch, agent, "shop_turn")
    _wire_recommend(monkeypatch)
    turns = [
        {"role": "user", "text": "위로되는 책"},
        {
            "role": "assistant",
            "text": "위로가 필요하시군요.\n" + asking.followup("어떤 일로 지치셨어요?"),
        },
    ]

    res = client.post(
        URL, json=_body("요즘 너무 지쳐서", recent_turns=turns, context_cards=[])
    )

    assert res.status_code == 200
    assert res.json()["data"]["route"] == "recommend"


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


def test_챗봇_입구로_온_장바구니와_가격으로_돌아_바꿀_것을_돌려준다(
    monkeypatch,
) -> None:
    # A안(#489): BE 는 챗봇 입구에 장바구니와 가격을 실어 보내고, 응답의 actions 를 반영한다
    _forbid(monkeypatch, chat, "update_spec")
    model = ScriptedModel(
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": 1}})),
            ai_text("담았어요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    offers = [{"isbn": A, "title": "책1", "price": 15000, "in_stock": True}]

    res = client.post(URL, json=_body("1번 담아줘", cart=[], offers=offers))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["route"] == "shop"
    assert data["actions"] == [{"op": "set", "isbn": A, "quantity": 1}]


def test_BE가_가격을_소수점_붙여_보내도_쇼핑_턴이_400이_아니다(monkeypatch) -> None:
    # BE 는 가격을 DECIMAL(19,2) 그대로 보낸다("price": 15000.00, BE 스택 6f98aae). 본문 글자 그대로 보낸다
    model = ScriptedModel(
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": 1}})),
            ai_text("담았어요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    line = {"isbn": C, "title": "책3", "price": 28000, "quantity": 1, "available": True}
    offers = [{"isbn": A, "title": "책1", "price": 15000, "in_stock": True}]
    raw = json.dumps(_body("1번 담아줘", cart=[line], offers=offers))
    raw = raw.replace('"price": 28000', '"price": 28000.00')
    raw = raw.replace('"price": 15000', '"price": 15000.00')
    assert '"price": 28000.00' in raw and '"price": 15000.00' in raw

    res = client.post(URL, content=raw, headers={"Content-Type": "application/json"})

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["route"] == "shop"
    assert data["actions"] == [{"op": "set", "isbn": A, "quantity": 1}]


def test_쇼핑_에이전트가_쇼핑이_아니라고_하면_추천으로_되돌리고_route는_recommend다(
    monkeypatch,
) -> None:
    # "안녕"은 규칙이 애매하다고 본다. 판정이 쇼핑으로 보낸 경우에도 ⑦ 의 거절 안전망이 그대로 도는지 본다
    monkeypatch.setattr(chat, "_judge_lane", _judged("shop"))
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


# --- 애매한 말의 판정(#530) ---


def _judged(lane):
    """판정 호출을 정해진 갈래를 돌려주는 가짜로 바꾼다. None 이면 판정 실패다."""

    async def _fake(req, cards, books):
        return lane

    return _fake


def test_확실한_말은_판정을_부르지_않는다(monkeypatch) -> None:
    _forbid(monkeypatch, chat, "_judge_lane")
    _forbid(monkeypatch, agent, "shop_turn")
    _wire_recommend(monkeypatch)

    res = client.post(URL, json=_body(RECOMMEND_MESSAGE))

    assert res.json()["data"]["route"] == "recommend"


def test_애매한_말을_판정이_추천이라고_하면_쇼핑을_거치지_않는다(monkeypatch) -> None:
    # 카드가 보일 때 "다른 거 보여줘"를 ⑦이 지금 카드 되읊기로 받았다(머지 전 점검)
    _forbid(monkeypatch, agent, "shop_turn")
    monkeypatch.setattr(chat, "_judge_lane", _judged("recommend"))
    _wire_recommend(monkeypatch)

    res = client.post(URL, json=_body("다른 거 보여줘"))

    data = res.json()["data"]
    assert data["route"] == "recommend"
    assert data["cards"]


def test_판정이_카드에_대한_말이라고_하면_검색하지_않고_그_책_소개로_답한다(
    monkeypatch,
) -> None:
    # 새로 찾으면 사용자가 가리킨 책이 화면에서 사라진다
    for name in ("update_spec", "get_candidates"):
        _forbid(monkeypatch, chat, name)
    _forbid(monkeypatch, agent, "shop_turn")
    monkeypatch.setattr(chat, "_judge_lane", _judged("card"))
    answer = (
        '{"reply": "2번은 꿈을 파는 상점 이야기예요.", "followup": "이 책으로 할까요?"}'
    )
    model = RunnableLambda(lambda _prompt: AIMessage(content=answer))
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post(URL, json=_body("2번은 어떤 내용이야?"))

    data = res.json()["data"]
    assert data["route"] == "recommend"
    assert data["cards"] == []
    assert data["reply"] == "2번은 꿈을 파는 상점 이야기예요."
    assert data["followup"] == "이 책으로 할까요?"


def test_카드가_보일_때_책을_칭찬하면_검색하지_않고_답해_카드를_지킨다(
    monkeypatch,
) -> None:
    # 칭찬 말("1번 책이 흥미로워 보여요")을 ③이 새로 찾으면 칭찬한 책이 화면에서 사라졌다(#530 남은 문제 1).
    # 번호로 가리킨 평가 말은 낱말만으로 확실해서 판정 호출도 부르지 않는다
    for name in ("update_spec", "get_candidates", "_judge_lane"):
        _forbid(monkeypatch, chat, name)
    _forbid(monkeypatch, agent, "shop_turn")
    answer = (
        '{"reply": "1번은 꿈을 파는 상점 이야기예요.", "followup": "담아 드릴까요?"}'
    )
    model = RunnableLambda(lambda _prompt: AIMessage(content=answer))
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    # 카드가 보이는 요청에는 그 카드를 낸 턴이 최근 대화에 실려 있다
    turns = [
        {"role": "user", "text": "비 오는 날 읽을 책 추천해줘"},
        {"role": "assistant", "text": "골라봤어요."},
    ]

    res = client.post(URL, json=_body("1번 책이 흥미로워 보여요", recent_turns=turns))

    data = res.json()["data"]
    assert data["route"] == "recommend"
    assert data["cards"] == []
    assert data["reply"] == "1번은 꿈을 파는 상점 이야기예요."
    assert data["followup"] == "담아 드릴까요?"


@pytest.mark.parametrize(
    "message",
    [
        "1번 담으면 될 것 같아",
        "2번 사면 될 것 같아",
        "1번 구매할게요 마음에 들어서",
        "2번으로 주세요 좋아 보여요",
    ],
)
def test_카드가_보일_때_사겠다는_말은_칭찬이_섞여도_쇼핑으로_간다(
    monkeypatch, message: str
) -> None:
    # ③으로 가면 카드가 갈아 끼워져 담을 수 없다. main 은 이 말들을 ⑦로 보냈다(#530 리뷰)
    for name in ("update_spec", "_judge_lane"):
        _forbid(monkeypatch, chat, name)
    model, _fake = _wire_shop(
        monkeypatch,
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": 1}})),
            ai_text("처리했어요."),
        ],
    )

    res = client.post(URL, json=_body(message))

    assert res.status_code == 200
    assert res.json()["data"]["route"] == "shop"
    assert len(model.calls) >= 1  # ⑦ 이 받았다


def test_판정이_실패하면_규칙의_기본값대로_쇼핑에_보낸다(monkeypatch) -> None:
    monkeypatch.setattr(chat, "_judge_lane", _judged(None))
    model, _fake = _wire_shop(monkeypatch, [ai_text(NOT_SHOPPING)])
    _wire_recommend(monkeypatch)

    res = client.post(URL, json=_body("다른 거"))

    assert len(model.calls) == 1  # ⑦ 을 거쳤다
    assert res.json()["data"]["route"] == "recommend"


def _req(message: str, **overrides):
    req = chat.parse_request(_body(message, **overrides))
    assert not isinstance(req, tuple)
    return req


def test_판정_호출에는_직전_챗봇_말과_화면_카드와_이번_말만_싣는다(monkeypatch) -> None:
    prompts: list[str] = []

    def _call(prompt_value) -> AIMessage:
        prompts.append(prompt_value.to_messages()[0].content)
        return AIMessage(content='{"lane": "card"}')

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_call))
    req = _req(
        "2번은 어떤 내용이야?",
        recent_turns=[
            {"role": "user", "text": "아주 오래된 첫 말"},
            {"role": "assistant", "text": "앞 답"},
            {"role": "user", "text": "꿈 이야기 책"},
            {"role": "assistant", "text": "꿈을 다룬 세 권이에요."},
        ],
    )
    books = [shown.ShownBook(1, 1088, "달러구트 꿈 백화점", "이미예", "소개")]

    lane = asyncio.run(chat._judge_lane(req, [(1, A), (2, B)], books))

    assert lane == "card"
    (text,) = prompts
    assert '직전 챗봇 말: "꿈을 다룬 세 권이에요."' in text
    # 책 표에서 못 찾은 카드는 번호만 남긴다
    assert "화면에 보이는 책 카드: 1번 『달러구트 꿈 백화점』, 2번\n" in text
    assert '이번 말: "2번은 어떤 내용이야?"' in text
    assert "아주 오래된 첫 말" not in text


def test_직전_줄이_사용자_말이면_직전_챗봇_말은_없음이다(monkeypatch) -> None:
    prompts: list[str] = []

    def _call(prompt_value) -> AIMessage:
        prompts.append(prompt_value.to_messages()[0].content)
        return AIMessage(content='{"lane": "recommend"}')

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_call))
    req = _req("다른 거", recent_turns=[{"role": "user", "text": "소설"}])

    asyncio.run(chat._judge_lane(req, [], []))

    assert '직전 챗봇 말: "(없음)"' in prompts[0]
    assert "화면에 보이는 책 카드: (없음)" in prompts[0]


@pytest.mark.parametrize("content", ['{"lane": "account"}', "JSON 아님"])
def test_판정_답을_쓸_수_없으면_None이다(monkeypatch, content: str) -> None:
    model = RunnableLambda(lambda _prompt: AIMessage(content=content))
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    assert asyncio.run(chat._judge_lane(_req("다른 거"), [], [])) is None


def test_판정이_늦으면_None이다(monkeypatch) -> None:
    async def _slow(_chain, _inputs):
        await asyncio.sleep(1)

    monkeypatch.setattr(chat, "GATE_JUDGE_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(chat, "invoke_chain", _slow)

    assert asyncio.run(chat._judge_lane(_req("다른 거"), [], [])) is None
