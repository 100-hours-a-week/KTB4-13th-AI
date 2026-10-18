"""⑦ 도구 실행(app/agent/tools.py) 테스트(#354). LLM 없이 가짜 BE(FakeCommerce)로 돈다.

여기서 지키려는 것: 잘못 고른 도구가 그대로 BE 에 실행되지 않는 것. 막히거나 BE 가 거절해도 예외 대신
결과에 이유가 남고, 막힌 호출은 BE 를 한 번도 부르지 않는다.
"""

import asyncio
import json
from pathlib import Path

import pytest

from app.agent.compare import CompareError
from app.agent.errors import ToolErrorCode
from app.agent.reference import TurnContext
from app.agent.schemas import ContextCard, ToolCall, ToolName, Turn
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


def _run(ctx, spy, name, args, comparer=None):
    return asyncio.run(execute(ctx, spy, name, args, comparer=comparer))


def _see_cart(ctx, spy) -> ToolCall:
    """장바구니 조회만 한 묶음. 결과를 LLM 에게 돌려주는 시점까지 흉내 낸다."""
    call = _run(ctx, spy, ToolName.CART_VIEW, {})
    ctx.commit_cart_view()
    return call


def card(n: int) -> dict:
    return {"source": "card", "n": n}


def cart(n: int) -> dict:
    return {"source": "cart", "n": n}


# --- 조회 ---


def test_장바구니_조회는_BE_순서대로_돌려주고_묶음이_끝나면_번호를_풀_목록으로_기억한다() -> (
    None
):
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
    assert ctx.cart_snapshot is None  # 아직 LLM 이 받지 못했다
    ctx.commit_cart_view()
    assert [line.isbn for line in ctx.cart_snapshot] == [B, A]
    assert call.grounded is True


def test_빈_장바구니도_목록으로_기억한다() -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.CART_VIEW, {})

    assert call.result == {"items": [], "total": 0}
    ctx.commit_cart_view()
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
    assert ctx.pending_cart is None
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
    _see_cart(ctx, spy)

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
    _see_cart(ctx, spy)

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
    _see_cart(ctx, spy)  # 목록: [C]

    added = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})
    removed = _run(ctx, spy, ToolName.CART_REMOVE, {"book": cart(1)})

    assert added.result == {"ok": True}
    assert removed.result == {"ok": True}
    assert removed.arguments == {"isbn": C}  # 방금 담은 A 가 아니다
    assert fake.cart_state(UID) == {A: 1}


def test_다시_조회하면_묶음이_끝난_뒤부터_번호가_새_목록_기준으로_바뀐다() -> None:
    spy, _fake, ctx = _setup(seed=[(C, 1)])
    _see_cart(ctx, spy)
    _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})
    assert ctx.cart_snapshot[0].isbn == C  # 아직 낡은 목록

    _run(ctx, spy, ToolName.CART_VIEW, {})
    assert ctx.cart_snapshot[0].isbn == C  # 조회했어도 LLM 이 받기 전이라 그대로다

    ctx.commit_cart_view()
    assert [line.isbn for line in ctx.cart_snapshot] == [A, C]
    assert ctx.pending_cart is None


def test_조회와_같은_묶음의_장바구니_번호는_본_적_없는_목록으로_풀지_않는다() -> None:
    # 장바구니를 한 번도 못 본 LLM 이 조회와 "장바구니 2번째 빼기"를 같이 불렀다
    spy, fake, ctx = _setup(seed=[(A, 1), (B, 1), (C, 1)])

    viewed = _run(ctx, spy, ToolName.CART_VIEW, {})
    removed = _run(ctx, spy, ToolName.CART_REMOVE, {"book": cart(2)})

    assert viewed.result["items"]
    assert removed.result == {"ok": False, "error": "CART_NOT_LOADED"}
    assert fake.cart_state(UID) == {A: 1, B: 1, C: 1}


