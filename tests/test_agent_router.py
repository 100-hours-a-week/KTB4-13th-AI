"""⑦ POST /agent/act 라우터 테스트(#350, #358). 가짜 LLM(ScriptedModel)과 가짜 BE 로 HTTP 끝까지 돈다.

요청 검사(400, 413)는 LLM 과 BE 를 부르기 전에 끝나야 하고, 통과한 요청은 그래프와 답변 검사를 거쳐 응답이 된다.
오류 응답(422, 503, 504, 500)은 명세 ⑦의 오류 표대로 나가고, 로그와 오류 응답에 대화 원문이 남지 않는다.
"""

import asyncio
import json
import logging
from pathlib import Path

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.descriptors import llm_name
from app.agent.fake_llm import ScriptedModel, ai_calls, ai_text
from app.agent.prompts import NOT_SHOPPING
from app.agent.schemas import AgentActData, ToolName
from app.core.config import get_settings
from app.gateway.commerce_fake import FakeBook, FakeCommerce
from app.gateway.llm import LLMUnavailableError
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
UID = 123  # 명세 예시 요청의 user_id
A, B, C = "9788900000011", "9788900000028", "9788900000035"  # 예시 요청의 카드 1~3번


def _request(**overrides) -> dict:
    body = json.loads(json.dumps(EXAMPLES["request"]))
    # 명세 예시의 말(예산으로 조합 고르기)이 아니라 가짜 LLM 이 카드 1번을 담는 기본 흐름에 맞는 말을 쓴다.
    # 사용자의 말에 책을 가리키는 근거가 없으면 담기가 실행되지 않는다(evidence)
    body["message"] = "1번 담아줘"
    body.update(overrides)
    return body


def _fake(seed=()) -> FakeCommerce:
    books = [
        FakeBook(A, "책1", 15000, 50),
        FakeBook(B, "책2", 22000, 50),
        FakeBook(C, "책3", 28000, 50),
    ]
    fake = FakeCommerce(books, users=[UID])
    for isbn, qty in seed:
        asyncio.run(fake.add_item(UID, isbn, qty))
    return fake


def _wire(monkeypatch, replies, fake=None):
    """라우터가 쓰는 LLM 과 장바구니를 가짜로 바꾼다."""
    fake = fake or _fake()
    model = ScriptedModel(replies)
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    monkeypatch.setattr(agent, "get_commerce", lambda user_id: fake)
    monkeypatch.setattr(agent, "get_catalog", lambda: fake)
    return model, fake


def card(n: int) -> dict:
    return {"source": "card", "n": n}


# --- 끝까지 도는 흐름 ---


def test_명세의_예시_요청이면_200과_응답_모양을_돌려준다(monkeypatch) -> None:
    _wire(monkeypatch, [ai_text(NOT_SHOPPING)])

    res = client.post(URL, json=EXAMPLES["request"])

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "agent_success"
    assert set(body["data"]) == {
        "reply",
        "not_shopping",
        "actions",
        "tool_calls",
        "resolved_reference",
        "selection",
        "buttons",
    }


def test_응답_데이터는_명세의_모양에_들어맞는다(monkeypatch) -> None:
    _wire(
        monkeypatch,
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("담았어요.")],
    )

    data = client.post(URL, json=_request()).json()["data"]

    assert AgentActData.model_validate(data).model_dump(mode="json") == data


def test_쇼핑_요청이_아니면_아무것도_안_한_응답이다(monkeypatch) -> None:
    _wire(monkeypatch, [ai_text(NOT_SHOPPING)])

    res = client.post(URL, json=_request(message="안녕"))

    assert res.json()["data"] == EXAMPLES["responses"]["not_shopping"]["data"]


def test_담아_달라는_요청이_끝까지_돌아_장바구니가_바뀐다(monkeypatch) -> None:
    _model, fake = _wire(
        monkeypatch,
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1)})),
            ai_text("《책1》을 담았어요."),
        ],
    )

    res = client.post(URL, json=_request(message="1번 담아줘"))

    assert res.status_code == 200
    data = res.json()["data"]
    assert (
        data["reply"] == "1번 책을 장바구니에 담았어요."
    )  # LLM 의 문장이 아니라 코드가 만든 문장
    assert data["not_shopping"] is False
    assert [c["name"] for c in data["tool_calls"]] == ["cart.add"]
    assert data["tool_calls"][0]["arguments"] == {"isbn": A, "quantity": 1}
    assert data["tool_calls"][0]["result"] == {"ok": True}
    assert data["resolved_reference"] == {"ref": "카드 1번", "isbns": [A]}
    assert data["buttons"] == [
        {"label": "장바구니 보기", "action": "navigate", "target": "CART-001"}
    ]
    assert fake.cart_state(UID) == {A: 1}


# --- 골라 담기와 비교(#363) ---


def _pick_call(budget=40000, count=2):
    return ai_calls((ToolName.SELECTION_PICK, {"budget_total": budget, "count": count}))


def test_골라_담기는_장바구니를_그대로_두고_확인_버튼을_돌려준다(monkeypatch) -> None:
    # 카드 1번 15,000원 + 2번 22,000원 = 37,000원이 예산 40,000원 안에서 점수 합이 가장 크다
    _model, fake = _wire(
        monkeypatch,
        [_pick_call(), ai_text("1번과 2번을 골랐어요. 합계 37,000원이에요. 담을까요?")],
    )

    res = client.post(URL, json=_request(message="4만원 안에서 2권 골라 담아줘"))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["reply"] == "1번과 2번을 골랐어요. 합계 37,000원이에요. 담을까요?"
    assert data["selection"] == {
        "goal": "budget_pick",
        "constraints": {"budget_total": 40000, "count": 2},
        "chosen": [
            {"isbn": A, "price": 15000, "match_score": 92},
            {"isbn": B, "price": 22000, "match_score": 87},
        ],
        "total_price": 37000,
        "alternatives_considered": 3,
    }
    assert data["resolved_reference"] == {"ref": "40,000원 2권", "isbns": [A, B]}
    assert data["buttons"] == [
        {"label": "담기", "action": "confirm_selection", "isbns": [A, B]}
    ]
    assert fake.cart_state(UID) == {}
    assert AgentActData.model_validate(data).model_dump(mode="json") == data


