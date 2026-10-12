"""⑦ 그래프(app/agent/graph.py) 테스트(#356). 가짜 LLM(ScriptedModel)과 가짜 BE 로 돈다.

여기서 지키려는 것: LLM 이 무엇을 고르든 반복은 3번에서 멈추고, LLM 에게는 ISBN 이 한 번도 보이지 않고, 막힌
호출은 BE 를 바꾸지 못하며, 실행하지 않은 호출에도 LLM 에게 이유가 돌아간다. 실제 LLM 의 인자 정확도는
여기서 재지 않는다(실제 모델 탐침은 이슈 #356 의 코멘트에 있다).
"""

import asyncio
import json
import re

import httpx
import openai
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent import prompts
from app.agent.fake_llm import ScriptedModel, ai_calls, ai_text
from app.agent.graph import MAX_CALLS_PER_STEP, MAX_TOOL_STEPS, run_agent
from app.agent.schemas import AgentActRequest, ToolName, parse_request
from app.gateway.commerce_fake import FakeBook, FakeCommerce
from app.gateway.llm import LLMUnavailableError

UID = 1
ISBNS = [f"97889{n:08d}" for n in range(1, 5)]
A, B, C, D = ISBNS


def card(n: int) -> dict:
    return {"source": "card", "n": n}


def cart(n: int) -> dict:
    return {"source": "cart", "n": n}


def _request(
    message="1번 담아줘", *, logged_in=True, focused=None, allow_tools=None
) -> AgentActRequest:
    req = parse_request(
        {
            "user_id": UID,
            "message": message,
            "context_cards": [
                {"isbn": A, "rank": 1, "match_score": 92},
                {"isbn": B, "rank": 2, "match_score": 87},
            ],
            "focused_isbn": focused,
            "user_context": {"logged_in": logged_in},
            "allow_tools": allow_tools,
            "idempotency_key": "idem_1",
        }
    )
    assert req is not None
    return req


def _fake(seed=()) -> FakeCommerce:
    books = [
        FakeBook(isbn, f"책{i}", 10000 * i, 100) for i, isbn in enumerate(ISBNS, 1)
    ]
    fake = FakeCommerce(books, users=[UID, 2])
    for isbn, qty in seed:
        asyncio.run(fake.add_item(UID, isbn, qty))
    return fake


def _run(replies, req=None, fake=None):
    fake = fake or _fake()
    model = ScriptedModel(replies)
    run = asyncio.run(run_agent(req or _request(), model=model, gateway=fake))
    return run, model, fake


def _tool_messages(messages) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


# --- 기본 흐름 ---


def test_도구_하나를_부르고_글로_답하면_끝난다() -> None:
    run, _model, fake = _run(
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("담았어요.")]
    )

    assert run.final_text == "담았어요."
    assert [c.name for c in run.tool_calls] == [ToolName.CART_ADD]
    assert run.tool_calls[0].result == {"ok": True}
    assert run.iterations == 1
    assert run.llm_calls == 2
    assert run.hit_cap is False
    assert fake.cart_state(UID) == {A: 1}


def test_도구가_필요_없으면_LLM을_한_번만_부른다() -> None:
    run, _model, fake = _run([ai_text("그건 도와드리기 어려워요.")])

    assert run.tool_calls == []
    assert run.iterations == 0
    assert run.llm_calls == 1
    assert run.not_shopping is False
    assert fake.cart_state(UID) == {}


@pytest.mark.parametrize("text", ["NOT_SHOPPING", "  NOT_SHOPPING\n"])
def test_도구_없이_NOT_SHOPPING으로_답하면_쇼핑_아님이다(text) -> None:
    run, _model, _fake_be = _run([ai_text(text)], _request("안녕"))

    assert run.not_shopping is True


def test_도구를_불렀으면_NOT_SHOPPING이라고_써도_쇼핑_아님이_아니다() -> None:
    run, _model, _fake_be = _run(
        [ai_calls((ToolName.CART_VIEW, {})), ai_text("NOT_SHOPPING")]
    )

    assert run.not_shopping is False


