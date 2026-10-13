"""⑦ LLM 에게 주는 도구 설명과 결과 보기(app/agent/descriptors.py, view.py) 테스트(#356). LLM 을 부르지 않는다."""

import json
import re

import pytest

from app.agent.descriptors import FROM_LLM_NAME, llm_name, tool_definitions
from app.agent.reference import TurnContext
from app.agent.schemas import ContextCard, ToolCall, ToolName
from app.agent.tools import ARG_MODELS
from app.agent.view import llm_view
from app.gateway.commerce import CartLine

A, B, C = (f"97889{n:08d}" for n in (1, 2, 3))
OPENAI_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _names(definitions: list[dict]) -> list[str]:
    return [d["function"]["name"] for d in definitions]


# --- 도구 설명 ---


def test_도구_이름은_OpenAI_함수_이름_규칙을_지킨다() -> None:
    # 점(.)이 들어가면 공급자가 요청을 거절한다
    for name in ToolName:
        assert OPENAI_NAME.fullmatch(llm_name(name)), name


def test_구현한_도구만_설명을_준다() -> None:
    assert _names(tool_definitions()) == [
        "cart_view",
        "cart_add",
        "cart_update",
        "cart_remove",
        "book_offer",
    ]
    assert set(FROM_LLM_NAME.values()) == set(ARG_MODELS)


def test_LLM_이름으로_도구를_되찾고_모르는_이름은_없다() -> None:
    assert FROM_LLM_NAME["cart_add"] == ToolName.CART_ADD
    assert "cart.add" not in FROM_LLM_NAME  # 점이 든 이름은 LLM 이 쓰지 않는다
    assert "selection_pick" not in FROM_LLM_NAME  # 아직 안 만든 도구


def test_허용한_도구만_설명을_준다() -> None:
    allowed = [ToolName.CART_VIEW, ToolName.BOOK_OFFER]

    assert _names(tool_definitions(allowed)) == ["cart_view", "book_offer"]
    assert tool_definitions([]) == []


def test_스키마에_ref가_남지_않는다() -> None:
    text = json.dumps(tool_definitions())

    assert "$ref" not in text
    assert "$defs" not in text


def test_도구마다_설명과_모르는_칸_금지가_있다() -> None:
    for definition in tool_definitions():
        function = definition["function"]
        assert definition["type"] == "function"
        assert function["description"]
        assert function["parameters"]["type"] == "object"
        # LLM 이 isbn 이나 user_id 를 끼워 넣지 못하게 스키마에서도 막는다
        assert function["parameters"]["additionalProperties"] is False


def test_책을_가리키는_인자에_쓰는_법이_설명되어_있다() -> None:
    parameters = {
        d["function"]["name"]: d["function"]["parameters"] for d in tool_definitions()
    }
    book = parameters["cart_add"]["properties"]["book"]

    assert (
        book["description"] == "담을 책"
    )  # 정의 쪽 설명이 아니라 자리의 설명이 이긴다
    assert book["properties"]["source"]["enum"] == ["card", "cart", "focused"]
    assert "몇 번째" in book["properties"]["n"]["description"]
    assert book["required"] == ["source"]


def test_책_목록_인자는_개수_제한이_스키마에_있다() -> None:
    books = tool_definitions()[-1]["function"]["parameters"]["properties"]["books"]

    assert books["minItems"] == 1
    assert books["maxItems"] == 10
    assert books["items"]["properties"]["source"]["enum"] == ["card", "cart", "focused"]


def test_도구_설명에_ISBN과_사용자_번호_칸이_없다() -> None:
    text = json.dumps(tool_definitions())

    assert '"isbn"' not in text
    assert '"user_id"' not in text


# --- 결과 보기 ---


def _ctx(**overrides) -> TurnContext:
    values = {
        "user_id": 1,
        "logged_in": True,
        "cards": [
            ContextCard(isbn=A, rank=1, match_score=90),
            ContextCard(isbn=B, rank=2, match_score=80),
        ],
        "focused_isbn": None,
        "allow_tools": None,
    }
    values.update(overrides)
    return TurnContext(**values)