def test_로그인하지_않았으면_골라도_확인_버튼이_없다(monkeypatch) -> None:
    _wire(monkeypatch, [_pick_call(), ai_text("로그인하시면 담을 수 있어요.")])
    body = _request(message="4만원 안에서 2권 골라 담아줘")
    body["user_context"] = {"logged_in": False}

    data = client.post(URL, json=body).json()["data"]

    assert data["selection"]["chosen"][0]["isbn"] == A
    assert data["buttons"] == []


def test_예산_안에_드는_조합이_없으면_알리기만_한다(monkeypatch) -> None:
    _model, fake = _wire(
        monkeypatch,
        [_pick_call(budget=10000), ai_text("만원 안에서는 두 권을 고를 수 없어요.")],
    )

    res = client.post(URL, json=_request(message="만원 안에서 2권 골라 담아줘"))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["selection"] is None
    assert data["buttons"] == []
    assert data["tool_calls"][0]["result"]["error"] == "NO_COMBINATION"
    assert fake.cart_state(UID) == {}


def test_골라_놓고_담았다고_거짓말하면_담기도_막히고_그_말도_지워진다(
    monkeypatch,
) -> None:
    # 담기는 코드가 막고(CONFIRMATION_REQUIRED), 담았다는 말은 답변 검사가 지운다. 이중으로 막힌다
    _model, fake = _wire(
        monkeypatch,
        [
            _pick_call(),
            ai_calls((ToolName.CART_ADD, {"book": card(1)})),
            ai_text("두 권을 장바구니에 담았어요."),
        ],
    )

    res = client.post(URL, json=_request(message="확인 없이 4만원 안에서 2권 담아줘"))

    assert res.status_code == 422
    assert fake.cart_state(UID) == {}


def test_골라_놓고_확인을_기다린다고_답하면_200이다(monkeypatch) -> None:
    _model, fake = _wire(
        monkeypatch,
        [
            _pick_call(),
            ai_calls((ToolName.CART_ADD, {"book": card(1)})),
            ai_text("확인 버튼을 눌러 주시면 담을게요."),
        ],
    )

    data = client.post(
        URL, json=_request(message="4만원 안에서 2권 바로 담아줘")
    ).json()["data"]

    assert data["reply"] == "확인 버튼을 눌러 주시면 담을게요."
    assert data["tool_calls"][1]["result"] == {
        "ok": False,
        "error": "CONFIRMATION_REQUIRED",
    }
    assert [b["action"] for b in data["buttons"]] == ["confirm_selection"]
    assert fake.cart_state(UID) == {}


def _fake_with_descriptions() -> FakeCommerce:
    books = [
        FakeBook(A, "책1", 15000, 50, description="입문자를 위한 책."),
        FakeBook(B, "책2", 22000, 50, description="실무자를 위한 책."),
        FakeBook(C, "책3", 28000, 50),
    ]
    return FakeCommerce(books, users=[UID])


def test_비교는_소개글_요약을_답에_쓰고_장바구니는_그대로다(monkeypatch) -> None:
    # 같은 가짜 LLM 이 도구 고르기, 소개글 읽기(두 번째 답), 최종 답 순서로 답한다
    _model, fake = _wire(
        monkeypatch,
        [
            ai_calls(
                (ToolName.BOOK_COMPARE, {"books": [card(1), card(2)]}),
                (ToolName.BOOK_OFFER, {"books": [card(1), card(2)]}),
            ),
            ai_text("책1은 입문용이고 책2는 실무용이에요."),
            ai_text("1번은 입문용이고 2번은 실무용이에요. 가격은 7,000원 차이예요."),
        ],
        _fake_with_descriptions(),
    )

    res = client.post(URL, json=_request(message="1번이랑 2번 뭐가 달라?"))

    assert res.status_code == 200
    data = res.json()["data"]
    assert (
        data["reply"] == "1번은 입문용이고 2번은 실무용이에요. 가격은 7,000원 차이예요."
    )
    compare = data["tool_calls"][0]
    assert compare["name"] == "book.compare"
    assert compare["arguments"] == {"isbns": [A, B]}
    assert compare["result"] == {
        "ok": True,
        "basis": "description",
        "summary": "책1은 입문용이고 책2는 실무용이에요.",
    }
    assert data["resolved_reference"] == {"ref": "카드 1번, 카드 2번", "isbns": [A, B]}
    assert data["selection"] is None
    assert fake.cart_state(UID) == {}


def test_소개글이_없는_책이_있으면_비교하지_못했다고_알린다(monkeypatch) -> None:
    _wire(
        monkeypatch,
        [
            ai_calls((ToolName.BOOK_COMPARE, {"books": [card(1), card(3)]})),
            ai_text("3번은 소개글이 없어서 비교하기 어려워요."),
        ],
        _fake_with_descriptions(),
    )

    res = client.post(URL, json=_request(message="1번이랑 3번 뭐가 달라?"))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["tool_calls"][0]["result"] == {"ok": False, "error": "NO_DESCRIPTION"}


def test_요청이_그대로_장바구니를_만드는_곳에_전달된다(monkeypatch) -> None:
    seen: list = []
    model = ScriptedModel([ai_text(NOT_SHOPPING)])
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    monkeypatch.setattr(agent, "get_commerce", lambda req: seen.append(req) or _fake())

    client.post(URL, json=_request(user_id=777))

    assert [req.user_id for req in seen] == [777]


