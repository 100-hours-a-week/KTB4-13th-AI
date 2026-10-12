"""⑦ 도구 실행(app/agent/tools.py) 테스트(#354). LLM 없이 가짜 BE(FakeCommerce)로 돈다.

여기서 지키려는 것: 잘못 고른 도구가 그대로 BE 에 실행되지 않는 것. 막히거나 BE 가 거절해도 예외 대신
결과에 이유가 남고, 막힌 호출은 BE 를 한 번도 부르지 않는다.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.agent.reference import TurnContext
from app.agent.schemas import ContextCard, ToolName
from app.agent.tools import ARG_MODELS, execute
from app.gateway.commerce import CommerceGateway
from app.gateway.commerce_fake import FakeBook, FakeCommerce

EXAMPLES = json.loads(
    (Path(__file__).parent / "data" / "agent_act_examples.json").read_text(
        encoding="utf-8"
    )
)

UID = 1
ISBNS = [f"97889{n:08d}" for n in range(1, 8)]
A, B, C, D, E, F, G = ISBNS


def _books() -> list[FakeBook]:
    books = [
        FakeBook(isbn, f"책{i}", 10000 + i, 100) for i, isbn in enumerate(ISBNS, 1)
    ]
    books[5].stock = 3  # F: 재고가 적다
    books[6].stock = 0  # G: 품절
    return books


class _Spy:
    """FakeCommerce 를 감싸 BE 호출을 센다. 막힌 호출이 BE 까지 갔는지 본다."""

    def __init__(self, inner: FakeCommerce) -> None:
        self.inner = inner
        self.calls: list[str] = []

    async def get_cart(self, user_id):
        self.calls.append("get_cart")
        return await self.inner.get_cart(user_id)

    async def add_item(self, user_id, isbn, quantity):
        self.calls.append("add_item")
        return await self.inner.add_item(user_id, isbn, quantity)

    async def set_quantity(self, user_id, isbn, quantity):
        self.calls.append("set_quantity")
        return await self.inner.set_quantity(user_id, isbn, quantity)

    async def remove_item(self, user_id, isbn):
        self.calls.append("remove_item")
        return await self.inner.remove_item(user_id, isbn)

    async def get_offers(self, isbns):
        self.calls.append("get_offers")
        return await self.inner.get_offers(isbns)


def _setup(seed=(), **ctx_overrides):
    """(감시 래퍼, 가짜 BE, 요청 상태). seed 는 먼저 담을 (isbn, 수량)이고 앞의 것이 먼저 담긴다."""
    fake = FakeCommerce(_books(), users=[UID])
    for isbn, qty in seed:
        asyncio.run(fake.add_item(UID, isbn, qty))
    values = {
        "user_id": UID,
        "logged_in": True,
        "cards": [
            ContextCard(isbn=A, rank=1, match_score=92),
            ContextCard(isbn=B, rank=2, match_score=87),
            ContextCard(isbn=C, rank=3, match_score=81),
        ],
        "focused_isbn": None,
        "allow_tools": None,
    }
    values.update(ctx_overrides)
    return _Spy(fake), fake, TurnContext(**values)


def _run(ctx, spy, name, args):
    return asyncio.run(execute(ctx, spy, name, args))


def card(n: int) -> dict:
    return {"source": "card", "n": n}


def cart(n: int) -> dict:
    return {"source": "cart", "n": n}


# --- 조회 ---


def test_장바구니_조회는_BE_순서대로_돌려주고_번호를_풀_목록으로_기억한다() -> None:
    spy, _fake, ctx = _setup(seed=[(A, 1), (B, 2)])

    call = _run(ctx, spy, ToolName.CART_VIEW, {})

    assert call.name == ToolName.CART_VIEW
    assert call.arguments == {}
    assert [item["isbn"] for item in call.result["items"]] == [
        B,
        A,
    ]  # 나중에 담은 것이 위
    assert call.result["items"][0] == {
        "isbn": B,
        "title": "책2",
        "price": 10002,
        "quantity": 2,
        "available": True,
    }
    assert [line.isbn for line in ctx.cart_snapshot] == [B, A]
    assert call.grounded is True


def test_빈_장바구니도_목록으로_기억한다() -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.CART_VIEW, {})

    assert call.result == {"items": [], "total": 0}
    assert ctx.cart_snapshot == []


def test_조회_결과의_합계는_구매할_수_있는_책만_더한_값이다() -> None:
    spy, _fake, ctx = _setup(seed=[(A, 2), (B, 1)])

    call = _run(ctx, spy, ToolName.CART_VIEW, {})

    assert call.result["total"] == 10001 * 2 + 10002


def test_재고가_모자라거나_판매가_중지된_책은_합계에서_뺀다() -> None:
    spy, fake, ctx = _setup(seed=[(A, 2), (B, 1), (C, 1)])
    fake.set_stock(B, 0)  # 담은 수량보다 재고가 적다
    fake.discontinue(C)  # 판매 중지라 가격이 없다

    call = _run(ctx, spy, ToolName.CART_VIEW, {})

    assert [(i["isbn"], i["available"]) for i in call.result["items"]] == [
        (C, False),
        (B, False),
        (A, True),
    ]
    assert call.result["items"][0]["price"] is None
    assert call.result["total"] == 10001 * 2  # 판매 중지 책이 들어간 합계가 아니다


def test_조회는_인자를_받지_않는다() -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.CART_VIEW, {"user_id": 2})

    assert call.result == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert spy.calls == []
    assert ctx.cart_snapshot is None


# --- 담기 ---


def test_카드_번호로_담으면_코드가_채운_ISBN으로_기록한다() -> None:
    spy, fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {"ok": True}
    assert call.arguments == {
        "isbn": A,
        "quantity": 1,
    }  # LLM 이 준 것이 아니라 코드가 채운 값
    assert fake.cart_state(UID) == {A: 1}


def test_수량을_주면_그_수량으로_담는다() -> None:
    spy, fake, ctx = _setup()

    _run(ctx, spy, ToolName.CART_ADD, {"book": card(3), "quantity": 2})

    assert fake.cart_state(UID) == {C: 2}


def test_지금_보는_책으로_담는다() -> None:
    spy, fake, ctx = _setup(focused_isbn=D)

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": {"source": "focused"}})

    assert call.result == {"ok": True}
    assert fake.cart_state(UID) == {D: 1}


def test_이미_담긴_책은_담지_않고_알린다() -> None:
    # BE 의 담기는 "요청 수량으로 바꾸기"라서 2권 담긴 책을 담으면 1권이 된다
    spy, fake, ctx = _setup(seed=[(A, 2)])

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {
        "ok": False,
        "error": "ALREADY_IN_CART",
        "title": "책1",
        "quantity": 2,
    }
    assert fake.cart_state(UID) == {A: 2}
    assert "add_item" not in spy.calls


def test_조회를_건너뛰고_바로_담으라고_해도_이미_담긴_책은_막힌다() -> None:
    # 확인은 LLM 이 조회했는지와 상관없이 담기 안에서 BE 를 다시 읽어 한다
    spy, fake, ctx = _setup(seed=[(B, 3)])
    assert ctx.cart_snapshot is None

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(2), "quantity": 1})

    assert call.result["error"] == "ALREADY_IN_CART"
    assert fake.cart_state(UID) == {B: 3}


def test_삭제한_책은_이미_담긴_책이_아니다() -> None:
    spy, fake, ctx = _setup(seed=[(A, 2)])
    asyncio.run(fake.remove_item(UID, A))

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {"ok": True}
    assert fake.cart_state(UID) == {A: 1}


@pytest.mark.parametrize(
    ("quantity", "error"),
    [
        pytest.param(0, "INVALID_CART_ITEM_QUANTITY", id="0권"),
        pytest.param(501, "INVALID_CART_ITEM_QUANTITY", id="501권"),
        pytest.param(-1, "INVALID_CART_ITEM_QUANTITY", id="음수"),
    ],
)
def test_수량_범위는_BE가_거절하고_결과에_이유가_남는다(quantity, error) -> None:
    spy, fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1), "quantity": quantity})

    assert call.result == {"ok": False, "error": error}
    assert call.arguments == {
        "isbn": A,
        "quantity": quantity,
    }  # 책은 풀었으므로 ISBN 이 남는다
    assert fake.cart_state(UID) == {}


def test_재고보다_많이_담으면_BE가_거절한다() -> None:
    spy, fake, ctx = _setup(cards=[ContextCard(isbn=F, rank=1, match_score=90)])

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1), "quantity": 5})

    assert call.result == {"ok": False, "error": "INSUFFICIENT_PRODUCT_STOCK"}
    assert fake.cart_state(UID) == {}


def test_품절_책은_BE가_거절한다() -> None:
    spy, fake, ctx = _setup(cards=[ContextCard(isbn=G, rank=1, match_score=90)])

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {"ok": False, "error": "INSUFFICIENT_PRODUCT_STOCK"}
    assert fake.cart_state(UID) == {}


# --- 수량 변경 ---


def test_장바구니_번호로_수량을_바꾼다() -> None:
    spy, fake, ctx = _setup(seed=[(B, 3), (D, 2)])  # 목록은 [D, B]
    _run(ctx, spy, ToolName.CART_VIEW, {})

    call = _run(ctx, spy, ToolName.CART_UPDATE, {"book": cart(2), "quantity": 1})

    assert call.result == {"ok": True}
    assert call.arguments == {"isbn": B, "quantity": 1}
    assert fake.cart_state(UID) == {B: 1, D: 2}


def test_카드로_가리킨_책의_수량도_바꾼다() -> None:
    spy, fake, ctx = _setup(seed=[(A, 1)])

    _run(ctx, spy, ToolName.CART_UPDATE, {"book": card(1), "quantity": 3})

    assert fake.cart_state(UID) == {A: 3}


def test_담기지_않은_책의_수량은_바꾸지_못한다() -> None:
    spy, fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.CART_UPDATE, {"book": card(1), "quantity": 2})

    assert call.result == {"ok": False, "error": "CART_ITEM_NOT_FOUND"}
    assert fake.cart_state(UID) == {}


def test_수량_0은_삭제가_아니라_거절이다() -> None:
    spy, fake, ctx = _setup(seed=[(A, 2)])

    call = _run(ctx, spy, ToolName.CART_UPDATE, {"book": card(1), "quantity": 0})

    assert call.result == {"ok": False, "error": "INVALID_CART_ITEM_QUANTITY"}
    assert fake.cart_state(UID) == {A: 2}


def test_수량_변경은_수량이_있어야_한다() -> None:
    spy, _fake, ctx = _setup(seed=[(A, 2)])

    call = _run(ctx, spy, ToolName.CART_UPDATE, {"book": card(1)})

    assert call.result == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert spy.calls == []


def test_재고보다_크게_바꾸면_BE가_거절하고_수량은_그대로다() -> None:
    spy, fake, ctx = _setup(
        seed=[(F, 2)], cards=[ContextCard(isbn=F, rank=1, match_score=9)]
    )

    call = _run(ctx, spy, ToolName.CART_UPDATE, {"book": card(1), "quantity": 4})

    assert call.result == {"ok": False, "error": "INSUFFICIENT_PRODUCT_STOCK"}
    assert fake.cart_state(UID) == {F: 2}


# --- 빼기 ---


def test_장바구니_맨_위는_가장_나중에_담은_책이다() -> None:
    spy, fake, ctx = _setup(seed=[(A, 1), (B, 1), (C, 1)])
    _run(ctx, spy, ToolName.CART_VIEW, {})

    call = _run(ctx, spy, ToolName.CART_REMOVE, {"book": cart(1)})

    assert call.result == {"ok": True}
    assert call.arguments == {"isbn": C}
    assert fake.cart_state(UID) == {A: 1, B: 1}


def test_담기지_않은_책을_빼면_BE가_거절한다() -> None:
    spy, fake, ctx = _setup(seed=[(B, 1)])

    call = _run(ctx, spy, ToolName.CART_REMOVE, {"book": card(1)})

    assert call.result == {"ok": False, "error": "CART_ITEM_NOT_FOUND"}
    assert fake.cart_state(UID) == {B: 1}


# --- 가격·재고 ---


def test_가격과_재고를_조회한다() -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.BOOK_OFFER, {"books": [card(1), card(3)]})

    assert call.arguments == {"isbns": [A, C]}
    assert call.result == {
        "offers": [
            {"isbn": A, "title": "책1", "price": 10001, "in_stock": True},
            {"isbn": C, "title": "책3", "price": 10003, "in_stock": True},
        ]
    }


def test_같은_책을_두_번_가리켜도_한_번만_묻고_순서를_지킨다() -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.BOOK_OFFER, {"books": [card(2), card(1), card(2)]})

    assert call.arguments == {"isbns": [B, A]}
    assert [o["isbn"] for o in call.result["offers"]] == [B, A]


def test_품절_책은_재고_없음으로_나온다() -> None:
    spy, _fake, ctx = _setup(cards=[ContextCard(isbn=G, rank=1, match_score=9)])

    call = _run(ctx, spy, ToolName.BOOK_OFFER, {"books": [card(1)]})

    assert call.result["offers"][0]["in_stock"] is False


def test_가격_조회는_하나라도_가리킬_수_없으면_통째로_실행하지_않는다() -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.BOOK_OFFER, {"books": [card(1), card(9)]})

    assert call.result == {"ok": False, "error": "BOOK_NOT_IN_CARDS"}
    assert spy.calls == []


@pytest.mark.parametrize("books", [[], [card(1)] * 11], ids=["빈_목록", "11개"])
def test_가격_조회는_1개에서_10개까지만_받는다(books) -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.BOOK_OFFER, {"books": books})

    assert call.result == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert spy.calls == []


# --- 한 번에 부른 도구들 ---


def test_한_번에_부른_도구들은_같은_목록_기준으로_번호를_푼다() -> None:
    # "1번 담고 장바구니 1번째 빼줘": 담기가 먼저 실행돼도 장바구니 1번은 LLM 이 본 목록의 1번이다
    spy, fake, ctx = _setup(seed=[(C, 1)])
    _run(ctx, spy, ToolName.CART_VIEW, {})  # 목록: [C]

    added = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})
    removed = _run(ctx, spy, ToolName.CART_REMOVE, {"book": cart(1)})

    assert added.result == {"ok": True}
    assert removed.result == {"ok": True}
    assert removed.arguments == {"isbn": C}  # 방금 담은 A 가 아니다
    assert fake.cart_state(UID) == {A: 1}


def test_다시_조회하면_번호가_새_목록_기준으로_바뀐다() -> None:
    spy, _fake, ctx = _setup(seed=[(C, 1)])
    _run(ctx, spy, ToolName.CART_VIEW, {})
    _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})
    assert ctx.cart_snapshot[0].isbn == C  # 아직 낡은 목록

    _run(ctx, spy, ToolName.CART_VIEW, {})

    assert [line.isbn for line in ctx.cart_snapshot] == [A, C]


# --- 책을 가리킬 수 없으면 실행하지 않는다 ---


@pytest.mark.parametrize(
    ("name", "args", "error"),
    [
        pytest.param(
            ToolName.CART_ADD, {"book": card(9)}, "BOOK_NOT_IN_CARDS", id="없는_카드"
        ),
        pytest.param(
            ToolName.CART_ADD,
            {"book": cart(1)},
            "CART_NOT_LOADED",
            id="조회_전_장바구니_번호",
        ),
        pytest.param(
            ToolName.CART_REMOVE,
            {"book": cart(1)},
            "CART_NOT_LOADED",
            id="조회_전_빼기",
        ),
        pytest.param(
            ToolName.CART_UPDATE,
            {"book": {"source": "focused"}, "quantity": 2},
            "NO_FOCUSED_BOOK",
            id="지목한_책_없음",
        ),
    ],
)
def test_책을_가리킬_수_없으면_BE를_부르지_않는다(name, args, error) -> None:
    spy, fake, ctx = _setup()

    call = _run(ctx, spy, name, args)

    assert call.result == {"ok": False, "error": error}
    assert call.arguments == args  # 풀기 전이라 LLM 이 준 그대로 남긴다
    assert spy.calls == []
    assert fake.cart_state(UID) == {}


def test_같은_번호에_다른_책이_둘이면_실행하지_않는다() -> None:
    spy, _fake, ctx = _setup(
        cards=[
            ContextCard(isbn=A, rank=1, match_score=9),
            ContextCard(isbn=B, rank=1, match_score=8),
        ]
    )

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {"ok": False, "error": "AMBIGUOUS_BOOK"}
    assert spy.calls == []


# --- LLM 이 지어낸 값은 BE 까지 가지 않는다 ---


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"book": card(1), "isbn": "9788900009999"}, id="최상위에_isbn"),
        pytest.param({"book": card(1), "user_id": 2}, id="최상위에_user_id"),
        pytest.param(
            {"book": {**card(1), "isbn": "9788900009999"}}, id="book_안에_isbn"
        ),
        pytest.param({"isbn": A}, id="book_대신_isbn"),
        pytest.param({"book": A}, id="book이_문자열"),
        pytest.param({"book": card(1), "quantity": "2"}, id="수량_문자열"),
        pytest.param({"book": card(1), "quantity": 1.5}, id="수량_실수"),
        pytest.param({"book": card(1), "quantity": True}, id="수량_불리언"),
        pytest.param({}, id="인자_없음"),
        pytest.param(None, id="인자가_null"),
        pytest.param("book=1", id="인자가_문자열"),
    ],
)
def test_모르는_칸이나_잘못된_자료형은_BE를_부르지_않는다(args) -> None:
    spy, fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.CART_ADD, args)

    assert call.result == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert spy.calls == []
    assert fake.cart_state(UID) == {}


def test_다른_사용자_번호는_요청의_값으로만_쓴다() -> None:
    # 도구는 user_id 를 인자로 받지 않는다. 항상 요청의 값이 BE 로 간다
    _spy, _fake, ctx = _setup()
    fake2 = FakeCommerce(_books(), users=[UID, 2])
    spy2 = _Spy(fake2)

    _run(ctx, spy2, ToolName.CART_ADD, {"book": card(1)})

    assert fake2.cart_state(UID) == {A: 1}
    assert fake2.cart_state(2) == {}


# --- 막는 규칙은 BE 를 한 번도 부르지 않는다 ---


@pytest.mark.parametrize(
    ("name", "args"),
    [
        pytest.param(ToolName.CART_ADD, {"book": card(1)}, id="담기"),
        pytest.param(
            ToolName.CART_UPDATE, {"book": card(1), "quantity": 2}, id="수량_변경"
        ),
        pytest.param(ToolName.CART_REMOVE, {"book": card(1)}, id="빼기"),
    ],
)
def test_로그인하지_않았으면_쓰기는_BE를_부르지_않는다(name, args) -> None:
    spy, fake, ctx = _setup(seed=[(A, 1)], logged_in=False)

    call = _run(ctx, spy, name, args)

    assert call.result == {"ok": False, "error": "LOGIN_REQUIRED"}
    assert spy.calls == []
    assert fake.cart_state(UID) == {A: 1}


def test_로그인하지_않아도_읽기는_된다() -> None:
    spy, _fake, ctx = _setup(logged_in=False)

    assert "items" in _run(ctx, spy, ToolName.CART_VIEW, {}).result
    assert "offers" in _run(ctx, spy, ToolName.BOOK_OFFER, {"books": [card(1)]}).result


def test_허용하지_않은_도구는_BE를_부르지_않는다() -> None:
    spy, fake, ctx = _setup(allow_tools=[ToolName.CART_VIEW])

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {"ok": False, "error": "TOOL_NOT_ALLOWED"}
    assert spy.calls == []
    assert fake.cart_state(UID) == {}


def test_로그인하지_않은_요청의_잘못된_인자는_로그인_이유로만_답한다() -> None:
    spy, _fake, ctx = _setup(logged_in=False)

    call = _run(ctx, spy, ToolName.CART_ADD, {"isbn": "x"})

    assert call.result == {"ok": False, "error": "LOGIN_REQUIRED"}


# --- 아직 만들지 않은 도구 ---


@pytest.mark.parametrize("name", [ToolName.SELECTION_PICK, ToolName.BOOK_COMPARE])
def test_뒤_단계에서_만들_도구는_아직_실행하지_않는다(name) -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, name, {})

    assert call.result == {"ok": False, "error": "TOOL_NOT_IMPLEMENTED"}
    assert spy.calls == []


def test_도구_이름은_구현했거나_아직_안_만든_것_둘_중_하나다() -> None:
    # 도구를 새로 만들면서 인자 모델이나 실행기를 빠뜨리지 않게
    from app.agent.tools import _HANDLERS

    assert set(ARG_MODELS) == set(_HANDLERS)
    assert set(_HANDLERS) | {
        ToolName.SELECTION_PICK,
        ToolName.BOOK_COMPARE,
    } == set(ToolName)


# --- 기록의 모양 ---


def test_결과는_JSON으로_내보낼_수_있다() -> None:
    spy, _fake, ctx = _setup(seed=[(A, 2)])
    calls = [
        _run(ctx, spy, ToolName.CART_VIEW, {}),
        _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)}),
        _run(ctx, spy, ToolName.BOOK_OFFER, {"books": [card(2)]}),
        _run(ctx, spy, ToolName.CART_ADD, {"isbn": "x"}),
    ]

    for call in calls:
        json.dumps(call.model_dump(mode="json"), ensure_ascii=False)


def test_실패한_결과에는_책_제목과_사용자_발화가_없다() -> None:
    # 로그에 남아도 안전하게 코드 이름만 적는다(이미 담긴 책은 알리려고 제목과 수량을 싣는다)
    spy, _fake, ctx = _setup(logged_in=False)

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert set(call.result) == {"ok", "error"}


def test_조회와_담기_결과는_명세_예시와_같은_칸을_가진다() -> None:
    example = EXAMPLES["responses"]["add"]["data"]["tool_calls"]
    spec_view = example[0]["result"]["items"][0]
    spec_add = example[1]
    spy, _fake, ctx = _setup(seed=[(B, 1)])

    view = _run(ctx, spy, ToolName.CART_VIEW, {})
    add = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert set(view.result["items"][0]) == set(spec_view)
    assert set(add.arguments) == set(spec_add["arguments"])
    assert add.result == spec_add["result"]


def test_FakeCommerce는_도구가_기대하는_BE_모양이다() -> None:
    assert isinstance(FakeCommerce([], users=[]), CommerceGateway)


# --- 화면 안내(ui.guide) ---


@pytest.mark.parametrize(
    ("target", "label"),
    [("ORDER-LIST-001", "주문내역"), ("CART-001", "장바구니 보기")],
)
def test_화면_안내는_BE를_부르지_않고_버튼_글자를_돌려준다(target, label) -> None:
    spy, fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.UI_GUIDE, {"target": target})

    assert call.result == {"ok": True, "label": label}
    assert call.arguments == {"target": target}
    assert spy.calls == []
    assert fake.cart_state(UID) == {}


def test_화면_안내는_로그인하지_않아도_된다() -> None:
    # 장바구니를 바꾸는 도구가 아니라 이동 버튼만 붙인다
    spy, _fake, ctx = _setup(logged_in=False)

    call = _run(ctx, spy, ToolName.UI_GUIDE, {"target": "ORDER-LIST-001"})

    assert call.result["ok"] is True


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"target": "HOME-001"}, id="목록에_없는_화면"),
        pytest.param({"target": "https://evil.example"}, id="바깥_주소"),
        pytest.param({}, id="화면_없음"),
        pytest.param({"target": "CART-001", "isbn": A}, id="모르는_칸"),
        pytest.param({"target": ["CART-001"]}, id="목록"),
    ],
)
def test_목록에_없는_화면이나_모르는_칸은_안내하지_않는다(args) -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.UI_GUIDE, args)

    assert call.result == {"ok": False, "error": "INVALID_ARGUMENTS"}


def test_허용하지_않은_화면_안내도_막는다() -> None:
    spy, _fake, ctx = _setup(allow_tools=[ToolName.CART_VIEW])

    call = _run(ctx, spy, ToolName.UI_GUIDE, {"target": "CART-001"})

    assert call.result == {"ok": False, "error": "TOOL_NOT_ALLOWED"}