def test_조회한_뒤_번호로_뺀다() -> None:
    run, _model, fake = _run(
        [
            ai_calls((ToolName.CART_VIEW, {})),
            ai_calls((ToolName.CART_REMOVE, {"book": cart(1)})),
            ai_text("뺐어요."),
        ],
        fake=_fake(seed=[(A, 1), (B, 1)]),
    )

    assert fake.cart_state(UID) == {A: 1}  # 나중에 담은 B 가 1번이다
    assert run.iterations == 2
    assert run.llm_calls == 3


def test_한_번에_여러_도구를_부르면_조회했던_목록_기준으로_번호를_푼다() -> None:
    # "1번 담고 장바구니 1번째 빼줘": 담기가 먼저 실행돼도 장바구니 1번은 LLM 이 본 목록의 1번이다
    run, _model, fake = _run(
        [
            ai_calls((ToolName.CART_VIEW, {})),
            ai_calls(
                (ToolName.CART_ADD, {"book": card(1)}),
                (ToolName.CART_REMOVE, {"book": cart(1)}),
            ),
            ai_text("바꿨어요."),
        ],
        fake=_fake(seed=[(C, 1)]),
    )

    assert fake.cart_state(UID) == {A: 1}
    assert [c.name for c in run.tool_calls] == [
        ToolName.CART_VIEW,
        ToolName.CART_ADD,
        ToolName.CART_REMOVE,
    ]


def test_그래프_상태는_조회한_장바구니를_남긴다() -> None:
    run, _model, _fake_be = _run(
        [ai_calls((ToolName.CART_VIEW, {})), ai_text("보여 드렸어요.")],
        fake=_fake(seed=[(A, 1)]),
    )

    assert [line.isbn for line in run.ctx.cart_snapshot] == [A]


# --- LLM 이 받는 것 ---


def test_LLM이_받는_어떤_메시지에도_ISBN이_없다() -> None:
    _run_result, model, _fake_be = _run(
        [
            ai_calls((ToolName.CART_VIEW, {})),
            ai_calls((ToolName.BOOK_OFFER, {"books": [card(1), card(2)]})),
            ai_calls((ToolName.CART_ADD, {"book": card(1)})),
            ai_text("담았어요."),
        ],
        _request(focused=D),
        fake=_fake(seed=[(C, 2)]),
    )

    seen = [m for call in model.calls for m in call]
    assert seen, "가짜 LLM 이 받은 메시지가 없다"
    for message in seen:
        text = (
            message.content
            if isinstance(message.content, str)
            else json.dumps(message.content)
        )
        assert not re.search(r"[0-9]{13}", text), (
            f"ISBN 이 LLM 에게 보였다: {text[:80]}"
        )
        for isbn in ISBNS:
            assert isbn not in text


def test_LLM이_받는_첫_메시지는_지시와_요청_상태와_사용자_말이다() -> None:
    _run_result, model, _fake_be = _run(
        [ai_text("네.")], _request("1번 담아줘", focused=C)
    )

    system, state, user = model.calls[0]
    assert isinstance(system, SystemMessage) and system.content == prompts.SYSTEM_PROMPT
    assert isinstance(state, SystemMessage)
    assert "로그인: 예" in state.content
    assert "1번, 2번" in state.content
    assert "지금 보고 있는 책: 있음" in state.content
    assert isinstance(user, HumanMessage) and user.content == "1번 담아줘"


def test_로그인하지_않았으면_요청_상태에_그렇게_적힌다() -> None:
    _run_result, model, _fake_be = _run([ai_text("네.")], _request(logged_in=False))

    assert "로그인: 아니오" in model.calls[0][1].content


def test_도구_결과는_JSON_글자로_LLM에게_돌아간다() -> None:
    _run_result, model, _fake_be = _run(
        [ai_calls((ToolName.CART_VIEW, {})), ai_text("네.")], fake=_fake(seed=[(A, 2)])
    )

    reply = _tool_messages(model.calls[1])[0]
    assert reply.tool_call_id == "call_0"
    assert json.loads(reply.content) == {
        "items": [
            {"n": 1, "title": "책1", "price": 10000, "quantity": 2, "available": True}
        ],
        "total": 20000,
    }