def test_같은_묶음에서_담고_다시_조회해도_번호는_앞서_본_목록으로_푼다() -> None:
    # 앞 묶음에서 본 목록은 [A]. 이번 묶음의 조회가 [B, A]를 돌려줘도 "1번째"는 LLM 이 본 A 다
    spy, fake, ctx = _setup(seed=[(A, 1)])
    _see_cart(ctx, spy)

    _run(ctx, spy, ToolName.CART_ADD, {"book": card(2)})
    _run(ctx, spy, ToolName.CART_VIEW, {})
    removed = _run(ctx, spy, ToolName.CART_REMOVE, {"book": cart(1)})

    assert removed.result == {"ok": True}
    assert removed.arguments == {"isbn": A}  # 방금 담은 B 가 아니다
    assert fake.cart_state(UID) == {B: 1}


def test_조회하지_않은_묶음이_끝나도_본_목록은_그대로다() -> None:
    spy, _fake, ctx = _setup(seed=[(A, 1)])
    _see_cart(ctx, spy)
    seen = ctx.cart_snapshot

    ctx.commit_cart_view()

    assert ctx.cart_snapshot is seen


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


# --- 조합 고르기(selection.pick) ---


def _pick_setup(offers, *, logged_in=True, allow_tools=None):
    """(감시 래퍼, 가짜 BE, 요청 상태). offers 는 카드 1번부터의 (가격, 점수, 재고)이다."""
    books = [
        FakeBook(isbn, f"책{i}", price, stock)
        for i, (isbn, (price, _score, stock)) in enumerate(zip(ISBNS, offers), 1)
    ]
    fake = FakeCommerce(books, users=[UID])
    cards = [
        ContextCard(isbn=isbn, rank=i, match_score=score)
        for i, (isbn, (_price, score, _stock)) in enumerate(zip(ISBNS, offers), 1)
    ]
    ctx = TurnContext(
        user_id=UID,
        logged_in=logged_in,
        cards=cards,
        focused_isbn=None,
        allow_tools=allow_tools,
    )
    return _Spy(fake), fake, ctx


# (가격, 점수, 재고). 1+2 는 예산 30,000원을 넘고 1+3 이 점수 합이 가장 크다
THREE = [(20000, 92, 5), (15000, 87, 5), (10000, 81, 5)]


def test_예산_안에서_점수_합이_가장_큰_조합을_코드가_고른다() -> None:
    spy, fake, ctx = _pick_setup(THREE)

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    assert call.arguments == {"budget_total": 30000, "count": 2}
    assert call.result["ok"] is True
    assert [b["isbn"] for b in call.result["chosen"]] == [A, C]
    assert call.result["chosen"][0] == {
        "isbn": A,
        "title": "책1",
        "price": 20000,
        "match_score": 92,
    }
    assert call.result["total_price"] == 30000
    assert call.result["alternatives_considered"] == 3  # 3권 중 2권 = 3가지
    assert ctx.picked == {A, C}
    assert fake.cart_state(UID) == {}  # 장바구니는 건드리지 않는다


def test_가격과_재고는_LLM이_아니라_BE에서_직접_읽는다() -> None:
    spy, _fake, ctx = _pick_setup(THREE)

    _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    assert spy.calls == ["get_cart", "get_offers"]  # 읽기뿐이다. 담지 않는다


def test_이미_장바구니에_있는_책은_고르지_않는다() -> None:
    # 확인 버튼으로 담으면 BE 가 수량을 바꾸므로(2권이 1권이 됨) 이미 담긴 책은 후보가 아니다
    spy, fake, ctx = _pick_setup(THREE)
    asyncio.run(fake.add_item(UID, A, 2))

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    assert [b["isbn"] for b in call.result["chosen"]] == [B, C]  # 1번이 담겨 있어 2+3
    assert call.result["alternatives_considered"] == 1
    assert fake.cart_state(UID) == {A: 2}


def test_카드가_모두_장바구니에_있으면_고를_조합이_없다() -> None:
    spy, fake, ctx = _pick_setup(THREE)
    for isbn in (A, B, C):
        asyncio.run(fake.add_item(UID, isbn, 1))

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 99000, "count": 1})

    assert call.result["error"] == "NO_COMBINATION"


