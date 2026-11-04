import asyncio

from app.agent.load_test_model import LoadTestModel
from app.agent.schemas import parse_request


def request(message):
    req = parse_request(
        {
            "user_id": 123,
            "message": message,
            "user_context": {"logged_in": True},
            "idempotency_key": "test",
            "context_cards": [{"isbn": "9788900000011", "rank": 1, "match_score": 80}],
        }
    )
    assert req is not None
    return req


def test_cart_add_mock_produces_tool_call_and_reply():
    model = LoadTestModel(request("1번 장바구니에 담아줘"), 0)
    bound = model.bind_tools([])
    first = asyncio.run(bound.ainvoke([]))
    second = asyncio.run(bound.ainvoke([]))
    assert first.tool_calls[0]["name"] == "cart_add"
    assert first.tool_calls[0]["args"] == {"book": {"source": "card", "n": 1}}
    assert not second.tool_calls


def test_other_shopping_request_returns_clarification_without_external_call():
    model = LoadTestModel(request("책 비교해줘"), 0)
    reply = asyncio.run(model.ainvoke([]))
    assert reply.content
    assert not reply.tool_calls