def test_LLM에게_주는_도구는_구현한_6개이고_허용한_것만이다() -> None:
    _run_result, model, _fake_be = _run([ai_text("네.")])
    names = [t["function"]["name"] for t in model.tools_seen[0]]
    assert names == [
        "cart_view",
        "cart_add",
        "cart_update",
        "cart_remove",
        "book_offer",
        "ui_guide",
    ]

    _run_result, model, _fake_be = _run(
        [ai_text("네.")], _request(allow_tools=["cart.view", "book.offer"])
    )
    assert [t["function"]["name"] for t in model.tools_seen[0]] == [
        "cart_view",
        "book_offer",
    ]


def test_허용한_도구가_하나도_없으면_도구_없이_부른다() -> None:
    # 빈 도구 목록은 공급자가 거절한다
    _run_result, model, _fake_be = _run([ai_text("네.")], _request(allow_tools=[]))

    assert model.tools_seen == []
    assert model.bind_kwargs == [{"tools": False}]


# --- 반복 상한 ---


def test_반복은_3번에서_멈추고_마지막_호출은_도구를_못_쓰게_한다() -> None:
    run, model, _fake_be = _run(
        [ai_calls((ToolName.CART_VIEW, {}))] * MAX_TOOL_STEPS
        + [ai_text("여기까지 확인했어요.")]
    )

    assert run.iterations == MAX_TOOL_STEPS
    assert run.llm_calls == MAX_TOOL_STEPS + 1
    assert run.hit_cap is True
    assert run.final_text == "여기까지 확인했어요."
    assert model.bind_kwargs[:MAX_TOOL_STEPS] == [{}] * MAX_TOOL_STEPS
    assert model.bind_kwargs[-1] == {"tool_choice": "none"}


def test_상한_뒤의_호출이_그래도_도구를_부르면_실행하지_않고_끝낸다() -> None:
    run, _model, fake = _run(
        [ai_calls((ToolName.CART_VIEW, {}))] * MAX_TOOL_STEPS
        + [ai_calls((ToolName.CART_ADD, {"book": card(1)}))]
    )

    assert run.iterations == MAX_TOOL_STEPS
    assert run.final_text == ""
    assert len(run.tool_calls) == MAX_TOOL_STEPS  # 네 번째 호출은 기록에도 없다
    assert fake.cart_state(UID) == {}  # 실행되지 않았다


def test_상한을_다_쓰지_않으면_상한에_걸린_것이_아니다() -> None:
    run, _model, _fake_be = _run([ai_calls((ToolName.CART_VIEW, {})), ai_text("끝.")])

    assert run.hit_cap is False


def test_한_번에_부를_수_있는_도구_수를_넘으면_넘는_호출은_실행하지_않는다() -> None:
    calls = [(ToolName.CART_VIEW, {})] * (MAX_CALLS_PER_STEP + 2)
    run, model, _fake_be = _run([ai_calls(*calls), ai_text("네.")])

    assert len(run.tool_calls) == MAX_CALLS_PER_STEP
    replies = _tool_messages(model.calls[1])
    assert len(replies) == MAX_CALLS_PER_STEP + 2  # 호출마다 답이 돌아가야 한다
    assert [json.loads(r.content).get("error") for r in replies[-2:]] == [
        "TOO_MANY_CALLS"
    ] * 2


# --- LLM 이 잘못 부르면 ---


def test_없는_도구_이름은_실행하지_않고_이유를_돌려준다() -> None:
    run, model, fake = _run(
        [ai_calls(("order_create", {"book": card(1)})), ai_text("못 해요.")]
    )

    assert run.tool_calls == []  # 도구가 아니라서 기록하지 않는다
    reply = _tool_messages(model.calls[1])[0]
    assert json.loads(reply.content) == {"ok": False, "error": "UNKNOWN_TOOL"}
    assert fake.cart_state(UID) == {}


def test_아직_안_만든_도구_이름도_모르는_도구다() -> None:
    run, model, _fake_be = _run([ai_calls(("selection_pick", {})), ai_text("네.")])

    assert run.tool_calls == []
    assert (
        json.loads(_tool_messages(model.calls[1])[0].content)["error"] == "UNKNOWN_TOOL"
    )