def test_품절_책은_점수가_높아도_고르지_않는다() -> None:
    spy, _fake, ctx = _pick_setup([(10000, 99, 0), (12000, 80, 5), (13000, 70, 5)])

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    assert [b["isbn"] for b in call.result["chosen"]] == [B, C]
    assert call.result["alternatives_considered"] == 1  # 재고 있는 2권 중 2권


def test_판매_중지한_책과_BE가_모르는_책도_고르지_않는다() -> None:
    spy, fake, ctx = _pick_setup([(10000, 99, 5), (12000, 80, 5), (13000, 70, 5)])
    fake.discontinue(A)
    ctx.cards.append(ContextCard(isbn="9780000000000", rank=4, match_score=100))

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    assert [b["isbn"] for b in call.result["chosen"]] == [B, C]


def test_예산_안에_드는_조합이_없으면_이유를_남기고_아무것도_고르지_않는다() -> None:
    spy, fake, ctx = _pick_setup(THREE)

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 10000, "count": 2})

    assert call.result == {
        "ok": False,
        "error": "NO_COMBINATION",
        "alternatives_considered": 3,
    }
    assert ctx.picked == set()
    assert fake.cart_state(UID) == {}


def test_카드가_없으면_고를_조합이_없다() -> None:
    spy, _fake, ctx = _setup(cards=[])

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 50000, "count": 1})

    assert call.result["error"] == "NO_COMBINATION"
    assert call.result["alternatives_considered"] == 0


@pytest.mark.parametrize(
    "args",
    [
        {"budget_total": 0, "count": 2},
        {"budget_total": -1, "count": 2},
        {"budget_total": 30000, "count": 0},
        {"budget_total": 30000, "count": 11},  # 카드는 10장까지다
        {"budget_total": "30000", "count": 2},
        {"budget_total": 30000},
        {
            "budget_total": 30000,
            "count": 2,
            "isbns": [A],
        },  # LLM 이 책을 정해 줄 수 없다
    ],
)
def test_잘못된_인자는_BE를_부르지_않고_막는다(args) -> None:
    spy, _fake, ctx = _pick_setup(THREE)

    call = _run(ctx, spy, ToolName.SELECTION_PICK, args)

    assert call.result == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert spy.calls == []


def test_로그인하지_않아도_고르기는_된다() -> None:
    # 고르기는 읽기다. 담기만 로그인이 필요하다
    spy, _fake, ctx = _pick_setup(THREE, logged_in=False)

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    assert call.result["ok"] is True


def test_허용하지_않은_요청이면_고르지_않는다() -> None:
    spy, _fake, ctx = _pick_setup(THREE, allow_tools=[ToolName.CART_VIEW])

    call = _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    assert call.result == {"ok": False, "error": "TOOL_NOT_ALLOWED"}
    assert spy.calls == []


# --- AI 가 고른 책은 확인 없이 담지 않는다 ---


def test_고른_책을_담으려_하면_BE를_부르지_않고_확인이_필요하다고_막는다() -> None:
    spy, fake, ctx = _pick_setup(THREE)
    _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})
    spy.calls.clear()

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {"ok": False, "error": "CONFIRMATION_REQUIRED"}
    assert call.arguments == {"isbn": A, "quantity": 1}
    assert spy.calls == []  # 조회도 담기도 하지 않았다
    assert fake.cart_state(UID) == {}


def test_지목한_책으로_담으려_해도_고른_책이면_막는다() -> None:
    spy, fake, ctx = _pick_setup(THREE)
    ctx.focused_isbn = C
    _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": {"source": "focused"}})

    assert call.result["error"] == "CONFIRMATION_REQUIRED"
    assert fake.cart_state(UID) == {}


def test_고르지_않은_책은_사용자가_말한_대로_담긴다() -> None:
    spy, fake, ctx = _pick_setup(THREE)
    _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(2)})  # 2번은 고르지 않았다

    assert call.result == {"ok": True}
    assert fake.cart_state(UID) == {B: 1}


def test_조합을_못_골랐으면_담기가_막히지_않는다() -> None:
    spy, fake, ctx = _pick_setup(THREE)
    _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 1000, "count": 2})

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {"ok": True}
    assert fake.cart_state(UID) == {A: 1}


