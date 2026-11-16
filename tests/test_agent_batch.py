"""⑦ 한 묶음의 쓰기를 실행 전에 같이 따져 보는 것(app/agent/batch.py) 테스트(#488).

여기서 지키려는 것: 한 묶음의 쓰기 중 하나가 미리 알 수 있는 이유로 안 되면 BE 에 쓰기를 한 번도 보내지 않고,
미리 알 수 없거나 따질 필요가 없는 경우는 지금처럼 하나씩 실행한다. LLM 없이 가짜 BE 로 돈다.
"""

import asyncio

import pytest

from app.agent.batch import plan_writes
from app.agent.errors import ToolErrorCode
from app.agent.fake_llm import ScriptedModel, ai_calls, ai_text
from app.agent.graph import run_agent
from app.agent.reference import TurnContext
from app.agent.reply import BATCH_NOTE, finish
from app.agent.schemas import ContextCard, ToolName, parse_request
from app.agent.tools import execute, write_intent
from app.gateway.commerce import (
    MAX_CART_ITEMS,
    CommerceError,
    CommerceErrorCode,
)
from app.gateway.commerce_fake import FakeBook, FakeCommerce

UID = 1
NEW = [f"97889{n:08d}" for n in range(1, 5)]  # 카드로 보이는 새 책 4권
A, B, C, D = NEW
FILLER = [
    f"97889{n:08d}" for n in range(100, 100 + MAX_CART_ITEMS)
]  # 장바구니를 채울 책
SOLD_OUT = "9788900000200"

LIMIT = CommerceErrorCode.CART_ITEM_LIMIT_EXCEEDED.value
NO_STOCK = CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK.value
BAD_QTY = CommerceErrorCode.INVALID_CART_ITEM_QUANTITY.value
NOT_FOUND = CommerceErrorCode.CART_ITEM_NOT_FOUND.value
TOGETHER = ToolErrorCode.NOT_APPLIED_WITH_OTHERS.value

WRITES = ("add_item", "set_quantity", "remove_item")