def _call(name: ToolName, result: dict, arguments: dict | None = None) -> ToolCall:
    return ToolCall(name=name, arguments=arguments or {}, result=result, grounded=True)


def test_장바구니_조회는_ISBN을_가리고_번호와_합계를_준다() -> None:
    call = _call(
        ToolName.CART_VIEW,
        {
            "items": [
                {
                    "isbn": C,
                    "title": "책3",
                    "price": 12000,
                    "quantity": 2,
                    "available": True,
                },
                {
                    "isbn": A,
                    "title": "책1",
                    "price": None,
                    "quantity": 1,
                    "available": False,
                },
            ],
            "total": 24000,
        },
    )

    view = llm_view(_ctx(), call)

    assert view == {
        "items": [
            {"n": 1, "title": "책3", "price": 12000, "quantity": 2, "available": True},
            {"n": 2, "title": "책1", "price": None, "quantity": 1, "available": False},
        ],
        "total": 24000,
    }


def test_가격_조회는_ISBN_대신_카드_번호를_준다() -> None:
    call = _call(
        ToolName.BOOK_OFFER,
        {
            "offers": [
                {"isbn": B, "title": "책2", "price": 22000, "in_stock": True},
                {"isbn": A, "title": "책1", "price": 15000, "in_stock": False},
            ]
        },
    )

    view = llm_view(_ctx(), call)

    assert view["offers"][0] == {
        "book": "카드 2번",
        "title": "책2",
        "price": 22000,
        "in_stock": True,
    }
    assert view["offers"][1]["book"] == "카드 1번"


def test_카드가_아닌_책은_장바구니_번호나_지금_보는_책으로_부른다() -> None:
    ctx = _ctx(
        cart_snapshot=[
            CartLine(isbn=C, title="책3", price=1, quantity=1, available=True)
        ],
        focused_isbn=A,
        cards=[],
    )
    call = _call(
        ToolName.BOOK_OFFER,
        {
            "offers": [
                {"isbn": C, "title": "책3", "price": 1, "in_stock": True},
                {"isbn": A, "title": "책1", "price": 1, "in_stock": True},
                {"isbn": B, "title": "책2", "price": 1, "in_stock": True},
            ]
        },
    )

    books = [o["book"] for o in llm_view(ctx, call)["offers"]]

    assert books == ["장바구니 1번째", "지금 보는 책", "목록 밖의 책"]


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"ok": True}, id="성공"),
        pytest.param({"ok": False, "error": "LOGIN_REQUIRED"}, id="막힘"),
        pytest.param(
            {"ok": False, "error": "ALREADY_IN_CART", "title": "책1", "quantity": 2},
            id="이미_담김",
        ),
    ],
)
def test_쓰기_결과와_거절은_그대로_준다(result) -> None:
    assert llm_view(_ctx(), _call(ToolName.CART_ADD, result)) == result


def test_거절된_조회는_그대로_준다() -> None:
    failed = {"ok": False, "error": "TOOL_NOT_ALLOWED"}

    assert llm_view(_ctx(), _call(ToolName.CART_VIEW, failed)) == failed
    assert llm_view(_ctx(), _call(ToolName.BOOK_OFFER, failed)) == failed


def test_어떤_결과에도_ISBN이_남지_않는다() -> None:
    calls = [
        _call(
            ToolName.CART_VIEW,
            {
                "items": [
                    {
                        "isbn": A,
                        "title": "책1",
                        "price": 1,
                        "quantity": 1,
                        "available": True,
                    }
                ],
                "total": 1,
            },
        ),
        _call(
            ToolName.BOOK_OFFER,
            {"offers": [{"isbn": B, "title": "책2", "price": 1, "in_stock": True}]},
        ),
        _call(
            ToolName.CART_ADD,
            {"ok": False, "error": "ALREADY_IN_CART", "title": "책1", "quantity": 1},
        ),
    ]

    for call in calls:
        assert not re.search(r"[0-9]{13}", json.dumps(llm_view(_ctx(), call)))