def test_도구_없이_되묻는_답도_200이다(monkeypatch) -> None:
    _wire(monkeypatch, [ai_text("어느 책 말씀이신가요?")])

    res = client.post(URL, json=_request(message="그거 담아줘"))

    data = res.json()["data"]
    assert res.status_code == 200
    assert data["reply"] == "어느 책 말씀이신가요?"
    assert data["tool_calls"] == [] and data["buttons"] == []


def test_배송_문의는_주문내역_화면_안내_버튼이_붙는다(monkeypatch) -> None:
    _wire(
        monkeypatch,
        [
            ai_calls((ToolName.UI_GUIDE, {"target": "ORDER-LIST-001"})),
            ai_text("주문내역에서 확인하실 수 있어요."),
        ],
    )

    data = client.post(URL, json=_request(message="배송 언제 와?")).json()["data"]

    assert data["buttons"] == [
        {"label": "주문내역", "action": "navigate", "target": "ORDER-LIST-001"}
    ]
    assert data["tool_calls"][0]["name"] == "ui.guide"


def test_로그인하지_않은_사용자의_쓰기는_막히고_그_이유가_기록된다(monkeypatch) -> None:
    _model, fake = _wire(
        monkeypatch,
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1)})),
            ai_text("로그인하신 뒤에 담을 수 있어요."),
        ],
    )

    res = client.post(URL, json=_request(user_context={"logged_in": False}))

    data = res.json()["data"]
    assert res.status_code == 200
    assert data["tool_calls"][0]["result"] == {"ok": False, "error": "LOGIN_REQUIRED"}
    assert data["buttons"] == []  # 장바구니를 건드리지 못했다
    assert fake.cart_state(UID) == {}


def test_옛_명세의_칸을_보내도_400이_아니다(monkeypatch) -> None:
    _wire(monkeypatch, [ai_text(NOT_SHOPPING)])
    body = _request(book_id=1088, focused_book_id=1088)
    body["user_context"]["has_default_address"] = True

    assert client.post(URL, json=body).status_code == 200


# --- 답변 검사 ---


def test_도구를_안_불렀는데_담았다고_답하면_422다(monkeypatch) -> None:
    # 6단계 탐침의 실제 사고. 지운 뒤 남는 답이 없으면 답을 통째로 버린다
    _model, fake = _wire(
        monkeypatch, [ai_text("수량을 바꿨고 카드 2번도 담아드렸습니다.")]
    )

    res = client.post(
        URL, json=_request(message="파이썬 책 2권으로 바꾸고 2번도 담아줘")
    )

    assert res.status_code == 422
    assert res.json() == {"message": "grounding_failed", "data": None}
    assert fake.cart_state(UID) == {}


def test_담기가_성공한_턴은_LLM이_합계만_말해도_422가_아니라_결과를_말한다(
    monkeypatch,
) -> None:
    # 리뷰에서 확인한 사고: 담기가 성공했는데 답이 낡은 합계 한 문장이라 지워져 422 가 났다
    _wire(
        monkeypatch,
        [
            ai_calls((ToolName.CART_VIEW, {})),
            ai_calls((ToolName.CART_ADD, {"book": card(1)})),
            ai_text("합계는 99,000원입니다."),
        ],
        fake=_fake(seed=[(B, 1)]),
    )

    res = client.post(URL, json=_request(message="1번 담고 합계 알려줘"))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["reply"] == "1번 책을 장바구니에 담았어요."


def test_합계를_지어내면_그_문장만_지우고_200이다(monkeypatch) -> None:
    # 6단계 탐침의 실제 사고. 합계는 마지막 조회의 total(37,000원)이어야 한다
    _wire(
        monkeypatch,
        [
            ai_calls((ToolName.CART_VIEW, {})),
            ai_text("합계는 15,000원입니다. 장바구니를 확인해 보세요."),
        ],
        fake=_fake(seed=[(A, 1), (B, 1)]),
    )

    res = client.post(URL, json=_request(message="합계 알려줘"))

    data = res.json()["data"]
    assert res.status_code == 200
    assert data["reply"] == "장바구니를 확인해 보세요."
    assert data["tool_calls"][0]["grounded"] is False


def test_맞는_합계는_그대로_나간다(monkeypatch) -> None:
    _wire(
        monkeypatch,
        [ai_calls((ToolName.CART_VIEW, {})), ai_text("합계는 37,000원입니다.")],
        fake=_fake(seed=[(A, 1), (B, 1)]),
    )

    data = client.post(URL, json=_request(message="합계 알려줘")).json()["data"]

    assert data["reply"] == "합계는 37,000원입니다."
    assert data["tool_calls"][0]["grounded"] is True


# --- 오류 응답 ---


def _line(isbn: str, title: str, price: int, quantity: int) -> dict:
    return {
        "isbn": isbn,
        "title": title,
        "price": price,
        "quantity": quantity,
        "available": True,
    }


def _offer(isbn: str, title: str, price: int) -> dict:
    return {"isbn": isbn, "title": title, "price": price, "in_stock": True}


OFFERS = [_offer(A, "책1", 15000), _offer(B, "책2", 22000), _offer(C, "책3", 28000)]


def test_BE를_부르지_않고_요청의_장바구니로_돌아_바꿀_것을_돌려준다(
    monkeypatch,
) -> None:
    # A안(#489): 장바구니를 바꾸는 것은 BE 다. COMMERCE_MOCK 이 꺼진 운영에서도 503 이 아니다
    model = ScriptedModel(
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1), "quantity": 2})),
            ai_text("담았어요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    res = client.post(
        URL,
        json=_request(
            message="1번 2권 담아줘", cart=[_line(C, "책3", 28000, 1)], offers=OFFERS
        ),
    )

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["actions"] == [{"op": "set", "isbn": A, "quantity": 2}]
    assert data["tool_calls"][0]["result"] == {"ok": True}