def test_조합을_고르기_전에_담은_것은_그대로다() -> None:
    spy, fake, ctx = _pick_setup(THREE)
    _run(ctx, spy, ToolName.CART_ADD, {"book": card(2)})

    _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})

    assert fake.cart_state(UID) == {B: 1}


def test_고른_책은_다음_요청으로_이어지지_않는다() -> None:
    # 확인은 요청마다 새로 받는다. 요청 상태(TurnContext)는 요청이 만든다
    spy, _fake, ctx = _pick_setup(THREE)
    _run(ctx, spy, ToolName.SELECTION_PICK, {"budget_total": 30000, "count": 2})
    next_turn = TurnContext(
        user_id=UID,
        logged_in=True,
        cards=ctx.cards,
        focused_isbn=None,
        allow_tools=None,
    )

    assert next_turn.picked == set()


# --- 비교(book.compare) ---


class _FakeComparer:
    """소개글을 읽는 부품의 가짜. 받은 ISBN 을 기록하고 미리 정한 요약이나 오류를 돌려준다."""

    def __init__(
        self, summary: str = "1번은 입문용이고 2번은 실무용이에요.", error=None
    ):
        self.summary = summary
        self.error = error
        self.calls: list[list[str]] = []

    async def compare(self, isbns):
        self.calls.append(list(isbns))
        if self.error is not None:
            raise CompareError(self.error)
        return self.summary


def test_비교는_코드가_채운_ISBN으로_요약만_받아_결과에_남긴다() -> None:
    spy, _fake, ctx = _setup()
    comparer = _FakeComparer()

    call = _run(
        ctx, spy, ToolName.BOOK_COMPARE, {"books": [card(1), card(3)]}, comparer
    )

    assert comparer.calls == [[A, C]]
    assert call.arguments == {"isbns": [A, C]}
    assert call.result == {
        "ok": True,
        "basis": "description",
        "summary": "1번은 입문용이고 2번은 실무용이에요.",
    }
    assert spy.calls == []  # 비교는 BE 를 부르지 않는다(가격은 book.offer 가 읽는다)


def test_장바구니_번호로도_비교할_수_있다() -> None:
    spy, _fake, ctx = _setup(seed=[(A, 1), (B, 1)])
    comparer = _FakeComparer()
    _see_cart(ctx, spy)  # 나중에 담은 B 가 1번째

    _run(ctx, spy, ToolName.BOOK_COMPARE, {"books": [cart(1), cart(2)]}, comparer)

    assert comparer.calls == [[B, A]]


def test_같은_책을_두_번_가리키면_비교하지_않는다() -> None:
    spy, _fake, ctx = _setup()
    comparer = _FakeComparer()

    call = _run(
        ctx, spy, ToolName.BOOK_COMPARE, {"books": [card(1), card(1)]}, comparer
    )

    assert call.result == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert comparer.calls == []


def test_목록에_없는_책은_소개글을_읽지_않는다() -> None:
    spy, _fake, ctx = _setup()
    comparer = _FakeComparer()

    call = _run(
        ctx, spy, ToolName.BOOK_COMPARE, {"books": [card(1), card(9)]}, comparer
    )

    assert call.result == {"ok": False, "error": "BOOK_NOT_IN_CARDS"}
    assert comparer.calls == []


@pytest.mark.parametrize("books", [[card(1)], [card(1), card(2), card(3), cart(1)], []])
def test_두_권에서_세_권까지만_비교한다(books) -> None:
    spy, _fake, ctx = _setup()
    comparer = _FakeComparer()

    call = _run(ctx, spy, ToolName.BOOK_COMPARE, {"books": books}, comparer)

    assert call.result == {"ok": False, "error": "INVALID_ARGUMENTS"}
    assert comparer.calls == []


@pytest.mark.parametrize("code", ["NO_DESCRIPTION", "COMPARE_FAILED"])
def test_비교하지_못하면_그_이유를_결과에_남긴다(code) -> None:
    spy, _fake, ctx = _setup()
    comparer = _FakeComparer(error=ToolErrorCode(code))

    call = _run(
        ctx, spy, ToolName.BOOK_COMPARE, {"books": [card(1), card(2)]}, comparer
    )

    assert call.result == {"ok": False, "error": code}
    assert call.arguments == {"isbns": [A, B]}