class _Counting(FakeCommerce):
    """BE 로 나간 호출을 센다. 막힌 쓰기가 BE 까지 갔는지 본다."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.calls: list[str] = []
        self.keys: list[str | None] = []  # 쓰기 호출에 실려 간 중복 방지 키

    async def get_cart(self, *args, **kwargs):
        self.calls.append("get_cart")
        return await super().get_cart(*args, **kwargs)

    async def get_offers(self, *args, **kwargs):
        self.calls.append("get_offers")
        return await super().get_offers(*args, **kwargs)

    async def add_item(self, *args, **kwargs):
        self.calls.append("add_item")
        self.keys.append(kwargs.get("idempotency_key"))
        return await super().add_item(*args, **kwargs)

    async def set_quantity(self, *args, **kwargs):
        self.calls.append("set_quantity")
        self.keys.append(kwargs.get("idempotency_key"))
        return await super().set_quantity(*args, **kwargs)

    async def remove_item(self, *args, **kwargs):
        self.calls.append("remove_item")
        self.keys.append(kwargs.get("idempotency_key"))
        return await super().remove_item(*args, **kwargs)

    @property
    def writes(self) -> list[str]:
        return [c for c in self.calls if c in WRITES]


def _books() -> list[FakeBook]:
    books = [
        FakeBook(isbn, f"새책{i}", 10000 + i, 100) for i, isbn in enumerate(NEW, 1)
    ]
    books += [FakeBook(isbn, f"담긴책{i}", 9000, 100) for i, isbn in enumerate(FILLER)]
    books.append(FakeBook(SOLD_OUT, "품절책", 12000, 0))
    return books


def _fake(filled: int = 0, extra=()) -> _Counting:
    """filled 권을 채운 장바구니. extra 는 그 뒤에 더 담을 (isbn, 수량)."""
    fake = _Counting(_books(), users=[UID])
    for isbn in FILLER[:filled]:
        asyncio.run(fake.add_item(UID, isbn, 1))
    for isbn, qty in extra:
        asyncio.run(fake.add_item(UID, isbn, qty))
    fake.calls.clear()  # 준비하느라 부른 호출은 세지 않는다
    fake.keys.clear()
    return fake


def _cards(*isbns: str) -> list[ContextCard]:
    return [
        ContextCard(isbn=isbn, rank=rank, match_score=90)
        for rank, isbn in enumerate(isbns, 1)
    ]


def _ctx(fake: FakeCommerce, message: str, cards=NEW, **overrides) -> TurnContext:
    values = {
        "user_id": UID,
        "logged_in": True,
        "cards": _cards(*cards),
        "focused_isbn": None,
        "allow_tools": None,
        "message": message,
        "cart_snapshot": asyncio.run(fake.get_cart(UID)),
    }
    values.update(overrides)
    if isinstance(fake, _Counting):
        fake.calls.clear()  # 요청 상태를 만들려고 읽은 것은 세지 않는다
    return TurnContext(**values)


def card(n: int) -> dict:
    return {"source": "card", "n": n}


def cart(n: int) -> dict:
    return {"source": "cart", "n": n}


def _adds(*numbers: int, quantity: int | None = None):
    """카드 번호마다 담기 호출 하나씩. (위치, 도구, 인자)"""
    args = [{"book": card(n)} for n in numbers]
    if quantity is not None:
        args = [{**a, "quantity": quantity} for a in args]
    return [(i, ToolName.CART_ADD, a) for i, a in enumerate(args)]


def _plan(ctx, fake, calls):
    return asyncio.run(plan_writes(ctx, fake, calls))


def _codes(plan) -> dict[int, str]:
    return {index: held.code for index, held in plan.items()}


MESSAGE_ALL = "1번이랑 2번이랑 3번이랑 4번 담아줘"


# --- 묶음을 같이 따져 본다 ---


def test_27권에_새_책_4권을_담으면_마지막이_한도라서_넷_다_실행하지_않는다() -> None:
    fake = _fake(filled=27)
    ctx = _ctx(fake, MESSAGE_ALL)

    plan = _plan(ctx, fake, _adds(1, 2, 3, 4))

    assert _codes(plan) == {0: TOGETHER, 1: TOGETHER, 2: TOGETHER, 3: LIMIT}
    assert fake.writes == []
    assert ctx.writes_halted is True


def test_27권에_새_책_3권은_자리가_맞아서_따지지_않고_그대로_실행한다() -> None:
    fake = _fake(filled=27)
    ctx = _ctx(fake, "1번이랑 2번이랑 3번 담아줘")

    plan = _plan(ctx, fake, _adds(1, 2, 3))

    assert plan == {}
    assert ctx.writes_halted is False


def test_품절_책이_섞여_있으면_멀쩡한_책도_담지_않는다() -> None:
    fake = _fake()
    ctx = _ctx(fake, "1번이랑 2번 담아줘", cards=[A, SOLD_OUT])

    plan = _plan(ctx, fake, _adds(1, 2))

    assert _codes(plan) == {0: TOGETHER, 1: NO_STOCK}
    assert fake.writes == []


def test_BE가_모르는_책은_담을_수_없다는_이유로_함께_막는다() -> None:
    fake = _fake()
    fake.discontinue(B)
    ctx = _ctx(fake, "1번이랑 2번 담아줘")

    plan = _plan(ctx, fake, _adds(1, 2))

    assert _codes(plan) == {
        0: TOGETHER,
        1: CommerceErrorCode.PRODUCT_NOT_FOUND.value,
    }


def test_수량이_범위_밖인_책이_있으면_그_이유로_막고_나머지도_담지_않는다() -> None:
    fake = _fake()
    ctx = _ctx(fake, "1번 600권이랑 2번 담아줘")
    calls = [
        (0, ToolName.CART_ADD, {"book": card(1), "quantity": 600}),
        (1, ToolName.CART_ADD, {"book": card(2)}),
    ]

    plan = _plan(ctx, fake, calls)

    assert _codes(plan) == {0: BAD_QTY, 1: TOGETHER}


def test_먼저_빼면_그_자리가_나서_30권에서_하나_빼고_새_책을_담는_것은_막지_않는다() -> (
    None
):
    fake = _fake(filled=MAX_CART_ITEMS)
    ctx = _ctx(fake, "장바구니 1번째 빼고 카드 1번 담아줘")
    calls = [
        (0, ToolName.CART_REMOVE, {"book": cart(1)}),
        (1, ToolName.CART_ADD, {"book": card(1)}),
    ]

    assert _plan(ctx, fake, calls) == {}


def test_빼는_것이_뒤에_있으면_담을_때_자리가_없어서_둘_다_하지_않는다() -> None:
    fake = _fake(filled=MAX_CART_ITEMS)
    ctx = _ctx(fake, "카드 1번 담고 장바구니 1번째 빼줘")
    calls = [
        (0, ToolName.CART_ADD, {"book": card(1)}),
        (1, ToolName.CART_REMOVE, {"book": cart(1)}),
    ]

    assert _codes(_plan(ctx, fake, calls)) == {0: LIMIT, 1: TOGETHER}


def test_이미_담긴_책은_다른_쓰기를_막지도_자리를_차지하지도_않는다() -> None:
    fake = _fake(extra=[(A, 1)])
    ctx = _ctx(fake, "1번이랑 2번 담아줘")

    assert _plan(ctx, fake, _adds(1, 2)) == {}
    assert ctx.writes_halted is False


def test_30권이_찬_장바구니에서_이미_담긴_책과_새_책이면_새_책만_BE가_거절하게_둔다() -> (
    None
):
    fake = _fake(filled=MAX_CART_ITEMS - 1, extra=[(A, 1)])
    ctx = _ctx(fake, "1번이랑 2번 담아줘")

    # 담긴 책(1번)은 바뀌지 않고 알리기만 한다. 함께 막을 쓰기가 없어서 따지지 않는다
    assert _plan(ctx, fake, _adds(1, 2)) == {}


def test_빼려는_책이_담겨_있지_않으면_그_이유로_막고_함께_요청된_담기도_하지_않는다() -> (
    None
):
    fake = _fake()
    ctx = _ctx(fake, "카드 1번 담고 카드 2번 빼줘")
    calls = [
        (0, ToolName.CART_ADD, {"book": card(1)}),
        (1, ToolName.CART_REMOVE, {"book": card(2)}),
    ]

    assert _codes(_plan(ctx, fake, calls)) == {0: TOGETHER, 1: NOT_FOUND}


def test_수량_변경도_재고가_없으면_함께_막는다() -> None:
    fake = _fake(extra=[(A, 1), (B, 1)])
    fake.set_stock(B, 0)  # 담은 뒤에 품절이 됐다
    ctx = _ctx(fake, "카드 1번 2권이랑 카드 2번 3권으로 바꿔줘", cards=[A, B])
    calls = [
        (0, ToolName.CART_UPDATE, {"book": card(1), "quantity": 2}),
        (1, ToolName.CART_UPDATE, {"book": card(2), "quantity": 3}),
    ]

    assert _codes(_plan(ctx, fake, calls)) == {0: TOGETHER, 1: NO_STOCK}


def test_본_뒤에_수량이_바뀐_책이_묶음에_있으면_그_이유로_막고_나머지도_하지_않는다() -> (
    None
):
    fake = _fake(extra=[(A, 2)])
    ctx = _ctx(fake, "카드 1번 3권으로 바꾸고 카드 2번 담아줘")  # 이 시점의 A 는 2권
    asyncio.run(fake.set_quantity(UID, A, 5))  # 그사이 다른 곳에서 바뀌었다
    calls = [
        (0, ToolName.CART_UPDATE, {"book": card(1), "quantity": 3}),
        (1, ToolName.CART_ADD, {"book": card(2)}),
    ]

    plan = _plan(ctx, fake, calls)

    assert _codes(plan) == {
        0: CommerceErrorCode.CART_QUANTITY_CHANGED.value,
        1: TOGETHER,
    }


def test_같은_묶음에서_한_책을_두_번_바꾸면_앞선_결과를_본_수량으로_삼아_막지_않는다() -> (
    None
):
    fake = _fake(extra=[(A, 2)])
    ctx = _ctx(fake, "카드 1번 3권으로 바꾸고 카드 1번 4권으로 바꿔줘")
    calls = [
        (0, ToolName.CART_UPDATE, {"book": card(1), "quantity": 3}),
        (1, ToolName.CART_UPDATE, {"book": card(1), "quantity": 4}),
    ]

    assert _plan(ctx, fake, calls) == {}


# --- 따지지 않는 경우 ---


def test_쓰기가_하나면_BE를_읽지도_않는다() -> None:
    fake = _fake(filled=MAX_CART_ITEMS)
    ctx = _ctx(fake, "1번 담아줘")
    fake.calls.clear()

    assert _plan(ctx, fake, _adds(1)) == {}
    assert fake.calls == []


def test_읽기_호출은_묶음에_세지_않는다() -> None:
    fake = _fake()
    ctx = _ctx(fake, "1번 담고 장바구니 보여줘")
    calls = [
        (0, ToolName.CART_ADD, {"book": card(1)}),
        (1, ToolName.CART_VIEW, {}),
    ]

    assert _plan(ctx, fake, calls) == {}
    assert fake.calls == []


def test_근거가_없어_막힐_호출은_묶음에_넣지_않는다() -> None:
    # "2번"을 말하지 않았으니 둘째 호출은 REFERENCE_NOT_STATED 로 막힌다. 그 책만 되묻고 첫째는 실행한다
    fake = _fake(filled=MAX_CART_ITEMS)
    ctx = _ctx(fake, "1번 담아줘")

    assert _plan(ctx, fake, _adds(1, 2)) == {}


def test_로그인하지_않았으면_따지지_않는다() -> None:
    fake = _fake(filled=27)
    ctx = _ctx(fake, MESSAGE_ALL, logged_in=False)

    assert _plan(ctx, fake, _adds(1, 2, 3, 4)) == {}
    assert fake.calls == []


def test_장바구니를_읽을_수_없으면_미리_걸러_내지_않고_하나씩_실행하게_둔다() -> None:
    class Down(_Counting):
        async def get_cart(self, *args, **kwargs):
            raise CommerceError(CommerceErrorCode.CART_NOT_FOUND)

    down = Down(_books(), users=[UID])
    ctx = _ctx(_fake(), MESSAGE_ALL)

    assert _plan(ctx, down, _adds(1, 2, 3, 4)) == {}
    assert ctx.writes_halted is False


# --- 한 번 막은 턴 ---


def test_한_번_막은_턴은_뒤_묶음의_쓰기도_모두_막는다() -> None:
    fake = _fake(filled=27)
    ctx = _ctx(fake, MESSAGE_ALL)
    _plan(ctx, fake, _adds(1, 2, 3, 4))
    fake.calls.clear()

    # LLM 이 되는 것만 골라 다시 부르거나, 하나만 다시 불러도 막는다
    again = _plan(ctx, fake, _adds(1, 2, 3))
    single = _plan(ctx, fake, _adds(1))

    assert _codes(again) == {0: TOGETHER, 1: TOGETHER, 2: TOGETHER}
    assert _codes(single) == {0: TOGETHER}
    assert fake.calls == []


def test_막은_뒤에도_가리킬_수_없는_호출은_막지_않고_그대로_실패시킨다() -> None:
    fake = _fake(filled=27)
    ctx = _ctx(fake, MESSAGE_ALL)
    _plan(ctx, fake, _adds(1, 2, 3, 4))

    # 9번 카드는 없다. 책을 풀 수 없는 호출은 쓰기 의도가 아니라서 execute 가 이유를 남긴다
    assert _plan(ctx, fake, [(0, ToolName.CART_ADD, {"book": card(9)})]) == {}


# --- 미리 본 것과 실제 실행이 같은 결정을 내린다 ---


def _ctx_for(case: str, fake: FakeCommerce) -> tuple[TurnContext, ToolName, object]:
    ctx = _ctx(fake, "1번이랑 2번 담아줘")
    name, args = ToolName.CART_ADD, {"book": card(1)}
    if case == "ok":
        pass
    elif case == "tool_not_allowed":
        ctx.allow_tools = [ToolName.CART_VIEW]
    elif case == "logged_out":
        ctx.logged_in = False
    elif case == "extra_field":
        args = {"book": card(1), "isbn": A}
    elif case == "bad_type":
        args = {"book": card(1), "quantity": "2"}
    elif case == "not_an_object":
        args = "1번"
    elif case == "no_such_card":
        args = {"book": card(9)}
    elif case == "not_stated":
        ctx.message = "1번 담아줘"
        args = {"book": card(2)}
    elif case == "picked_by_ai":
        ctx.picked.add(A)
    elif case == "already_in_cart":
        asyncio.run(fake.add_item(UID, A, 1))
    elif case == "update_ok":
        asyncio.run(fake.add_item(UID, A, 1))
        name, args = ToolName.CART_UPDATE, {"book": card(1), "quantity": 2}
    elif case == "update_not_in_cart":
        name, args = ToolName.CART_UPDATE, {"book": card(1), "quantity": 2}
    elif case == "remove_not_in_cart":
        name, args = ToolName.CART_REMOVE, {"book": card(1)}
    elif case == "remove_unknown_cart_number":
        ctx.cart_snapshot = []
        name, args = ToolName.CART_REMOVE, {"book": cart(3)}
    elif case == "view":
        name, args = ToolName.CART_VIEW, {}
    else:  # pragma: no cover
        raise AssertionError(case)
    return ctx, name, args


@pytest.mark.parametrize(
    "case",
    [
        "ok",
        "tool_not_allowed",
        "logged_out",
        "extra_field",
        "bad_type",
        "not_an_object",
        "no_such_card",
        "not_stated",
        "picked_by_ai",
        "already_in_cart",
        "update_ok",
        "update_not_in_cart",
        "remove_not_in_cart",
        "remove_unknown_cart_number",
        "view",
    ],
)
def test_쓰기_의도가_있으면_실제로_BE_쓰기_또는_이미_담김_안내까지_가고_없으면_안_간다(
    case,
) -> None:
    fake = _fake()
    ctx, name, args = _ctx_for(case, fake)
    fake.calls.clear()

    intent = write_intent(ctx, name, args)
    call = asyncio.run(execute(ctx, fake, name, args))

    reached = bool(fake.writes) or (
        call.result.get("error") == ToolErrorCode.ALREADY_IN_CART.value
    )
    assert (intent is not None) == reached, call.result


# --- 그래프와 답변까지 ---


def _request(message: str):
    req = parse_request(
        {
            "user_id": UID,
            "message": message,
            "context_cards": [
                {"isbn": isbn, "rank": rank, "match_score": 90}
                for rank, isbn in enumerate(NEW, 1)
            ],
            "focused_isbn": None,
            "user_context": {"logged_in": True},
            "allow_tools": None,
            "idempotency_key": "idem_batch",
        }
    )
    assert req is not None
    return req


def _turn(replies, fake, message=MESSAGE_ALL):
    model = ScriptedModel(replies)
    run = asyncio.run(run_agent(_request(message), model=model, gateway=fake))
    return run, finish(run)


def _four_adds():
    return ai_calls(*[(ToolName.CART_ADD, {"book": card(n)}) for n in (1, 2, 3, 4)])


def test_27권에_4권을_담으라고_하면_BE에_쓰기를_보내지_않고_이유를_말한다() -> None:
    fake = _fake(filled=27)

    run, finished = _turn([_four_adds(), ai_text("네, 모두 담았어요.")], fake)

    assert fake.writes == []
    assert len(fake.cart_state(UID)) == 27
    errors = [c.result["error"] for c in run.tool_calls]
    assert errors == [TOGETHER, TOGETHER, TOGETHER, LIMIT]
    reply = finished.data.reply
    assert "장바구니가 가득 차서 4번 책을 담지 못했어요." in reply
    assert "함께 요청하신 일이 안 돼서 1번 책을 담지 않았어요." in reply
    assert BATCH_NOTE in reply
    # LLM 이 쓴 "모두 담았어요"는 사실이 아니라 지운다
    assert "모두 담았어요" not in reply
    assert finished.data.buttons == []


def test_막힌_뒤_LLM이_되는_책만_다시_불러도_이번_턴에는_담지_않는다() -> None:
    fake = _fake(filled=27)
    retry = ai_calls(*[(ToolName.CART_ADD, {"book": card(n)}) for n in (1, 2, 3)])

    run, finished = _turn([_four_adds(), retry, ai_text("담았어요.")], fake)

    assert fake.writes == []
    assert len(fake.cart_state(UID)) == 27
    assert [c.result["error"] for c in run.tool_calls[4:]] == [TOGETHER] * 3
    # 다시 불러도 같은 책의 결과는 한 번만 말한다(마지막 시도). LLM 이 쓴 "담았어요"는 지운다
    assert finished.data.reply.count("함께 요청하신 일이 안 돼서 1번 책을") == 1
    assert "담았어요" not in finished.data.reply


def test_자리가_맞는_묶음은_지금처럼_모두_담긴다() -> None:
    fake = _fake(filled=27)
    replies = [
        ai_calls(*[(ToolName.CART_ADD, {"book": card(n)}) for n in (1, 2, 3)]),
        ai_text("담았어요."),
    ]

    run, finished = _turn(replies, fake, "1번이랑 2번이랑 3번 담아줘")

    assert fake.writes == ["add_item"] * 3
    assert len(fake.cart_state(UID)) == 30
    assert all(c.result == {"ok": True} for c in run.tool_calls)
    assert BATCH_NOTE not in finished.data.reply


def test_묶음_기록의_책은_코드가_채운_ISBN이다() -> None:
    fake = _fake(filled=27)

    run, finished = _turn([_four_adds(), ai_text("네.")], fake)

    assert [c.arguments for c in run.tool_calls] == [
        {"isbn": isbn, "quantity": 1} for isbn in NEW
    ]
    assert finished.data.resolved_reference is not None
    assert finished.data.resolved_reference.isbns == NEW


def test_수량을_바꾸는_사이_다른_곳에서_바뀌었으면_바꾸지_않고_그렇게_말한다() -> None:
    class Racy(_Counting):
        """요청이 장바구니를 처음 읽은 직후에 사용자가 다른 화면에서 수량을 바꾼다."""

        reads = 0

        async def get_cart(self, *args, **kwargs):
            lines = await super().get_cart(*args, **kwargs)
            Racy.reads += 1
            if Racy.reads == 1:
                await self.set_quantity(UID, A, 5)
            return lines

    Racy.reads = 0
    racy = Racy(_books(), users=[UID])
    asyncio.run(racy.add_item(UID, A, 2))
    racy.calls.clear()
    replies = [
        ai_calls((ToolName.CART_UPDATE, {"book": cart(1), "quantity": 3})),
        ai_text("3권으로 바꿨어요."),
    ]

    run, finished = _turn(replies, racy, "장바구니 1번째 3권으로 바꿔줘")

    assert racy.cart_state(UID) == {A: 5}
    assert run.tool_calls[0].result == {
        "ok": False,
        "error": "CART_QUANTITY_CHANGED",
    }
    assert "그사이 《새책1》의 수량이 달라져서 바꾸지 않았어요." in finished.data.reply
    assert "바꿨어요" not in finished.data.reply


def _update_turn(fake, idempotency_key: str):
    req = _request("장바구니 1번째 2권으로 바꿔줘")
    req = req.model_copy(update={"idempotency_key": idempotency_key})
    replies = [
        ai_calls((ToolName.CART_UPDATE, {"book": cart(1), "quantity": 2})),
        ai_text("2권으로 바꿨어요."),
    ]
    return asyncio.run(run_agent(req, model=ScriptedModel(replies), gateway=fake))


def test_응답을_못_받아_같은_요청을_다시_처리해도_그사이_바꾼_수량을_덮어쓰지_않는다() -> (
    None
):
    fake = _fake(extra=[(A, 3)])
    _update_turn(fake, "idem_retry")  # 3권을 2권으로 바꿨지만 응답이 유실됐다
    asyncio.run(fake.set_quantity(UID, A, 5))  # 사용자가 다른 화면에서 5권으로 바꿨다

    retry = _update_turn(
        fake, "idem_retry"
    )  # 기록소가 없거나 504 라서 AI 가 다시 처리한다

    assert retry.tool_calls[0].result == {
        "ok": True
    }  # 이미 반영된 요청이라 성공으로 답한다
    assert fake.cart_state(UID) == {A: 5}
    # 두 번의 처리가 같은 키를 실었다. 가운데 None 은 사용자가 직접 바꾼 것(키 없음)이다
    key = f"idem_retry:cart.update:{A}:2"
    assert fake.keys == [key, None, key]


def test_키가_다른_새_요청은_사용자가_말한_대로_바꾼다() -> None:
    fake = _fake(extra=[(A, 3)])
    _update_turn(fake, "idem_first")
    asyncio.run(fake.set_quantity(UID, A, 5))

    _update_turn(fake, "idem_second")

    assert fake.cart_state(UID) == {A: 2}