def test_장바구니에_이미_있는_책은_요청의_장바구니로_알아본다(monkeypatch) -> None:
    model = ScriptedModel(
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("이미 담겨 있어요.")]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    res = client.post(
        URL, json=_request(cart=[_line(A, "책1", 15000, 3)], offers=OFFERS)
    )

    data = res.json()["data"]
    assert data["tool_calls"][0]["result"]["error"] == "ALREADY_IN_CART"
    assert data["actions"] == []


def test_빼기는_remove로_돌려주고_수량_칸이_없다(monkeypatch) -> None:
    model = ScriptedModel(
        [
            ai_calls((ToolName.CART_REMOVE, {"book": {"source": "cart", "n": 1}})),
            ai_text("뺐어요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    res = client.post(
        URL,
        json=_request(
            message="장바구니 1번째 빼줘",
            cart=[_line(B, "책2", 22000, 1), _line(C, "책3", 28000, 1)],
            offers=OFFERS,
        ),
    )

    assert res.json()["data"]["actions"] == [{"op": "remove", "isbn": B}]


def test_가격을_모르는_카드_책은_담지_못한다(monkeypatch) -> None:
    # BE 가 offers 에 싣지 않은 책(모르는 상품)은 AI 가 후보로 내지 않는다
    model = ScriptedModel(
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("담지 못했어요.")]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    res = client.post(URL, json=_request(offers=[]))

    data = res.json()["data"]
    assert data["tool_calls"][0]["result"]["error"] == "PRODUCT_NOT_FOUND"
    assert data["actions"] == []


def test_LLM_장애는_503과_Retry_After다(monkeypatch) -> None:
    request = httpx.Request("POST", "http://localhost/v1")
    _wire(monkeypatch, [openai.APIConnectionError(request=request)])

    res = client.post(URL, json=_request())

    assert res.status_code == 503
    assert res.json() == {"message": "upstream_unavailable", "data": None}
    assert res.headers["retry-after"] == "5"


def test_LLM_시간_초과는_504다(monkeypatch) -> None:
    request = httpx.Request("POST", "http://localhost/v1")
    _wire(monkeypatch, [openai.APITimeoutError(request=request)])

    res = client.post(URL, json=_request())

    assert res.status_code == 504
    assert res.json() == {"message": "generation_timeout", "data": None}


def test_도구를_실행한_뒤의_LLM_장애도_503이다(monkeypatch) -> None:
    request = httpx.Request("POST", "http://localhost/v1")
    _wire(
        monkeypatch,
        [
            ai_calls((ToolName.CART_VIEW, {})),
            openai.APIConnectionError(request=request),
        ],
    )

    assert client.post(URL, json=_request()).status_code == 503


def test_llm_mock이_켜져_있으면_503이다(monkeypatch) -> None:
    # llm_mock 의 가짜 모델은 도구 바인딩이 안 돼서 ⑦ 을 쓸 수 없다
    monkeypatch.setenv("LLM_MOCK", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(agent, "get_commerce", lambda user_id: _fake())

    res = client.post(URL, json=_request())

    assert res.status_code == 503


def test_agent_model은_llm_mock이면_LLMUnavailableError를_던진다(monkeypatch) -> None:
    monkeypatch.setenv("LLM_MOCK", "true")
    get_settings.cache_clear()

    with pytest.raises(LLMUnavailableError):
        agent.agent_model()


def test_예기치_못한_예외는_500이고_대화_원문이_응답과_로그에_남지_않는다(
    monkeypatch, caplog
) -> None:
    secret = "비밀스러운_사용자_발화"
    _wire(monkeypatch, [ai_text("네.")])

    def broken(run):
        raise ValueError(f"입력 검증 실패: {secret}")

    monkeypatch.setattr(agent, "finish", broken)

    with caplog.at_level(logging.DEBUG):
        res = client.post(URL, json=_request(message=secret))

    assert res.status_code == 500
    assert res.json() == {"message": "internal_server_error", "data": None}
    assert secret not in res.text
    assert secret not in caplog.text
    assert "ValueError" in caplog.text  # 종류는 남는다


def test_정상_처리의_로그에도_대화_원문이_없다(monkeypatch, caplog) -> None:
    # 카드 1번을 담으려면 사용자의 말에 "1번"이 있어야 한다(evidence). 로그에 없어야 하는 글은 그 말 전체다
    secret = "비밀스러운_사용자_발화 1번 담아줘"
    _wire(
        monkeypatch,
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("담았어요.")],
    )

    with caplog.at_level(logging.DEBUG):
        client.post(URL, json=_request(message=secret))

    assert secret not in caplog.text
    assert "llm_calls=2" in caplog.text  # 숫자는 남는다


# --- 요청 검사(LLM 과 BE 를 부르기 전에 끝난다) ---


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
def test_계약에_안_맞는_요청은_400이다(monkeypatch, overrides) -> None:
    called: list[str] = []
    monkeypatch.setattr(agent, "get_commerce", lambda user_id: called.append("be"))
    monkeypatch.setattr(agent, "agent_model", lambda: called.append("model"))

    res = client.post(URL, json=_request(**overrides))

    assert res.status_code == 400
    assert res.json() == INVALID
    assert called == []  # 검사에 걸리면 LLM 도 BE 도 부르지 않는다


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


def test_상한_안의_본문은_413이_아니다(monkeypatch) -> None:
    _wire(monkeypatch, [ai_text(NOT_SHOPPING)])
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


# --- 같은 요청의 재시도(멱등 키) ---


def _add_turn() -> list:
    return [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("담았어요.")]


def test_같은_키와_본문으로_다시_보내면_LLM을_부르지_않고_처음_응답을_돌려준다(
    monkeypatch, idempotency_rows
) -> None:
    model, _fake_be = _wire(
        monkeypatch, _add_turn()
    )  # 한 턴어치 답만 있다. 또 부르면 실패한다
    body = _request(message="1번 담아줘", idempotency_key="idem_retry")

    first = client.post(URL, json=body)
    llm_calls = len(model.calls)
    second = client.post(URL, json=body)

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert first.json()["data"]["reply"] == "1번 책을 장바구니에 담았어요."
    assert len(model.calls) == llm_calls  # 다시 처리하지 않았다


def test_처음_응답은_사용자에게_간_응답_그대로_저장된다(
    monkeypatch, idempotency_rows
) -> None:
    _wire(monkeypatch, _add_turn())
    body = _request(idempotency_key="idem_stored")

    res = client.post(URL, json=body)

    (stored,) = idempotency_rows.values()
    assert stored[1] == res.json()


def test_같은_키에_다른_본문이면_409다(monkeypatch, idempotency_rows) -> None:
    model, _fake_be = _wire(monkeypatch, _add_turn())
    client.post(URL, json=_request(message="1번 담아줘", idempotency_key="idem_x"))
    llm_calls = len(model.calls)

    res = client.post(
        URL, json=_request(message="2번 담아줘", idempotency_key="idem_x")
    )

    assert res.status_code == 409
    assert res.json() == {"message": "idempotency_conflict", "data": None}
    assert len(model.calls) == llm_calls  # 충돌이면 처리하지 않는다


def test_키가_다르면_각각_처리한다(monkeypatch, idempotency_rows) -> None:
    _model, fake = _wire(
        monkeypatch,
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1)})),
            ai_text("담았어요."),
            ai_calls((ToolName.CART_ADD, {"book": card(2)})),
            ai_text("담았어요."),
        ],
    )

    first = client.post(URL, json=_request(message="1번 담아줘", idempotency_key="k1"))
    second = client.post(URL, json=_request(message="2번 담아줘", idempotency_key="k2"))

    assert first.status_code == second.status_code == 200
    assert len(idempotency_rows) == 2
    assert fake.cart_state(UID) == {A: 1, B: 1}