def test_로그인하지_않아도_비교는_된다() -> None:
    spy, _fake, ctx = _setup(logged_in=False)
    comparer = _FakeComparer()

    call = _run(
        ctx, spy, ToolName.BOOK_COMPARE, {"books": [card(1), card(2)]}, comparer
    )

    assert call.result["ok"] is True


# --- 실행할 부품이 없는 도구 ---


def test_비교_부품이_없으면_비교는_실행하지_않는다() -> None:
    spy, _fake, ctx = _setup()

    call = _run(ctx, spy, ToolName.BOOK_COMPARE, {"books": [card(1), card(2)]})

    assert call.result == {"ok": False, "error": "TOOL_NOT_IMPLEMENTED"}
    assert spy.calls == []


def test_모든_도구에_인자_모델이_있고_실행기가_있다() -> None:
    # 도구를 새로 만들면서 인자 모델이나 실행기를 빠뜨리지 않게. 비교는 부품(comparer)을 받아 실행해서
    # 실행기 표에 없다
    from app.agent.tools import _HANDLERS

    assert set(ARG_MODELS) == set(ToolName)
    assert set(_HANDLERS) | {ToolName.BOOK_COMPARE} == set(ToolName)


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


# --- 사용자의 말이 책의 근거인지 (evidence, #387) ---


def _words(ctx: TurnContext, message: str) -> TurnContext:
    ctx.message = message
    return ctx


@pytest.mark.parametrize(
    ("name", "args", "isbn", "book"),
    [
        (ToolName.CART_ADD, {"book": card(1)}, A, "카드 1번"),
        (
            ToolName.CART_UPDATE,
            {"book": cart(2), "quantity": 3},
            B,
            "장바구니 2번째 《",
        ),
        (ToolName.CART_REMOVE, {"book": cart(2)}, B, "장바구니 2번째 《"),
    ],
)
def test_사용자가_누구인지_말하지_않았으면_쓰기는_BE를_부르지_않고_막힌다(
    name, args, isbn, book
) -> None:
    spy, fake, ctx = _setup(seed=[(A, 1), (B, 1), (C, 1)])
    _see_cart(ctx, spy)
    spy.calls.clear()
    before = fake.cart_state(UID)
    _words(ctx, "그거 해줘")

    call = _run(ctx, spy, name, args)

    assert call.result["ok"] is False
    assert call.result["error"] == "REFERENCE_NOT_STATED"
    # 모델이 그 책을 짚어 물을 수 있게 고른 책을 알려 준다(#418). 장바구니 책은 제목을 안다
    assert call.result["book"].startswith(book)
    assert spy.calls == []
    assert fake.cart_state(UID) == before
    # 책은 코드가 풀었으니 기록의 ISBN 은 코드가 채운 값이다. 막혔으므로 응답의 정해진 책은 아니다
    assert call.arguments["isbn"] == isbn


def test_번호나_제목을_말했으면_쓰기가_실행된다() -> None:
    spy, fake, ctx = _setup(seed=[(A, 1), (B, 1), (C, 1)])
    _see_cart(ctx, spy)

    # 장바구니는 최근에 담은 책이 1번이라 2번째는 B 다
    call = _run(
        _words(ctx, "장바구니 두 번째 빼줘"),
        spy,
        ToolName.CART_REMOVE,
        {"book": cart(2)},
    )

    assert call.result == {"ok": True}
    assert B not in fake.cart_state(UID)