def test_깨진_인자는_실행하지_않고_이유를_돌려준다() -> None:
    broken = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "cart_add",
                "args": "{not json",
                "id": "bad_1",
                "error": "x",
                "type": "invalid_tool_call",
            }
        ],
    )
    run, model, fake = _run([broken, ai_text("다시 말씀해 주세요.")])

    reply = _tool_messages(model.calls[1])[0]
    assert reply.tool_call_id == "bad_1"
    assert json.loads(reply.content) == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert run.iterations == 1
    assert fake.cart_state(UID) == {}


def test_LLM이_ISBN을_인자에_넣어도_BE까지_가지_않는다() -> None:
    run, model, fake = _run(
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1), "isbn": C})),
            ai_text("못 담았어요."),
        ]
    )

    assert run.tool_calls[0].result == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert fake.cart_state(UID) == {}
    assert (
        json.loads(_tool_messages(model.calls[1])[0].content)["error"]
        == "INVALID_ARGUMENTS"
    )


def test_LLM이_사용자_번호를_인자에_넣어도_요청의_사용자만_바뀐다() -> None:
    fake = _fake()
    _run(
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1), "user_id": 2})),
            ai_text("네."),
        ],
        fake=fake,
    )

    assert fake.cart_state(2) == {}
    assert fake.cart_state(UID) == {}  # 인자가 틀려서 아무도 안 바뀐다


def test_로그인하지_않으면_쓰기는_BE를_바꾸지_못하고_이유가_LLM에게_돌아간다() -> None:
    run, model, fake = _run(
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("로그인해 주세요.")],
        _request(logged_in=False),
    )

    assert run.tool_calls[0].result == {"ok": False, "error": "LOGIN_REQUIRED"}
    assert (
        json.loads(_tool_messages(model.calls[1])[0].content)["error"]
        == "LOGIN_REQUIRED"
    )
    assert fake.cart_state(UID) == {}


def test_허용하지_않은_도구를_LLM이_불러도_실행하지_않는다() -> None:
    run, _model, fake = _run(
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("네.")],
        _request(allow_tools=["cart.view"]),
    )

    assert run.tool_calls[0].result == {"ok": False, "error": "TOOL_NOT_ALLOWED"}
    assert fake.cart_state(UID) == {}


# --- 시간·토큰·장애 ---


def test_LLM_호출과_도구_실행의_시간과_토큰을_기록한다() -> None:
    first = ai_calls((ToolName.CART_VIEW, {}))
    first.usage_metadata = {
        "input_tokens": 100,
        "output_tokens": 20,
        "total_tokens": 120,
    }
    last = ai_text("네.")
    last.usage_metadata = {
        "input_tokens": 150,
        "output_tokens": 10,
        "total_tokens": 160,
    }

    run, _model, _fake_be = _run([first, last])

    assert [t.kind for t in run.timings] == ["llm", "tools", "llm"]
    assert all(t.ms >= 0 for t in run.timings)
    assert sum(t.input_tokens for t in run.timings) == 250
    assert sum(t.output_tokens for t in run.timings) == 30
    assert sum(t.kind == "llm" for t in run.timings) == run.llm_calls


def test_LLM_장애는_LLMUnavailableError로_올라간다() -> None:
    request = httpx.Request("POST", "http://localhost/v1")

    with pytest.raises(LLMUnavailableError):
        _run([openai.APIConnectionError(request=request)])


def test_도구를_실행한_뒤의_LLM_장애도_올라가고_이미_바뀐_장바구니는_그대로다() -> None:
    request = httpx.Request("POST", "http://localhost/v1")
    fake = _fake()

    with pytest.raises(LLMUnavailableError):
        _run(
            [
                ai_calls((ToolName.CART_ADD, {"book": card(1)})),
                openai.APIConnectionError(request=request),
            ],
            fake=fake,
        )

    # 이 단계는 되돌리지 않는다. 이미 실행한 쓰기는 BE 에 남는다(그래서 쓰기는 사용자가 시킨 것만 한다)
    assert fake.cart_state(UID) == {A: 1}


def test_가짜_LLM의_답이_모자라면_실패한다() -> None:
    # 그래프가 예상보다 더 부르면 테스트가 알려 준다
    with pytest.raises(AssertionError):
        _run([ai_calls((ToolName.CART_VIEW, {}))])