def test_읽기만_한_턴은_저장하지_않고_같은_키로_다시_처리한다(
    monkeypatch, idempotency_rows
) -> None:
    model, _fake_be = _wire(
        monkeypatch,
        [
            ai_calls((ToolName.CART_VIEW, {})),
            ai_text("비어 있어요."),
            ai_calls((ToolName.CART_VIEW, {})),
            ai_text("비어 있어요."),
        ],
    )
    body = _request(message="장바구니 보여줘", idempotency_key="idem_read")

    client.post(URL, json=body)
    assert idempotency_rows == {}
    res = client.post(URL, json=body)

    assert res.status_code == 200
    assert len(model.calls) == 4  # 두 번 다 처리했다


def test_쓰기가_거절된_턴은_저장하지_않는다(monkeypatch, idempotency_rows) -> None:
    _wire(
        monkeypatch,
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1), "quantity": 9999})),
            ai_text("재고가 부족해요."),
        ],
    )

    res = client.post(URL, json=_request(idempotency_key="idem_rejected"))

    assert res.status_code == 200
    assert res.json()["data"]["tool_calls"][0]["result"]["ok"] is False
    assert idempotency_rows == {}


def test_오류_응답은_저장하지_않는다(monkeypatch, idempotency_rows) -> None:
    _wire(monkeypatch, [ai_text("담았어요.")])  # 도구 없이 담았다고 해서 422 다

    res = client.post(URL, json=_request(idempotency_key="idem_422"))

    assert res.status_code == 422
    assert idempotency_rows == {}


def test_기록소가_고장_나도_200으로_답한다(monkeypatch) -> None:
    # idempotency_rows 를 쓰지 않는다: 테스트 환경에는 DB 풀이 없어 조회도 저장도 실패한다
    _wire(monkeypatch, _add_turn())

    res = client.post(URL, json=_request(idempotency_key="idem_down"))

    assert res.status_code == 200
    assert res.json()["data"]["reply"] == "1번 책을 장바구니에 담았어요."


def _lose_the_race(monkeypatch, idempotency_rows, earlier_digest: str, body: dict):
    """조회할 때는 없었는데 저장할 때 보니 같은 키가 먼저 저장된 상황."""
    key = agent.replay.key_of(body["idempotency_key"])
    earlier = {"message": "agent_success", "data": {"reply": "먼저 끝난 응답"}}
    idempotency_rows[key] = (earlier_digest, earlier)

    async def not_found(key, digest):
        return None

    monkeypatch.setattr(agent.replay, "find", not_found)
    return earlier


def test_같은_요청이_동시에_처리되면_먼저_끝난_쪽의_응답이_나간다(
    monkeypatch, idempotency_rows
) -> None:
    _wire(monkeypatch, _add_turn())
    body = _request(idempotency_key="idem_race")
    earlier = _lose_the_race(
        monkeypatch, idempotency_rows, agent.idempotency.body_hash(body), body
    )

    res = client.post(URL, json=body)

    assert res.status_code == 200
    assert res.json() == earlier


def test_동시에_처리됐는데_본문이_다르면_409다(monkeypatch, idempotency_rows) -> None:
    _wire(monkeypatch, _add_turn())
    body = _request(idempotency_key="idem_race2")
    _lose_the_race(monkeypatch, idempotency_rows, "다른_본문의_해시", body)

    res = client.post(URL, json=body)

    assert res.status_code == 409
    assert res.json() == {"message": "idempotency_conflict", "data": None}


def test_멱등_로그에_대화_원문이_남지_않는다(
    monkeypatch, idempotency_rows, caplog
) -> None:
    _wire(monkeypatch, _add_turn())
    secret = "비밀스러운_사용자_발화"
    body = _request(message=secret, idempotency_key="idem_log")

    with caplog.at_level(logging.DEBUG):
        client.post(URL, json=body)
        client.post(URL, json=body)  # 재생 경로도

    assert secret not in caplog.text