def test_말한_번호와_다른_책을_고르면_막힌다() -> None:
    spy, fake, ctx = _setup()
    before = fake.cart_state(UID)

    # 2번이라고 했는데 카드 1번을 골랐다
    call = _run(_words(ctx, "2번 담아줘"), spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result["error"] == "REFERENCE_NOT_STATED"
    assert fake.cart_state(UID) == before


def test_지금_보고_있는_책은_말에_근거가_없어도_된다() -> None:
    spy, fake, ctx = _setup(focused_isbn=C)

    call = _run(
        _words(ctx, "담아줘"), spy, ToolName.CART_ADD, {"book": {"source": "focused"}}
    )

    assert call.result == {"ok": True}
    assert fake.cart_state(UID) == {C: 1}


def test_book_offer로_카드_제목을_알게_되면_제목으로_말한_요청이_통과한다() -> None:
    # 제목에서 맞춘 낱말이 세 글자 이상이어야 근거다("책2" 같은 짧은 제목은 근거가 못 된다)
    books = [
        FakeBook(A, "파이썬 입문서", 10000, 100),
        FakeBook(B, "자바 프로그래밍", 12000, 100),
        FakeBook(C, "러스트 시스템", 14000, 100),
    ]
    fake = FakeCommerce(books, users=[UID])
    spy = _Spy(fake)
    ctx = TurnContext(
        user_id=UID,
        logged_in=True,
        cards=[
            ContextCard(isbn=A, rank=1, match_score=92),
            ContextCard(isbn=B, rank=2, match_score=87),
            ContextCard(isbn=C, rank=3, match_score=81),
        ],
        focused_isbn=None,
        allow_tools=None,
        message="프로그래밍 책 담아줘",
    )

    # 제목을 모르는 동안에는 카드 2번이 "프로그래밍 책"인지 말과 맞춰 볼 수 없다
    blocked = _run(ctx, spy, ToolName.CART_ADD, {"book": card(2)})
    assert blocked.result["error"] == "REFERENCE_NOT_STATED"

    _run(ctx, spy, ToolName.BOOK_OFFER, {"books": [card(1), card(2), card(3)]})
    ok = _run(ctx, spy, ToolName.CART_ADD, {"book": card(2)})

    assert ok.result == {"ok": True}
    assert fake.cart_state(UID) == {B: 1}


def test_AI가_고른_책은_근거보다_확인이_먼저다() -> None:
    spy, fake, ctx = _setup()
    ctx.picked.add(A)
    # 말에 근거가 없어도, 고른 책을 담으려는 시도에는 확인 규칙의 이유가 나간다
    call = _run(_words(ctx, "바로 담아줘"), spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result["error"] == "CONFIRMATION_REQUIRED"
    assert fake.cart_state(UID) == {}


# --- 대화에서 이야기한 책과 막힌 쓰기의 후보 (#418) ---


def _talked_about_a(ctx: TurnContext) -> TurnContext:
    """카드 1번(A) 한 권만 이야기한 최근 대화. 에이전트는 제목으로 말한다."""
    title = next(b.title for b in _books() if b.isbn == A)
    ctx.turns = [
        Turn(role="user", text="1번 얼마야?"),
        Turn(role="assistant", text=f"《{title}》은 10,000원이에요."),
    ]
    return ctx


def test_대화에서_한_권만_이야기했으면_제목을_읽어_보고_담는다() -> None:
    spy, fake, ctx = _setup()
    _talked_about_a(_words(ctx, "응 담아"))

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(1)})

    assert call.result == {"ok": True}
    assert fake.cart_state(UID) == {A: 1}
    # 에이전트가 제목으로 말한 줄을 알아보려고, 막히기 직전에 카드 제목을 한 번 읽었다
    assert spy.calls[0] == "get_offers"


def test_이야기한_책과_다른_책을_고르면_막히고_고른_책을_알려_준다() -> None:
    spy, fake, ctx = _setup()
    _talked_about_a(_words(ctx, "응 담아"))
    title_b = next(b.title for b in _books() if b.isbn == B)

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(2)})

    assert call.result == {
        "ok": False,
        "error": "REFERENCE_NOT_STATED",
        "book": f"카드 2번 《{title_b}》",
    }
    assert fake.cart_state(UID) == {}


def test_근거가_있으면_제목을_더_읽지_않는다() -> None:
    spy, _fake, ctx = _setup()
    _talked_about_a(_words(ctx, "2번 담아줘"))

    call = _run(ctx, spy, ToolName.CART_ADD, {"book": card(2)})

    assert call.result == {"ok": True}
    assert "get_offers" not in spy.calls