# --- 턴 전체 시간 제한 ---


def test_턴이_시간_제한을_넘으면_504다(monkeypatch, idempotency_rows) -> None:
    monkeypatch.setenv("AGENT_TURN_TIMEOUT_SECONDS", "0.05")
    _wire(monkeypatch, [])

    async def too_slow(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(agent, "run_agent", too_slow)

    res = client.post(URL, json=_request(idempotency_key="idem_slow"))

    assert res.status_code == 504
    assert res.json() == {"message": "generation_timeout", "data": None}
    assert idempotency_rows == {}  # 오류 응답은 저장하지 않는다


def test_시간_제한_안에_끝나면_영향이_없다(monkeypatch, idempotency_rows) -> None:
    monkeypatch.setenv("AGENT_TURN_TIMEOUT_SECONDS", "5")
    _wire(monkeypatch, _add_turn())

    res = client.post(URL, json=_request(idempotency_key="idem_fast"))

    assert res.status_code == 200


def test_시간_제한의_기본값은_명세의_30초다() -> None:
    assert get_settings().agent_turn_timeout_seconds == 30


# --- 최근 대화 (#397) ---

PROPOSE_1 = [
    {"role": "user", "text": "그거 담아줘"},
    {"role": "assistant", "text": "카드 1번을 담을까요?"},
]


def test_긴_에이전트_답의_끝에_제안이_있어도_네라고_하면_끝까지_담긴다(
    monkeypatch,
) -> None:
    # ⑦의 답변은 200자를 넘을 수 있다. 호출자가 그대로 싣어도 400이 아니라 뒤쪽 200자로 풀린다(#402)
    _model, fake = _wire(monkeypatch, _add_turn())
    long_reply = "장바구니 사정을 길게 설명드립니다. " * 20 + "카드 1번을 담을까요?"
    turns = [
        {"role": "user", "text": "그거 담아줘"},
        {"role": "assistant", "text": long_reply},
    ]
    assert len(long_reply) > 200

    res = client.post(URL, json=_request(message="네", recent_turns=turns))

    assert res.status_code == 200
    assert fake.cart_state(UID) == {A: 1}


def _blocked_then_ask() -> list:
    # 담기를 시도했다가 막히고, 막힌 결과를 본 모델이 어느 책인지 되묻는 흐름
    return [
        ai_calls((ToolName.CART_ADD, {"book": card(1)})),
        ai_text("어느 책을 말씀하시는지 알려 주세요."),
    ]


def test_에이전트가_한_권을_제안했고_네라고_하면_끝까지_담긴다(monkeypatch) -> None:
    _model, fake = _wire(monkeypatch, _add_turn())

    res = client.post(URL, json=_request(message="네", recent_turns=PROPOSE_1))

    assert res.status_code == 200
    data = res.json()["data"]
    assert [c["name"] for c in data["tool_calls"]] == ["cart.add"]
    assert data["tool_calls"][0]["result"] == {"ok": True}
    assert data["resolved_reference"] == {"ref": "카드 1번", "isbns": [A]}
    assert fake.cart_state(UID) == {A: 1}


def test_같은_네라도_최근_대화가_없으면_담지_않는다(monkeypatch) -> None:
    # 이번 말만으로는 어느 책인지 몰라 코드가 막는다. 실제 모델은 막힌 결과를 보고 되묻는다
    _model, fake = _wire(monkeypatch, _blocked_then_ask())

    res = client.post(URL, json=_request(message="네"))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["tool_calls"][0]["result"] == {
        "ok": False,
        "error": "REFERENCE_NOT_STATED",
        "book": "카드 1번",
        "ask": "카드 1번을 담을까요?",
    }
    assert data["resolved_reference"] is None
    assert fake.cart_state(UID) == {}


def test_선택지를_나열한_말에_네라고_하면_담지_않는다(monkeypatch) -> None:
    _model, fake = _wire(monkeypatch, _blocked_then_ask())
    listed = [
        {"role": "user", "text": "그거 담아줘"},
        {"role": "assistant", "text": "카드 1번과 2번, 3번 중 어느 책을 담을까요?"},
    ]

    res = client.post(URL, json=_request(message="네", recent_turns=listed))

    assert res.status_code == 200
    assert fake.cart_state(UID) == {}


def test_최근_대화만_달라도_같은_키의_다른_본문이라_409다(
    monkeypatch, idempotency_rows
) -> None:
    model, _fake_be = _wire(monkeypatch, _add_turn())
    client.post(
        URL,
        json=_request(
            message="네", recent_turns=PROPOSE_1, idempotency_key="idem_turns"
        ),
    )
    llm_calls = len(model.calls)

    res = client.post(
        URL, json=_request(message="네", recent_turns=[], idempotency_key="idem_turns")
    )

    assert res.status_code == 409
    assert res.json() == {"message": "idempotency_conflict", "data": None}
    assert len(model.calls) == llm_calls


def test_최근_대화의_글도_로그에_남지_않는다(monkeypatch, caplog) -> None:
    secret = "비밀스러운_이전_대화"
    _wire(monkeypatch, _add_turn())
    turns = [
        {"role": "user", "text": secret},
        {"role": "assistant", "text": "카드 1번을 담을까요?"},
    ]

    with caplog.at_level(logging.DEBUG):
        client.post(URL, json=_request(message="네", recent_turns=turns))

    assert secret not in caplog.text


def test_최근_대화의_role이_틀리면_400이다(monkeypatch) -> None:
    _wire(monkeypatch, _add_turn())

    res = client.post(
        URL, json=_request(recent_turns=[{"role": "bot", "text": "안녕"}])
    )

    assert res.status_code == 400
    assert res.json() == INVALID


# --- 한 번에 시킨 쓰기는 모두 되거나 모두 안 된다 (#488, #489) ---


def _full_cart(n: int) -> list[dict]:
    return [_line(f"97911{i:08d}", f"꽂힌 책{i}", 10000, 1) for i in range(n)]


def test_한_번에_시킨_담기_중_하나가_한도에_걸리면_모두_되돌린다(monkeypatch) -> None:
    # 29권에 두 권을 담으면 두 번째가 30권 한도에 걸린다. 첫 권만 담기면 사용자가 모르는 사이 일부만 바뀐다
    model = ScriptedModel(
        [
            ai_calls(
                (ToolName.CART_ADD, {"book": card(1)}),
                (ToolName.CART_ADD, {"book": card(2)}),
            ),
            ai_text("둘 다 담았어요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    res = client.post(
        URL,
        json=_request(message="1번이랑 2번 담아줘", cart=_full_cart(29), offers=OFFERS),
    )

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["actions"] == []
    assert [c["result"].get("error") for c in data["tool_calls"]] == [
        "BATCH_ROLLED_BACK",
        "CART_ITEM_LIMIT_EXCEEDED",
    ]
    assert "가득 차서" in data["reply"]
    assert "함께 부탁하신 것 중 하나가 안 돼서" in data["reply"]
    assert "둘 다 담았어요" not in data["reply"]


def test_빼고_담기를_같이_시키면_뺀_자리에_담긴다(monkeypatch) -> None:
    model = ScriptedModel(
        [
            ai_calls(
                (ToolName.CART_REMOVE, {"book": {"source": "cart", "n": 1}}),
                (ToolName.CART_ADD, {"book": card(1)}),
            ),
            ai_text("바꿔 담았어요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    cart = _full_cart(30)

    res = client.post(
        URL,
        json=_request(
            message="장바구니 1번째 빼고 1번 담아줘", cart=cart, offers=OFFERS
        ),
    )

    assert res.json()["data"]["actions"] == [
        {"op": "remove", "isbn": cart[0]["isbn"]},
        {"op": "set", "isbn": A, "quantity": 1},
    ]


def test_따로_시킨_쓰기는_되돌리지_않고_한_것과_못_한_것을_말한다(monkeypatch) -> None:
    # 되돌리는 것은 LLM 이 한 번에 시킨 묶음 안에서만이다. 앞 묶음에서 한 일은 이미 결과를 말했다
    model = ScriptedModel(
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1)})),
            ai_calls((ToolName.CART_ADD, {"book": card(2)})),
            ai_text("결과예요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    res = client.post(
        URL,
        json=_request(message="1번이랑 2번 담아줘", cart=_full_cart(29), offers=OFFERS),
    )

    data = res.json()["data"]
    assert data["actions"] == [{"op": "set", "isbn": A, "quantity": 1}]
    assert "가득 차서" in data["reply"]


def test_책을_가리키는_근거가_없어_막힌_쓰기는_되돌릴_이유가_아니다(
    monkeypatch,
) -> None:
    # "1번 담아줘"에 LLM 이 3번까지 담으려 하면 3번만 막고(되묻기) 1번은 담는다
    model = ScriptedModel(
        [
            ai_calls(
                (ToolName.CART_ADD, {"book": card(1)}),
                (ToolName.CART_ADD, {"book": card(3)}),
            ),
            ai_text("담았어요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    res = client.post(URL, json=_request(message="1번 담아줘", offers=OFFERS))

    data = res.json()["data"]
    assert data["actions"] == [{"op": "set", "isbn": A, "quantity": 1}]
    assert data["tool_calls"][1]["result"]["error"] == "REFERENCE_NOT_STATED"


def _add_two_then_view() -> AIMessage:
    return ai_calls(
        (ToolName.CART_ADD, {"book": card(1)}),
        (ToolName.CART_ADD, {"book": card(2)}),
        (ToolName.CART_VIEW, {}),
    )


def _seen_view(model: ScriptedModel) -> dict:
    """두 번째 LLM 호출이 받은 장바구니 조회 결과."""
    (view,) = [
        m
        for m in model.calls[1]
        if isinstance(m, ToolMessage) and m.name == llm_name(ToolName.CART_VIEW)
    ]
    return json.loads(view.content)


def test_되돌린_묶음의_장바구니_조회는_되돌린_장바구니를_보여_준다(monkeypatch) -> None:
    # 29권에 1번·2번을 담고 장바구니를 보면 2번이 30권 한도에 걸려 1번도 되돌린다. 조회 결과가 되돌리기 전
    # 그대로면 "1번도 담지 않았어요" 옆에 1번이 담긴 목록과 틀린 합계가 LLM 에게 간다(#493 리뷰)
    model = ScriptedModel([_add_two_then_view(), ai_text("결과예요.")])
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    cart = _full_cart(29)

    res = client.post(
        URL,
        json=_request(
            message="1번이랑 2번 담고 장바구니 보여줘", cart=cart, offers=OFFERS
        ),
    )

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["actions"] == []
    view = data["tool_calls"][2]["result"]
    assert [item["isbn"] for item in view["items"]] == [line["isbn"] for line in cart]
    assert view["total"] == 290000
    seen = _seen_view(model)
    assert len(seen["items"]) == 29
    assert "책1" not in [item["title"] for item in seen["items"]]
    assert seen["total"] == 290000


def test_되돌린_묶음에서_본_장바구니가_다음_번호의_기준이다(monkeypatch) -> None:
    # 다음 묶음의 "장바구니 1번째"는 되돌린 장바구니의 1번째다. 되돌리기 전 목록이면 1번(맨 위에 담겼던 책)을
    # 빼려다 장바구니에 없다고 실패한다
    model = ScriptedModel(
        [
            _add_two_then_view(),
            ai_calls((ToolName.CART_REMOVE, {"book": {"source": "cart", "n": 1}})),
            ai_text("결과예요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    cart = _full_cart(29)

    res = client.post(
        URL,
        json=_request(
            message="1번이랑 2번 담고 장바구니 보여줘. 장바구니 1번째는 빼줘",
            cart=cart,
            offers=OFFERS,
        ),
    )

    data = res.json()["data"]
    assert data["tool_calls"][3]["result"] == {"ok": True}
    assert data["actions"] == [{"op": "remove", "isbn": cart[0]["isbn"]}]


# --- 장바구니를 바꾼 턴과 골라 담기는 섞지 않는다 ---
# BE(스택 6f98aae)는 selection 과 actions 가 같이 오면 응답 전체를 거절한다. 골라 담기는 확인 버튼으로 담고,
# actions 는 이미 했다고 말한 것이라 둘을 한 응답에 싣지 않는다. 먼저 한 쪽을 하고 뒤에 온 쪽을 막는다.

REMOVE_FIRST_LINE = {"book": {"source": "cart", "n": 1}}
MIXED_MESSAGE = "장바구니 1번째 빼고 4만원 안에서 2권 골라줘"


def _shop(monkeypatch, replies, message=MIXED_MESSAGE, cart=None) -> dict:
    model = ScriptedModel(replies)
    monkeypatch.setattr(agent, "agent_model", lambda: model)
    cart = [_line(C, "책3", 28000, 1)] if cart is None else cart
    res = client.post(URL, json=_request(message=message, cart=cart, offers=OFFERS))
    assert res.status_code == 200
    return res.json()["data"]


def _errors(data: dict) -> list:
    return [(c["name"], c["result"].get("error")) for c in data["tool_calls"]]


def test_장바구니를_바꾼_묶음의_골라_담기는_막는다(monkeypatch) -> None:
    data = _shop(
        monkeypatch,
        [
            ai_calls(
                (ToolName.CART_REMOVE, REMOVE_FIRST_LINE),
                (ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2}),
            ),
            ai_text("골라 드리는 것은 따로 말씀해 주세요."),
        ],
    )

    assert _errors(data) == [
        ("cart.remove", None),
        ("selection.pick", "SEPARATE_TURN_REQUIRED"),
    ]
    assert data["actions"] == [{"op": "remove", "isbn": C}]
    assert data["selection"] is None
    assert "confirm_selection" not in [b["action"] for b in data["buttons"]]


def test_골라_담기를_한_묶음의_장바구니_쓰기는_막는다(monkeypatch) -> None:
    data = _shop(
        monkeypatch,
        [
            ai_calls(
                (ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2}),
                (ToolName.CART_REMOVE, REMOVE_FIRST_LINE),
            ),
            ai_text("1번과 2번을 골랐어요. 담을까요?"),
        ],
    )

    assert _errors(data) == [
        ("selection.pick", None),
        ("cart.remove", "SEPARATE_TURN_REQUIRED"),
    ]
    assert data["actions"] == []
    assert [book["isbn"] for book in data["selection"]["chosen"]] == [A, B]
    assert [b["action"] for b in data["buttons"]] == ["confirm_selection"]


@pytest.mark.parametrize(
    ("first", "second", "blocked"),
    [
        pytest.param(
            (ToolName.CART_REMOVE, REMOVE_FIRST_LINE),
            (ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2}),
            "selection.pick",
            id="뺀_다음_묶음에서_고르기",
        ),
        pytest.param(
            (ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2}),
            (ToolName.CART_ADD, {"book": card(3)}),
            "cart.add",
            id="고른_다음_묶음에서_고르지_않은_책_담기",
        ),
    ],
)
def test_다음_묶음이라도_한_턴에_둘을_섞지_않는다(
    monkeypatch, first, second, blocked
) -> None:
    data = _shop(
        monkeypatch,
        [ai_calls(first), ai_calls(second), ai_text("결과예요.")],
        message="장바구니 1번째 빼고 4만원 안에서 2권 골라주고 3번도 담아줘",
        cart=[_line(C, "책3", 28000, 1)] if blocked == "selection.pick" else [],
    )

    assert _errors(data)[1] == (blocked, "SEPARATE_TURN_REQUIRED")
    assert not (data["selection"] is not None and data["actions"])


def test_되돌린_쓰기는_장바구니를_바꾼_것이_아니라_골라_담기를_막지_않는다(
    monkeypatch,
) -> None:
    # 29권에 두 권을 담다 한도에 걸려 모두 되돌렸으면 바꾼 것이 없다. 다음 묶음의 골라 담기는 된다
    data = _shop(
        monkeypatch,
        [
            ai_calls(
                (ToolName.CART_ADD, {"book": card(1)}),
                (ToolName.CART_ADD, {"book": card(2)}),
            ),
            ai_calls((ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2})),
            ai_text("결과예요."),
        ],
        message="1번이랑 2번 담아줘. 안 되면 4만원 안에서 2권 골라줘",
        cart=_full_cart(29),
    )

    assert data["actions"] == []
    assert data["selection"] is not None
    assert _errors(data)[2] == ("selection.pick", None)


def test_담긴_책에_수량을_말해_담으면_수량을_바꿨다고_말한다(monkeypatch) -> None:
    model = ScriptedModel(
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1), "quantity": 2})),
            ai_text("2권 담았어요."),
        ]
    )
    monkeypatch.setattr(agent, "agent_model", lambda: model)

    res = client.post(
        URL,
        json=_request(
            message="1번 2권 담아줘", cart=[_line(A, "책1", 15000, 3)], offers=OFFERS
        ),
    )

    data = res.json()["data"]
    assert data["actions"] == [{"op": "set", "isbn": A, "quantity": 2}]
    assert data["reply"] == "《책1》의 수량을 2권으로 바꿨어요."
