"""⑦ 카드 밖 책 찾기·고르기를 맡긴 담기·주문 화면과 쿠폰·포인트(#631) 테스트. LLM 없이 돈다.

여기서 지키려는 것
- 찾은 책은 사용자가 원하는 책을 말로 그렸을 때만 담긴다. "그거"처럼 보인 책을 가리킨 말로는 담기지 않는다
- 골라서 담으라고 맡긴 말만 확인 없이 담고, 그 응답에는 확인받을 조합(selection)을 싣지 않는다(BE 는 selection 이
  있으면 actions 를 보지 않는다)
- "주문해 줘"로 담았을 때만 주문 화면 버튼과 checkout 이 나가고, 쿠폰·포인트는 BE 결제 규칙대로 고른다
- 기능을 끄면(기본) 지금과 똑같다: 책 찾기 도구가 없고, 고른 책은 확인 버튼으로만 담는다
"""

import asyncio
import json
from time import perf_counter

import httpx
import pytest

from app.agent import evidence
from app.agent import graph as graph_module
from app.agent import reply as reply_module
from app.agent import tools as tools_module
from app.agent.checkout import OrderLine, discount_of, plan
from app.agent.errors import ToolErrorCode
from app.agent.fake_llm import ScriptedModel, ai_calls, ai_text
from app.agent.graph import run_agent
from app.agent.reference import TurnContext
from app.agent.reply import COUPON_LATER, ORDER_SCREEN, finish
from app.agent.schemas import (
    AgentActData,
    Checkout,
    OwnedCouponIn,
    ToolName,
    parse_request,
)
from app.agent.tools import execute
from app.gate import rules, shopping
from app.gateway.book_finder import (
    FakeBookFinder,
    FinderUnavailable,
    FoundBook,
    HttpBookFinder,
)
from app.gateway.commerce_provider import get_commerce

UID = 1
P1, P2, P3, P4 = (f"97911{n:08d}" for n in (1, 2, 3, 4))
CARD_A = "9788900000011"


def _coupon(
    owned: int, coupon: int, kind: str, discount: float, minimum: int = 0
) -> OwnedCouponIn:
    return OwnedCouponIn(
        owned_coupon_id=owned,
        coupon_id=coupon,
        name=f"쿠폰{owned}",
        type=kind,  # type: ignore[arg-type]
        discount=discount,
        min_order_amount=minimum,
    )


def _shelf() -> list[FoundBook]:
    return [
        FoundBook(
            P1, 101, "처음 배우는 개발 입문서", "가", 18000, True, frozenset({3})
        ),
        FoundBook(P2, 102, "개발 입문서 실전편", "나", 22000, True, frozenset({3, 9})),
        FoundBook(P3, 103, "개발 입문서 품절판", "다", 15000, False, frozenset()),
        FoundBook(P4, 104, "파이썬 입문서", "라", 20000, True, frozenset()),
    ]


# --- 쿠폰·포인트 계산 ---


def test_할인이_가장_큰_쿠폰과_남은_금액만큼_포인트를_고른다() -> None:
    lines = [
        OrderLine(P1, 18000, 1, frozenset({3})),
        OrderLine(P2, 22000, 1, frozenset({3})),
    ]
    coupons = [
        _coupon(7, 3, "FIXED_AMOUNT", 3000, 30000),
        _coupon(8, 3, "PERCENT_RATE", 0.1, 30000),  # 40,000 × 10% = 4,000
    ]

    result = plan(lines, coupons, point_balance=5000)

    assert result.amount == 40000
    assert result.coupon is not None and result.coupon.owned_coupon_id == 8
    assert result.discount == 4000
    assert result.use_point == 5000


def test_대상이_아니거나_최소_금액이_모자라면_쓰지_않는다() -> None:
    lines = [OrderLine(P1, 18000, 1, frozenset({3}))]
    coupons = [
        _coupon(1, 9, "FIXED_AMOUNT", 5000),  # 이 책이 대상이 아니다
        _coupon(2, 3, "FIXED_AMOUNT", 3000, 30000),  # 18,000 < 30,000
        _coupon(3, 3, "NONE", 0),
    ]

    result = plan(lines, coupons, point_balance=None)

    assert result.coupon is None
    assert result.use_point == 0


def test_할인이_주문_금액보다_크면_쓰지_않는다() -> None:
    lines = [OrderLine(P1, 5000, 1, frozenset({3}))]

    assert plan(lines, [_coupon(1, 3, "FIXED_AMOUNT", 6000)], 0).coupon is None


def test_포인트는_쿠폰을_뺀_금액까지만_쓴다() -> None:
    lines = [OrderLine(P1, 10000, 1, frozenset({3}))]

    result = plan(lines, [_coupon(1, 3, "FIXED_AMOUNT", 3000)], point_balance=50000)

    assert result.use_point == 7000


def test_할인이_같으면_보유_쿠폰_번호가_작은_쪽이다() -> None:
    lines = [OrderLine(P1, 30000, 1, frozenset({3}))]
    coupons = [
        _coupon(9, 3, "FIXED_AMOUNT", 3000),
        _coupon(4, 3, "PERCENT_RATE", 0.1),
    ]

    chosen = plan(lines, coupons, 0).coupon
    assert chosen is not None and chosen.owned_coupon_id == 4


def test_정률은_원_아래를_자르지_않고_견주고_포인트는_내림한다() -> None:
    lines = [OrderLine(P1, 12345, 1, frozenset({3}))]
    coupon = _coupon(1, 3, "PERCENT_RATE", 0.15)

    assert str(discount_of(coupon, 12345)) == "1851.75"
    result = plan(lines, [coupon], point_balance=99999)
    assert result.discount == 1851
    assert result.use_point == 10493  # 12345 - 1851.75 = 10493.25 → 내림


# --- BE 공개 API ---


def _be(handler) -> HttpBookFinder:
    return HttpBookFinder("https://be.test", 5, transport=httpx.MockTransport(handler))


def _ok(data) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


def test_검색한_뒤_상품_상세로_가격_재고_쿠폰을_채운다() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/api/v1/search":
            assert request.url.params["query"] == "개발 입문서"
            assert request.url.params["size"] == "5"
            return _ok(
                {
                    "items": [
                        {"isbn": P1, "productId": 101, "title": "책1", "author": "가"},
                        {"isbn": None, "productId": 102, "title": "ISBN 없음"},
                        {"isbn": P3, "productId": 103, "title": "상세 실패"},
                        {"isbn": P4, "productId": 104, "title": "품절"},
                    ]
                }
            )
        product = int(request.url.path.rsplit("/", 1)[-1])
        if product == 103:
            return httpx.Response(404, json={"success": False})
        return _ok(
            {
                "discountedPrice": 13500.00 if product == 101 else 9000,
                "stockQuantity": 3 if product == 101 else 0,
                "couponIds": [3, 9] if product == 101 else [],
            }
        )

    books = asyncio.run(_be(handler).find("개발 입문서", 5))

    assert books == [
        FoundBook(P1, 101, "책1", "가", 13500, True, frozenset({3, 9})),
        FoundBook(P4, 104, "품절", None, 9000, False, frozenset()),
    ]
    assert not any(
        "/products/102" in url for url in seen
    )  # ISBN 없는 책은 상세도 읽지 않는다


def test_검색이_실패하면_FinderUnavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(FinderUnavailable):
        asyncio.run(_be(handler).find("파이썬", 5))


def test_가짜_서가는_검색어_낱말이_많이_든_책부터_준다() -> None:
    finder = FakeBookFinder(_shelf())

    found = asyncio.run(finder.find("개발 입문서", 3))

    assert [b.isbn for b in found] == [P1, P2, P3]


# --- 말 읽기 ---


def _ctx(message: str, **overrides) -> TurnContext:
    values = {
        "user_id": UID,
        "logged_in": True,
        "cards": [],
        "focused_isbn": None,
        "allow_tools": None,
        "message": message,
        "checkout_enabled": True,
    }
    values.update(overrides)
    return TurnContext(**values)


@pytest.mark.parametrize(
    ("message", "order"),
    [
        ("개발 입문서 두 권 골라서 쿠폰 적용해서 주문해 줘", True),
        ("파이썬 입문서 하나 사 줘", True),
        ("결제까지 해 줘", True),
        ("1번 주문", True),
        ("2번 구매!", True),
        ("파이썬 입문서 하나 담아 줘", False),
        ("주문 내역 보여줘", False),
        ("주문한 책 언제 와?", False),
        ("주문내역", False),
    ],
)
def test_주문하겠다는_말만_주문으로_본다(message: str, order: bool) -> None:
    assert evidence.wants_order(_ctx(message)) is order


@pytest.mark.parametrize(
    ("message", "delegated"),
    [
        ("3만원 안으로 두 권 골라서 담아줘", True),
        ("알아서 골라서 주문해 줘", True),
        ("3만원 안에서 2권 골라줘", False),  # 고르기만 맡겼다
        ("1번 담아줘", False),
    ],
)
def test_고르기와_담기를_함께_맡긴_말만_위임이다(message: str, delegated: bool) -> None:
    assert evidence.delegates_pick(_ctx(message)) is delegated


def test_사_달라는_말은_행동을_말한_것이다() -> None:
    assert evidence.states_action(_ctx("파이썬 입문서 하나 사 줘"))


@pytest.mark.parametrize(
    "message",
    [
        "파이썬 책 결제해 줘",
        "리눅스 책 바로 주문할게요",
        "알고리즘 책 하나 구매하고 싶어요",
    ],
)
def test_주문하겠다는_말은_담으라는_말이기도_하다(message: str) -> None:
    # 주문 말로 읽고 담기는 막으면 주문 화면을 못 연다. "결제해 줘"가 그랬다(11/10 평가 checkout-m09)
    ctx = _ctx(message)

    assert evidence.wants_order(ctx)
    assert evidence.states_action(ctx)


def test_기능을_끄면_결제해_달라는_말로는_담지_않는다() -> None:
    # 꺼진 ⑦ 은 주문 화면이 없다. 결제는 못 한다고 알릴 말이다(main 과 같다)
    assert not evidence.states_action(
        _ctx("파이썬 책 결제해 줘", checkout_enabled=False)
    )


def test_기능을_끄면_사_달라는_말은_행동을_말한_것이_아니다() -> None:
    # 꺼져 있으면 이 말에 장바구니를 바꾸지 않는다. main 과 같다
    assert not evidence.states_action(
        _ctx("파이썬 입문서 하나 사 줘", checkout_enabled=False)
    )


# --- 도구 ---


def _snapshot_request(message: str, **extra) -> dict:
    payload = {
        "user_id": UID,
        "message": message,
        "context_cards": [],
        "user_context": {"logged_in": True},
        "idempotency_key": "idem_631",
        "cart": [],
        "offers": [],
    }
    payload.update(extra)
    return payload


def _tool_ctx(message: str, **overrides):
    req = parse_request(_snapshot_request(message))
    assert req is not None
    ctx = TurnContext.from_request(req)
    ctx.checkout_enabled = True
    ctx.cart_snapshot = []
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx, get_commerce(req)


def _exec(ctx, gateway, name, args, finder=None):
    return asyncio.run(execute(ctx, gateway, name, args, finder=finder))


def search(n: int) -> dict:
    return {"source": "search", "n": n}


class _SlowFinder:
    """검색이 느린 BE. 몇 번 불렸는지 센다."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.calls = 0

    async def find(self, query: str, size: int) -> list[FoundBook]:
        self.calls += 1
        await asyncio.sleep(self.seconds)
        return _shelf()


@pytest.fixture
def no_grace(monkeypatch) -> None:
    """검색에 마감 뒤로 얹어 주는 시간을 없앤다. 시험이 몇 초씩 기다리지 않게 한다."""
    monkeypatch.setattr(tools_module, "SEARCH_GRACE_SECONDS", 0.0)


def test_마감이_지났으면_책_찾기를_부르지_않는다() -> None:
    ctx, gateway = _tool_ctx("개발 입문서 하나 담아 줘")
    finder = _SlowFinder(0.0)

    found = asyncio.run(
        execute(
            ctx,
            gateway,
            ToolName.BOOK_SEARCH,
            {"query": "개발 입문서"},
            finder=finder,
            time_left=lambda: -0.1,
        )
    )

    assert found.result == {"ok": False, "error": ToolErrorCode.SEARCH_FAILED.value}
    assert finder.calls == 0
    assert ctx.search_results == []


def test_마감까지_남은_시간_안에_못_끝낸_검색은_실패로_끝낸다(no_grace) -> None:
    ctx, gateway = _tool_ctx("개발 입문서 하나 담아 줘")
    finder = _SlowFinder(5.0)

    started = perf_counter()
    found = asyncio.run(
        execute(
            ctx,
            gateway,
            ToolName.BOOK_SEARCH,
            {"query": "개발 입문서"},
            finder=finder,
            time_left=lambda: 0.05,
        )
    )

    assert perf_counter() - started < 1.0
    assert found.result == {"ok": False, "error": ToolErrorCode.SEARCH_FAILED.value}
    assert ctx.search_results == []


def test_시간이_넉넉하면_검색은_그대로_돈다(no_grace) -> None:
    ctx, gateway = _tool_ctx("개발 입문서 하나 담아 줘")

    found = asyncio.run(
        execute(
            ctx,
            gateway,
            ToolName.BOOK_SEARCH,
            {"query": "개발 입문서"},
            finder=_SlowFinder(0.0),
            time_left=lambda: 20.0,
        )
    )

    assert len(found.result["books"]) == 4


def test_한_반복에서_검색이_느려도_마감_뒤의_검색은_하지_않고_턴_시간_안에_답한다(
    no_grace,
) -> None:
    # 턴 제한 0.9초면 0.3초를 남기고 0.6초에 도구를 그만 쓴다. 검색 하나가 0.4초 걸리니 세 번을 차례로 하면
    # 1.2초다(운영의 30초에서 검색 5초 + 상세 5초 한도가 겹치는 모양). 마감 뒤의 검색은 부르지 않는다(#637 리뷰)
    finder = _SlowFinder(0.4)
    req = parse_request(_snapshot_request("개발 입문서 알아봐 줘"))
    assert req is not None
    model = ScriptedModel(
        [
            ai_calls(
                (ToolName.BOOK_SEARCH, {"query": "개발 입문서"}),
                (ToolName.BOOK_SEARCH, {"query": "파이썬 입문서"}),
                (ToolName.BOOK_SEARCH, {"query": "데이터베이스"}),
            ),
            ai_text("찾아봤어요."),
        ]
    )

    started = perf_counter()
    run = asyncio.run(
        run_agent(
            req,
            model=model,
            gateway=get_commerce(req),
            finder=finder,
            checkout=True,
            turn_seconds=0.9,
        )
    )

    assert perf_counter() - started < 1.0
    assert finder.calls < 3
    errors = [c.result.get("error") for c in run.tool_calls]
    assert errors[-1] == ToolErrorCode.SEARCH_FAILED.value
    assert run.final_text == "찾아봤어요."


def test_찾은_책은_장바구니_복사본이_알게_되어_담을_수_있다() -> None:
    ctx, gateway = _tool_ctx("개발 입문서 하나 담아 줘")
    finder = FakeBookFinder(_shelf())

    found = _exec(ctx, gateway, ToolName.BOOK_SEARCH, {"query": "개발 입문서"}, finder)
    added = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(2)})

    # "입문서"만 맞는 P4 는 두 낱말이 다 맞는 책 뒤에 온다
    assert [b["isbn"] for b in found.result["books"]] == [P1, P2, P3, P4]
    assert added.result == {"ok": True}
    assert asyncio.run(gateway.get_cart(UID))[0].isbn == P2
    assert gateway.actions()[0].isbn == P2


def test_보인_책을_가리킨_말로는_찾은_책을_담지_않는다() -> None:
    ctx, gateway = _tool_ctx("그거 담아줘")
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "개발 입문서"},
        FakeBookFinder(_shelf()),
    )

    blocked = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1)})

    assert blocked.result["error"] == ToolErrorCode.REFERENCE_NOT_STATED.value
    assert "찾은 책 1번" in blocked.result["ask"]
    assert gateway.actions() == []


def test_두_권_골라서의_두_권은_책마다의_수량이_아니다() -> None:
    ctx, gateway = _tool_ctx("개발 입문서 두 권 골라서 담아 줘")
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "개발 입문서"},
        FakeBookFinder(_shelf()),
    )

    added = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1), "quantity": 2})

    assert added.arguments["quantity"] == 1


def test_품절인_찾은_책은_담지_못한다() -> None:
    ctx, gateway = _tool_ctx("개발 입문서 품절판 담아 줘")
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "개발 입문서"},
        FakeBookFinder(_shelf()),
    )

    added = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(3)})

    assert added.result["error"] == "INSUFFICIENT_PRODUCT_STOCK"


def test_검색이_실패하면_이유만_남긴다() -> None:
    class Broken:
        async def find(self, query, size):
            raise FinderUnavailable("x")

    ctx, gateway = _tool_ctx("파이썬 책 담아줘")
    call = _exec(ctx, gateway, ToolName.BOOK_SEARCH, {"query": "파이썬"}, Broken())

    assert call.result == {"ok": False, "error": ToolErrorCode.SEARCH_FAILED.value}


def test_찾을_곳이_없으면_책_찾기는_막힌다() -> None:
    ctx, gateway = _tool_ctx("파이썬 책 담아줘")

    call = _exec(ctx, gateway, ToolName.BOOK_SEARCH, {"query": "파이썬"})

    assert call.result["error"] == ToolErrorCode.TOOL_NOT_IMPLEMENTED.value


def test_찾았으면_조합은_찾은_책_안에서_고르고_맡겼으면_바로_담는다() -> None:
    ctx, gateway = _tool_ctx("4만원 안으로 개발 입문서 두 권 골라서 담아 줘")
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "개발 입문서"},
        FakeBookFinder(_shelf()),
    )

    picked = _exec(
        ctx, gateway, ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2}
    )
    first = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1)})
    second = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(2)})

    assert {b["isbn"] for b in picked.result["chosen"]} == {P1, P2}  # 품절 P3 는 뺀다
    assert first.result == {"ok": True} and second.result == {"ok": True}


def test_기능이_꺼져_있으면_고른_책은_확인_버튼으로만_담는다() -> None:
    ctx, gateway = _tool_ctx(
        "4만원 안으로 두 권 골라서 담아 줘", checkout_enabled=False
    )
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "개발 입문서"},
        FakeBookFinder(_shelf()),
    )
    _exec(ctx, gateway, ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2})

    blocked = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1)})

    assert blocked.result["error"] == ToolErrorCode.CONFIRMATION_REQUIRED.value


# --- 한 턴 전체 ---


def _turn(message: str, replies, *, checkout=True, finder=True, **extra):
    req = parse_request(_snapshot_request(message, **extra))
    assert req is not None
    model = ScriptedModel(replies)
    run = asyncio.run(
        run_agent(
            req,
            model=model,
            gateway=get_commerce(req),
            finder=FakeBookFinder(_shelf()) if finder else None,
            checkout=checkout,
        )
    )
    return run, finish(run).data, model


COUPONS = [
    {
        "owned_coupon_id": 7,
        "coupon_id": 3,
        "name": "IT 3천원",
        "type": "FIXED_AMOUNT",
        "discount": 3000.00,
        "min_order_amount": 30000.00,
        "expired_at": "2026-11-30T23:59:59",
    }
]


def test_골라서_주문해_달라면_담고_주문_화면과_쿠폰_포인트를_준다() -> None:
    _, data, _ = _turn(
        "개발 입문서 두 권 골라서 쿠폰 적용해서 주문해 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls(
                (ToolName.CART_ADD, {"book": search(1)}),
                (ToolName.CART_ADD, {"book": search(2)}),
            ),
            ai_text("두 권을 담았어요. 주문을 완료했어요."),
        ],
        owned_coupons=COUPONS,
        point_balance=5000.00,
    )

    assert [(a.op, a.isbn, a.quantity) for a in data.actions] == [
        ("set", P1, 1),
        ("set", P2, 1),
    ]
    assert data.checkout == Checkout(owned_coupon_id=7, use_point=5000)
    assert [(b.action, b.target) for b in data.buttons] == [("navigate", ORDER_SCREEN)]
    assert data.selection is None
    assert "IT 3천원 쿠폰(3,000원 할인)과 포인트 5,000원을 적용해 둘게요" in data.reply
    assert "주문을 완료했어요" not in data.reply  # 주문서를 만들지 않았다
    dumped = data.model_dump(mode="json")
    assert dumped["checkout"] == {"owned_coupon_id": 7, "use_point": 5000}


@pytest.mark.parametrize(
    "sentence",
    [
        "주문 화면에서 주문을 마무리해 주세요.",
        "결제를 마무리하시면 돼요.",
        "주문 화면에서 결제를 완료해 주세요.",
        "주문을 완료하시면 재고가 잡혀요.",
        "결제를 완료하려면 주문 화면으로 가세요.",
        "구매를 마무리하세요.",
    ],
)
def test_주문을_마무리하라는_부탁은_거짓_완료가_아니다(sentence: str) -> None:
    assert reply_module._ORDER_CLAIM.search(sentence) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "주문을 완료했어요.",
        "주문이 완료되었습니다.",
        "결제가 완료됐어요.",
        "주문을 마무리했어요.",
        "주문을 마쳤어요.",
        "구매를 끝냈어요.",
        "주문 완료!",
        "주문해 드렸어요.",
        "결제를 마무리해 드렸어요.",
    ],
)
def test_주문을_했다는_말은_여전히_거짓_완료로_본다(sentence: str) -> None:
    assert reply_module._ORDER_CLAIM.search(sentence) is not None


def test_주문_화면에서_마무리하라는_말은_답에_남는다() -> None:
    _, data, _ = _turn(
        "개발 입문서 두 권 골라서 주문해 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls(
                (ToolName.CART_ADD, {"book": search(1)}),
                (ToolName.CART_ADD, {"book": search(2)}),
            ),
            ai_text("두 권을 담았어요. 주문 화면에서 주문을 마무리해 주세요."),
        ],
    )

    assert "주문 화면에서 주문을 마무리해 주세요" in data.reply


def test_담기만_하면_장바구니_버튼이고_checkout이_없다() -> None:
    _, data, _ = _turn(
        "개발 입문서 하나 담아 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls((ToolName.CART_ADD, {"book": search(1)})),
            ai_text("담았어요."),
        ],
        owned_coupons=COUPONS,
        point_balance=5000,
    )

    assert data.checkout is None
    assert "checkout" not in data.model_dump(mode="json")
    assert [b.target for b in data.buttons] == ["CART-001"]


def test_쿠폰_적용해서_장바구니에_넣어_달라면_쿠폰은_주문_화면에서라고_알린다() -> None:
    _, data, _ = _turn(
        "개발 입문서 하나 쿠폰 적용해서 장바구니에 넣어 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls((ToolName.CART_ADD, {"book": search(1)})),
            ai_text("담았어요."),
        ],
    )

    assert COUPON_LATER in data.reply
    assert data.checkout is None


def test_골라서_담으라고_맡기면_고른_책을_코드가_담고_조합은_싣지_않는다() -> None:
    # 모델이 고르기만 하고 끝내도 담긴다(평가 checkout-05). BE 는 selection 이 있으면 actions 를 보지 않는다
    run, data, model = _turn(
        "4만원 안으로 개발 입문서 두 권 골라서 주문해 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls((ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2})),
            ai_text("골라 드렸어요."),
        ],
        point_balance=1000,
    )

    assert {a.isbn for a in data.actions} == {P1, P2}
    assert data.selection is None
    assert all(b.action != "confirm_selection" for b in data.buttons)
    assert [b.target for b in data.buttons] == [ORDER_SCREEN]
    assert data.checkout == Checkout(owned_coupon_id=None, use_point=1000)
    names = [c.name for c in run.tool_calls]
    assert names == [
        ToolName.BOOK_SEARCH,
        ToolName.SELECTION_PICK,
        ToolName.CART_ADD,
        ToolName.CART_ADD,
    ]
    seen = model.calls[-1][-1].content
    assert '"added"' in seen and "찾은 책 1번" in seen and P1 not in seen


def _full_cart(n: int) -> list[dict]:
    return [
        {
            "isbn": f"97911{i:08d}",
            "title": f"꽂힌 책{i}",
            "price": 10000,
            "quantity": 1,
            "available": True,
        }
        for i in range(100, 100 + n)
    ]


def test_골라서_담는_두_권_중_하나가_한도에_걸리면_모두_되돌린다() -> None:
    # 29권에 두 권을 담으면 두 번째가 30권 한도에 걸린다. 모델이 cart.add 두 개를 시켰다면 모두 되돌렸을 일이다.
    # 코드가 대신 담아도 같아야 한다(#488, #489, #637 리뷰)
    run, data, _ = _turn(
        "4만원 안으로 개발 입문서 두 권 골라서 담아 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls((ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2})),
            ai_text("골라서 담았어요."),
        ],
        cart=_full_cart(29),
    )

    adds = [c for c in run.tool_calls if c.name == ToolName.CART_ADD]
    assert [c.result.get("error") for c in adds] == [
        "BATCH_ROLLED_BACK",
        "CART_ITEM_LIMIT_EXCEEDED",
    ]
    assert data.actions == []
    assert run.ctx.changed_cart is False
    assert "가득 차서" in data.reply
    assert "함께 부탁하신 것 중 하나가 안 돼서" in data.reply
    assert data.checkout is None


def test_코드가_담는_책에도_한_번에_담는_수_상한이_걸린다(monkeypatch) -> None:
    # 모델이 cart.add 를 한 번에 여럿 시킬 때(MAX_CALLS_PER_STEP)와 같다. 넘는 책은 담지 않고 이유를 알린다(#637 리뷰)
    monkeypatch.setattr(graph_module, "MAX_CALLS_PER_STEP", 1)
    run, data, _ = _turn(
        "4만원 안으로 개발 입문서 두 권 골라서 담아 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls((ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2})),
            ai_text("골라서 담았어요."),
        ],
    )

    pick = next(c for c in run.tool_calls if c.name == ToolName.SELECTION_PICK)
    first, second = (book["isbn"] for book in pick.result["chosen"])
    assert [a.isbn for a in data.actions] == [first]
    assert [(a["isbn"], a.get("error")) for a in pick.result["added"]] == [
        (first, None),
        (second, ToolErrorCode.TOO_MANY_CALLS.value),
    ]
    assert len([c for c in run.tool_calls if c.name == ToolName.CART_ADD]) == 1


def test_골라서_담는_두_권이_모두_들어가면_되돌리지_않는다() -> None:
    run, data, _ = _turn(
        "4만원 안으로 개발 입문서 두 권 골라서 담아 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls((ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2})),
            ai_text("골라서 담았어요."),
        ],
        cart=_full_cart(5),
    )

    adds = [c for c in run.tool_calls if c.name == ToolName.CART_ADD]
    assert [c.result.get("ok") for c in adds] == [True, True]
    assert {a.isbn for a in data.actions} == {P1, P2}
    assert run.ctx.changed_cart is True


def test_코드가_담은_책을_모델이_또_담아도_담았다고_말한다() -> None:
    _, data, _ = _turn(
        "4만원 안으로 개발 입문서 두 권 골라서 담아 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls((ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2})),
            ai_calls((ToolName.CART_ADD, {"book": search(1)})),
            ai_text("담았어요."),
        ],
    )

    assert "《처음 배우는 개발 입문서》를 장바구니에 담았어요" in data.reply
    assert "이미 장바구니에" not in data.reply


def test_골라만_달라면_기능이_켜져_있어도_확인_버튼이다() -> None:
    _, data, _ = _turn(
        "카드 중에 4만원 안에서 2권 골라줘",
        [
            ai_calls((ToolName.SELECTION_PICK, {"budget_total": 40000, "count": 2})),
            ai_text("골랐어요."),
        ],
        context_cards=[
            {"isbn": P1, "rank": 1, "match_score": 90},
            {"isbn": P2, "rank": 2, "match_score": 80},
        ],
        offers=[
            {"isbn": P1, "title": "책1", "price": 18000, "in_stock": True},
            {"isbn": P2, "title": "책2", "price": 22000, "in_stock": True},
        ],
    )

    assert data.actions == []
    assert data.selection is not None
    assert any(b.action == "confirm_selection" for b in data.buttons)


def test_제목을_말한_책이_품절이면_다른_찾은_책을_대신_담지_않는다() -> None:
    # 평가 checkout-04: "테스트 주도 개발 책"이 품절이라 모델이 다른 책을 담았다
    ctx, gateway = _tool_ctx("개발 입문서 품절판 담아 줘")
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "개발 입문서"},
        FakeBookFinder(_shelf()),
    )

    other = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1)})

    assert other.result["error"] == ToolErrorCode.REFERENCE_NOT_STATED.value
    assert gateway.actions() == []


def test_주문해_달라도_담은_책이_없으면_주문_화면으로_보내지_않는다() -> None:
    _, data, _ = _turn(
        "개발 입문서 품절판 주문해 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_calls((ToolName.CART_ADD, {"book": search(3)})),
            ai_text("품절이에요."),
        ],
        owned_coupons=COUPONS,
    )

    assert data.actions == []
    assert data.checkout is None
    assert all(b.target != ORDER_SCREEN for b in data.buttons)


def test_기능이_꺼져_있으면_책_찾기를_주지_않고_주문_화면도_없다() -> None:
    _, data, model = _turn(
        "1번 주문해 줘",
        [ai_text("주문은 할 수 없어요.")],
        checkout=False,
        finder=False,
    )

    offered = {tool["function"]["name"] for tool in model.tools_seen[0]}
    assert "book_search" not in offered
    assert data.checkout is None
    assert "주문서를 만들거나" not in model.calls[0][0].content


def test_찾을_곳이_없으면_켜져_있어도_책_찾기를_주지_않는다() -> None:
    _, _, model = _turn("파이썬 책 담아줘", [ai_text("못 찾았어요.")], finder=False)

    offered = {tool["function"]["name"] for tool in model.tools_seen[0]}
    assert "book_search" not in offered
    assert "주문서를 만들거나" in model.calls[0][0].content  # 주문 안내는 켜져 있다


def test_LLM에게는_찾은_책의_ISBN이_보이지_않는다() -> None:
    _, _, model = _turn(
        "개발 입문서 하나 담아 줘",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_text("어느 책을 담을까요?"),
        ],
    )

    seen = json.dumps([m.content for m in model.calls[-1]], ensure_ascii=False)
    assert P1 not in seen and "찾은 책 1번" in seen


# --- 요청·응답 모양 ---


def test_보유_쿠폰과_포인트를_받는다() -> None:
    req = parse_request(
        _snapshot_request("주문해 줘", owned_coupons=COUPONS, point_balance=5000.0)
    )

    assert req is not None
    assert req.owned_coupons[0].min_order_amount == 30000
    assert req.point_balance == 5000


def test_보유_쿠폰이_null이면_빈_목록이다() -> None:
    req = parse_request(_snapshot_request("주문해 줘", owned_coupons=None))

    assert req is not None and req.owned_coupons == []


def test_포인트에_원_아래가_있으면_400이다() -> None:
    assert parse_request(_snapshot_request("주문해 줘", point_balance=10.5)) is None


def test_쇼핑이_아님_응답에는_checkout이_없다() -> None:
    assert "checkout" not in AgentActData.not_shopping_reply().model_dump(mode="json")


def test_챗봇_요청의_쿠폰과_포인트가_쇼핑_요청으로_넘어간다() -> None:
    payload = {
        **_snapshot_request("주문해 줘", owned_coupons=COUPONS, point_balance=5000),
        "spec": {},
    }

    req = shopping.to_agent_request(payload)

    assert req is not None and req.owned_coupons[0].owned_coupon_id == 7


# --- 문지기 ---


@pytest.mark.parametrize("message", ["파이썬 입문서 하나 사 줘", "파이썬 책 사 주세요"])
def test_기능을_켜면_카드가_없어도_사_달라는_말은_쇼핑이다(message: str) -> None:
    assert rules.route(message, False, False, False, False, buy_requests=True) == "shop"


@pytest.mark.parametrize(
    ("message", "kind", "sure"),
    [
        # 추천 낱말("입문서")이 있어 ③
        ("파이썬 입문서 하나 사 줘", "recommend", True),
        # 낱말 근거가 없어 작은 판정 호출이 가른다
        ("파이썬 책 사 주세요", "neither", False),
    ],
)
def test_기능을_끄면_사_달라는_말을_v3처럼_가른다(
    message: str, kind: str, sure: bool
) -> None:
    # 꺼진 ⑦ 에는 book.search 가 없고 프롬프트는 "주문은 할 수 없다"다. 스위치를 끈 채 머지해도 ③의 길이 바뀌면 안 된다
    for decision in (
        rules.decide(message, False, False, False, False),
        rules.decide(message, False, False, False, False, buy_requests=False),
    ):
        assert (decision.kind, decision.sure) == (kind, sure)


def test_검사_주세요는_사_달라는_말이_아니다() -> None:
    decision = rules.decide(
        "검사 주세요", False, False, False, False, buy_requests=True
    )

    assert decision.kind == "neither"


# --- 평가 묶음(evals/agent/checkout_cases.json) ---


def _checkout_cases() -> dict:
    from evals.agent import grade

    return grade.load_cases(grade.CHECKOUT_CASES_PATH)


def test_평가_사례는_API가_받는_모양이고_기능을_켠_채로_돈다() -> None:
    data = _checkout_cases()
    catalog = {book["isbn"] for book in data["catalog"]}
    ids = [case["id"] for case in data["cases"]]
    users = [case["request"]["user_id"] for case in data["cases"]]

    assert len(set(ids)) == len(ids) and len(set(users)) == len(users)
    for case in data["cases"]:
        assert case["checkout"] is True
        assert case["kind"] in data["kinds"]
        assert parse_request(case["request"]) is not None, case["id"]
        assert set(case["expect"]["cart"]) <= catalog


def test_평가는_기대한_checkout과_다르면_실패로_센다() -> None:
    from evals.agent import grade

    case = {"expect": {"outcome": "executed", "cart": {}, "checkout": None}}
    record = {
        "status": 200,
        "reply": "",
        "data": {"buttons": [], "checkout": {"owned_coupon_id": 1, "use_point": 0}},
        "cart": {},
        "start_cart": {},
        "others": {},
        "start_others": {},
    }

    assert any("쿠폰·포인트" in p for p in grade._check_reply(case, record))


def test_평가의_가짜_서가는_판매_중인_책만_찾는다() -> None:
    from evals.agent.run import finder_for

    data = _checkout_cases()
    finder = finder_for(data, {"checkout": True})
    assert finder is not None

    found = asyncio.run(finder.find("웹 개발", 5))

    assert all(book.isbn != "9788900001124" for book in found)  # 판매 중지 책
    assert finder_for(data, {}) is None


def test_책_찾기를_주지_않으면_책을_가리키는_인자에도_찾은_책이_없다() -> None:
    # 기능을 끈 평가에서 없는 도구를 말하는 설명 때문에 모델이 멈췄다(describe-01)
    from app.agent.descriptors import tool_definitions

    names = [name for name in ToolName if name != ToolName.BOOK_SEARCH]
    text = json.dumps(tool_definitions(names), ensure_ascii=False)

    assert "book_search" not in text
    assert '"search"' not in text


def test_찾을_곳을_받아도_기능이_꺼져_있으면_책_찾기를_주지_않는다() -> None:
    _, _, model = _turn("파이썬 책 담아줘", [ai_text("못 찾았어요.")], checkout=False)

    offered = {tool["function"]["name"] for tool in model.tools_seen[0]}
    assert "book_search" not in offered


# --- 한 턴에 여러 번 찾기, 제목과 권수 (#631 평가 checkout-h04·h05) ---


def _search_twice(message: str):
    ctx, gateway = _tool_ctx(message)
    finder = FakeBookFinder(_shelf())
    first = _exec(ctx, gateway, ToolName.BOOK_SEARCH, {"query": "개발"}, finder)
    second = _exec(ctx, gateway, ToolName.BOOK_SEARCH, {"query": "파이썬"}, finder)
    return ctx, gateway, first, second


def test_두_번_찾으면_찾은_책_번호가_턴_전체로_이어_붙는다() -> None:
    # 마지막 검색 기준으로 매겼더니 두 검색의 책이 둘 다 1번이 되어 같은 책을 두 번 담았다(h04)
    from app.agent.view import llm_view

    ctx, gateway, first, second = _search_twice(
        "개발 입문서 하나랑 파이썬 책 하나 담아 줘"
    )

    assert [b.isbn for b in ctx.search_results] == [P1, P2, P3, P4]
    assert [b["book"] for b in llm_view(ctx, first)["books"]] == [
        "찾은 책 1번",
        "찾은 책 2번",
        "찾은 책 3번",
    ]
    assert [b["book"] for b in llm_view(ctx, second)["books"]] == ["찾은 책 4번"]

    a = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1)})
    b = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(4)})

    assert a.result == b.result == {"ok": True}
    assert [(x.isbn, x.quantity) for x in gateway.actions()] == [(P1, 1), (P4, 1)]


def test_다시_나온_책은_처음_번호를_그대로_쓴다() -> None:
    from app.agent.view import llm_view

    ctx, gateway = _tool_ctx("개발 입문서 담아 줘")
    finder = FakeBookFinder(_shelf())
    _exec(ctx, gateway, ToolName.BOOK_SEARCH, {"query": "파이썬"}, finder)
    again = _exec(ctx, gateway, ToolName.BOOK_SEARCH, {"query": "입문서"}, finder)

    assert [b.isbn for b in ctx.search_results] == [P4, P1, P2, P3]
    # 두 번째 검색에도 P4 가 나오지만 번호는 처음 찾았을 때의 1번이다
    labels = {b["title"]: b["book"] for b in llm_view(ctx, again)["books"]}
    assert labels["파이썬 입문서"] == "찾은 책 1번"
    assert labels["처음 배우는 개발 입문서"] == "찾은 책 2번"


def test_제목을_말한_검색이_주제로_그린_다른_검색의_책을_막지_않는다() -> None:
    # 제목 판정을 턴 전체로 했더니 "파이썬"(제목 낱말)이 첫 검색의 책까지 근거 없음으로 막았다
    ctx, gateway, _, _ = _search_twice("개발 입문서 하나랑 파이썬 입문서 담아 줘")

    described = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(2)})
    named = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(4)})

    assert described.result == {"ok": True}
    assert named.result == {"ok": True}


def test_한_검색_안에서는_제목을_말한_책만_담는다() -> None:
    # 검색마다 따로 보아도 같은 검색 안의 "제목을 말했으면 그 책만"은 그대로다(checkout-04)
    ctx, gateway, _, _ = _search_twice("개발 입문서 품절판이랑 파이썬 책 담아 줘")

    other = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1)})

    assert other.result["error"] == ToolErrorCode.REFERENCE_NOT_STATED.value


def test_제목을_말하고_권수를_말하면_그_책의_수량이다() -> None:
    # "두 권 = 서로 다른 책" 규칙이 제목을 말한 책에도 걸려 3권이 1권이 됐다(h05)
    ctx, gateway = _tool_ctx("파이썬 입문서 3권 주문해 줘")
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "파이썬"},
        FakeBookFinder(_shelf()),
    )

    added = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1), "quantity": 3})

    assert added.result == {"ok": True}
    assert added.arguments["quantity"] == 3


@pytest.mark.parametrize("given", [{"quantity": 1}, {}])
def test_제목과_권수를_말했으면_모델이_빠뜨린_수량을_코드가_채운다(given: dict) -> None:
    # 실제 모델이 "리눅스 명령어 사전 두 권 사 주세요"에 수량을 1로 채우거나 비워 한 권만 담았다(checkout-m08)
    ctx, gateway = _tool_ctx("파이썬 입문서 두 권 사 주세요")
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "파이썬"},
        FakeBookFinder(_shelf()),
    )

    added = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1), **given})

    assert added.result == {"ok": True}
    assert added.arguments["quantity"] == 2


@pytest.mark.parametrize(
    ("message", "copies"),
    [
        ("파이썬 입문서 두 권 사 주세요", 2),
        ("파이썬 입문서 3권 주문해 줘", 3),
        (
            "입문서 세 권 골라서 담아 줘",
            None,
        ),  # 고르기를 맡긴 권수는 서로 다른 책의 수다
        ("파이썬 입문서 담아 줘", None),  # 권수를 말하지 않았다
    ],
)
def test_제목과_권수를_말했을_때만_그_권수를_준다(
    message: str, copies: int | None
) -> None:
    ctx, gateway = _tool_ctx(message)
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "파이썬"},
        FakeBookFinder(_shelf()),
    )
    isbn = next(b.isbn for b in ctx.search_results if b.title == "파이썬 입문서")

    assert evidence.copies_of(ctx, isbn) == copies


def test_책마다_권수가_다르면_모델이_채운_수량을_쓴다() -> None:
    # 어느 수가 어느 책의 것인지 낱말로 모른다
    ctx, gateway = _tool_ctx("파이썬 입문서 2권이랑 개발 입문서 실전편 3권 담아 줘")
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "입문서"},
        FakeBookFinder(_shelf()),
    )
    python = next(
        n
        for n, book in enumerate(ctx.search_results, start=1)
        if book.title == "파이썬 입문서"
    )

    added = _exec(
        ctx, gateway, ToolName.CART_ADD, {"book": search(python), "quantity": 2}
    )

    assert added.arguments["quantity"] == 2


@pytest.mark.parametrize(
    "message",
    [
        "파이썬 입문서 담아 줘",  # 권수를 말하지 않았다(모델이 채운 수량은 믿지 않는다)
        "입문서 세 권 골라서 담아 줘",  # 고르기를 맡긴 말의 권수는 서로 다른 책의 수다
    ],
)
def test_제목과_권수를_같이_말하지_않으면_한_권이다(message: str) -> None:
    ctx, gateway = _tool_ctx(message)
    _exec(
        ctx,
        gateway,
        ToolName.BOOK_SEARCH,
        {"query": "파이썬"},
        FakeBookFinder(_shelf()),
    )

    added = _exec(ctx, gateway, ToolName.CART_ADD, {"book": search(1), "quantity": 3})

    assert added.arguments["quantity"] == 1


def test_두_주제를_한_턴에_찾아_하나씩_담는다() -> None:
    _, data, model = _turn(
        "개발 입문서 하나랑 파이썬 책 하나 담아 줘",
        [
            ai_calls(
                (ToolName.BOOK_SEARCH, {"query": "개발"}),
                (ToolName.BOOK_SEARCH, {"query": "파이썬"}),
            ),
            ai_calls(
                (ToolName.CART_ADD, {"book": search(1)}),
                (ToolName.CART_ADD, {"book": search(4)}),
            ),
            ai_text("두 권을 담았어요."),
        ],
    )

    assert [(a.op, a.isbn, a.quantity) for a in data.actions] == [
        ("set", P1, 1),
        ("set", P4, 1),
    ]
    # LLM 은 두 번째 검색의 책을 4번으로 받았다
    seen = json.dumps(
        [m.content for m in model.calls[1] if getattr(m, "type", "") == "tool"],
        ensure_ascii=False,
    )
    assert "찾은 책 4번" in seen


def test_주문_화면이_켜지면_사겠다는_말도_되묻지_말라고_알려_준다() -> None:
    # 평가 checkout-h03: "…구매하고 싶어요"에 책을 찾고도 "담아 드릴까요?"라고 되물었다
    from app.agent import prompts

    on = prompts.system_prompt(search=True, order=True)
    off = prompts.system_prompt(search=False, order=False)

    assert "사겠다고 한 말도 주문해 달라는 말이다" in on
    assert "그 책을 그 권수로 담는다" in on
    assert "사겠다고 한 말" not in off


# --- 쿠폰·포인트를 쓰지 말라는 말 (11/10 dev) ---

_CARD_TITLE = "웹 서비스 개발 입문서"


def card(n: int) -> dict:
    return {"source": "card", "n": n}


CARD_B = "9788900000028"
CARD_C = "9788900000035"


def _card_turn(message: str, replies, *, turns=(), cards=1, **extra):
    """카드(《웹 서비스 개발 입문서》, 둘이면 《자바 첫걸음》도)를 보여 준 뒤의 말."""
    shown = [(CARD_A, _CARD_TITLE), (CARD_B, "자바 첫걸음"), (CARD_C, "파이썬 산책")][
        :cards
    ]
    return _turn(
        message,
        replies,
        context_cards=[
            {"isbn": isbn, "rank": rank, "match_score": 48}
            for rank, (isbn, _) in enumerate(shown, start=1)
        ],
        offers=[
            {"isbn": isbn, "title": title, "price": 28000, "in_stock": True}
            for isbn, title in shown
        ],
        recent_turns=[{"role": r, "text": t} for r, t in turns],
        owned_coupons=COUPONS,
        point_balance=3000,
        **extra,
    )


_SHOWN = [
    ("user", "개발 입문서"),
    ("assistant", f"개발 입문서로 손색없는 『{_CARD_TITLE}』이 눈에 띄어요."),
]


@pytest.mark.parametrize(
    "message",
    [
        "이거 주문해줘 포인트 적용하지 말고",
        "이 책 주문해 줘 포인트는 안 쓸래",
        "그거 사 줘 쿠폰이랑 포인트 없이",
    ],
)
def test_할인을_쓰지_말라는_말은_지시어가_가리킨_책을_부정하지_않는다(
    message: str,
) -> None:
    # "말고"를 책 부정으로 읽어 "지금 보고 있는 책이 없어요"로 끝났다(11/10 dev)
    _, data, _ = _card_turn(
        message,
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("담았어요.")],
        turns=_SHOWN,
    )

    assert [(a.op, a.isbn) for a in data.actions] == [("set", CARD_A)]


def test_책_말고는_여전히_그_책을_부정한다() -> None:
    _, data, _ = _card_turn(
        "그거 말고 다른 거 주문해 줘 포인트 쓰지 말고",
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("어느 책일까요?")],
        turns=_SHOWN,
        cards=2,
    )

    assert data.actions == []


def test_포인트를_쓰지_말라면_주문_화면에_포인트를_채우지_않고_그렇다고_알린다() -> (
    None
):
    _, data, _ = _card_turn(
        "1번책 주문해봐 포인트 적용하지말고",
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("담았어요.")],
        turns=_SHOWN,
    )

    assert data.checkout == Checkout(owned_coupon_id=None, use_point=0)
    assert "포인트는 쓰지 않게 해 둘게요" in data.reply
    assert "포인트 3,000원" not in data.reply


def test_앞서_포인트를_쓰지_말라고_했으면_다음_주문에도_채우지_않는다() -> None:
    _, data, _ = _card_turn(
        "이 책 2개 주문해줘",
        [
            ai_calls((ToolName.CART_ADD, {"book": card(1), "quantity": 2})),
            ai_text("2권으로 바꿨어요."),
        ],
        turns=[
            *_SHOWN,
            ("user", "1번책 주문해봐 포인트 적용하지말고"),
            ("assistant", f"《{_CARD_TITLE}》을 장바구니에 담았어요."),
        ],
    )

    assert data.checkout is not None and data.checkout.use_point == 0


def test_이번_말이_포인트를_쓰라고_하면_앞의_말보다_이번_말을_따른다() -> None:
    _, data, _ = _card_turn(
        "1번 주문해 줘 포인트 써 줘",
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("담았어요.")],
        turns=[*_SHOWN, ("user", "포인트 쓰지 말고 주문할게")],
    )

    assert data.checkout is not None and data.checkout.use_point == 3000


def test_쿠폰만_빼라면_포인트는_그대로_채운다() -> None:
    lines = [OrderLine(P1, 40000, 1, frozenset({3}))]
    coupons = [_coupon(7, 3, "FIXED_AMOUNT", 3000)]

    result = plan(lines, coupons, point_balance=5000, use_coupon=False)

    assert result.coupon is None and result.coupon_declined
    assert result.use_point == 5000 and not result.points_declined


@pytest.mark.parametrize(
    ("message", "coupon", "point"),
    [
        ("쿠폰 말고 포인트만 써서 주문해 줘", True, False),
        ("포인트 빼고 주문해 줘", False, True),
        ("쿠폰 없이 주문해 줘", True, False),
        ("포인트 사용 안 함", False, True),
        ("쿠폰 적용해서 주문해 줘", False, False),
        ("1번 주문해 줘", False, False),
    ],
)
def test_쓰지_말라는_할인만_알아본다(message: str, coupon: bool, point: bool) -> None:
    ctx = _ctx(message)
    assert evidence.declines_discount(ctx, "쿠폰") is coupon
    assert evidence.declines_discount(ctx, "포인트") is point


def test_카드가_한_권이면_이거를_지금_보는_책으로_불러도_그_카드를_담는다() -> None:
    # 11/10 dev: 모델이 "이거"를 지금 보는 책(focused)으로 불러 "지금 보고 있는 책이 없어요"로 끝났다
    _, data, _ = _card_turn(
        "이거 주문해줘 포인트 적용하지 말고",
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "focused"}})),
            ai_text("담았어요."),
        ],
        turns=_SHOWN,
    )

    assert [(a.op, a.isbn) for a in data.actions] == [("set", CARD_A)]
    assert data.checkout == Checkout(owned_coupon_id=None, use_point=0)


# --- 직전 말이 여러 권을 짚었을 때의 "네", "둘 다" (11/10 dev) ---
# "카드 1번과 2번을 담을까요?" → "네" → "어느 책을 담을까요?" → "둘다" → 같은 물음이 되풀이됐다

_BOTH_PROPOSED = f"카드 1번 《{_CARD_TITLE}》과 카드 2번 《자바 첫걸음》을 담을까요?"
_BOTH_CHOICE = (
    f"카드 1번 《{_CARD_TITLE}》과 카드 2번 《자바 첫걸음》 중 어느 책을 담을까요?"
)
_ADD_BOTH = [
    ai_calls(
        (ToolName.CART_ADD, {"book": card(1)}),
        (ToolName.CART_ADD, {"book": card(2)}),
    ),
    ai_text("담았어요."),
]


@pytest.mark.parametrize(
    ("last", "message"),
    [
        (_BOTH_PROPOSED, "네"),
        (_BOTH_PROPOSED, "둘다"),
        (_BOTH_CHOICE, "둘다"),
        (_BOTH_CHOICE, "둘 다요"),
        (_BOTH_CHOICE, "모두 담아줘"),
    ],
)
def test_직전_말이_두_권을_짚었으면_동의나_둘_다는_두_권_다다(
    last: str, message: str
) -> None:
    _, data, _ = _card_turn(
        message,
        _ADD_BOTH,
        turns=[("user", "1번 2번 담아"), ("assistant", last)],
        cards=3,
    )

    assert {a.isbn for a in data.actions} == {CARD_A, CARD_B}


@pytest.mark.parametrize(
    ("last", "message"),
    [
        (_BOTH_CHOICE, "네"),  # 고르라는 물음에 "네"는 어느 책인지 모른다
        (_BOTH_PROPOSED, "네 근데 3번으로요"),
        (_BOTH_PROPOSED, "아니요"),
    ],
)
def test_직전_말이_두_권을_짚어도_전부를_뜻하지_않는_말이면_담지_않는다(
    last: str, message: str
) -> None:
    run, _, _ = _card_turn(
        message,
        [*_ADD_BOTH[:1], ai_text("어느 책일까요?")],
        turns=[("user", "1번 2번 담아"), ("assistant", last)],
        cards=3,
    )

    assert run.actions == []


def test_주문_기능이_켜져_있으면_포인트_빼고는_책을_빼라는_말이_아니라고_알려_준다() -> (
    None
):
    from app.agent import prompts

    assert "책을 빼라는 말이 아니다" in prompts.system_prompt(search=True, order=True)
    assert "책을 빼라는 말이 아니다" not in prompts.system_prompt(
        search=False, order=False
    )


# --- 주문해 달라는데 주문 화면으로 보낼 책이 없을 때 (11/10 dev) ---


def test_이미_담긴_책을_주문해_달라면_그_책만_같은_수량으로_다시_담아_주문_화면을_연다() -> (
    None
):
    # 예전(#649)에는 "이미 장바구니에 있는 책은 주문 화면으로 보내지 못해요"였다. 같은 수량으로 다시 담으면 연다(#678)
    _, data, _ = _card_turn(
        "1번 주문해줘",
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("그대로 뒀어요.")],
        cart=[
            {
                "isbn": CARD_A,
                "title": _CARD_TITLE,
                "quantity": 1,
                "price": 28000,
                "available": True,
            },
            {
                "isbn": P1,
                "title": "다른 장바구니 책",
                "quantity": 1,
                "price": 10000,
                "available": True,
            },
        ],
    )

    assert [(a.op, a.isbn, a.quantity) for a in data.actions] == [("set", CARD_A, 1)]
    assert data.checkout is not None
    assert [(b.action, b.target) for b in data.buttons] == [("navigate", ORDER_SCREEN)]
    assert reply_module.ORDER_FROM_CART not in data.reply
    assert "다른 장바구니 책" not in data.reply  # 짚은 책만 올린다
    # 한 권이면 "모두 1권"이라 하지 않는다(#683 리뷰)
    assert (
        f"장바구니에 담긴 《{_CARD_TITLE}》 1권으로 주문 화면을 열어 둘게요."
        in data.reply
    )


def test_짚은_책만_주문_화면에_올렸으면_장바구니의_다른_품절_책은_알리지_않는다() -> (
    None
):
    # 장바구니 전체를 올린 턴만 "살 수 없어서 넣지 않았어요"라고 한다. "1번 주문해줘"는 1번만 말한 것이다(#683 리뷰)
    _, data, _ = _card_turn(
        "1번 주문해줘",
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("그대로 뒀어요.")],
        cart=[
            {
                "isbn": CARD_A,
                "title": _CARD_TITLE,
                "quantity": 1,
                "price": 28000,
                "available": True,
            },
            {
                "isbn": P1,
                "title": "판매 중지된 책",
                "quantity": 1,
                "price": 10000,
                "available": False,
            },
        ],
    )

    assert [(a.op, a.isbn, a.quantity) for a in data.actions] == [("set", CARD_A, 1)]
    assert "판매 중지된 책" not in data.reply
    assert "살 수 없어서" not in data.reply


def test_이미_담긴_책이_살_수_없으면_주문_화면을_열지_않고_장바구니로_보낸다() -> None:
    _, data, _ = _card_turn(
        "1번 주문해줘",
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("그대로 뒀어요.")],
        cart=[
            {
                "isbn": CARD_A,
                "title": _CARD_TITLE,
                "quantity": 1,
                "price": 28000,
                "available": False,
            }
        ],
    )

    assert data.checkout is None
    assert reply_module.ORDER_FROM_CART in data.reply
    assert [(b.action, b.target) for b in data.buttons] == [("navigate", "CART-001")]


def test_주문_한_마디에_쇼핑이_아니라고_해도_담아_둔_책이_있으면_주문_화면을_연다() -> (
    None
):
    run, data, _ = _card_turn("주문", [ai_text("NOT_SHOPPING")], cart=CART_TWO)

    assert run.not_shopping is False
    assert data.not_shopping is False
    assert sorted(a.isbn for a in data.actions) == sorted([P1, P2])
    assert [(b.action, b.target) for b in data.buttons] == [("navigate", ORDER_SCREEN)]


def test_담기만_해_달라면_이미_담긴_책에_주문_안내를_붙이지_않는다() -> None:
    _, data, _ = _card_turn(
        "1번 담아줘",
        [ai_calls((ToolName.CART_ADD, {"book": card(1)})), ai_text("그대로 뒀어요.")],
        cart=[
            {
                "isbn": CARD_A,
                "title": _CARD_TITLE,
                "quantity": 1,
                "price": 28000,
                "available": True,
            }
        ],
    )

    assert reply_module.ORDER_FROM_CART not in data.reply


@pytest.mark.parametrize("message", ["주문", "주문이요", "결제할게요", "주문!"])
def test_주문_한_마디에_쇼핑이_아니라고_해도_추천으로_넘기지_않는다(
    message: str,
) -> None:
    run, data, _ = _card_turn(message, [ai_text("NOT_SHOPPING")])

    assert run.not_shopping
    assert data.not_shopping is False
    assert data.reply == reply_module.ORDER_FROM_CART
    assert [b.target for b in data.buttons] == ["CART-001"]


@pytest.mark.parametrize(
    "message", ["주문 내역 보여줘", "추천해줘", "주문한 책 언제 와?"]
)
def test_주문하겠다는_말이_아니면_쇼핑이_아님은_그대로다(message: str) -> None:
    _, data, _ = _card_turn(message, [ai_text("NOT_SHOPPING")])

    assert data.not_shopping is True


@pytest.mark.parametrize(
    "message",
    [
        "선물로 책 한 권 사고 싶은데 뭐가 좋을까요",
        "책 하나 구매하고 싶은데 뭘 사야 할지 모르겠어요",
    ],
)
def test_주문_낱말이_든_추천_요청은_쇼핑이_아니라면_추천으로_돌아간다(
    message: str,
) -> None:
    # 주문 기능을 켜면 문지기가 이런 말을 쇼핑으로 보낸다. 에이전트가 쇼핑이 아니라고 하면 장바구니 안내로 끝내지
    # 않고 추천으로 돌려보낸다(#649 리뷰)
    _, data, _ = _card_turn(message, [ai_text("NOT_SHOPPING")])

    assert data.not_shopping is True
    assert data.reply != reply_module.ORDER_FROM_CART


def test_기능이_꺼져_있으면_주문_한_마디도_예전처럼_쇼핑이_아니다() -> None:
    _, data, _ = _card_turn("주문", [ai_text("NOT_SHOPPING")], checkout=False)

    assert data.not_shopping is True


# --- 앞 턴에 담아 둔 책으로 주문 화면 열기 (#678) ---


@pytest.mark.parametrize(
    ("message", "orders"),
    [
        ("이제 주문할게", True),
        ("이제 결제해 줘", True),
        ("장바구니에 있는 거 다 주문해 줘", True),
        ("쿠폰 써서 주문해 줘", True),
        ("네 주문할게요 감사합니다", True),
        ("리눅스 책 바로 주문할게요", False),  # 새 책을 말했다. 모델이 찾는다
        ("1번 주문해줘", False),
        ("그거 주문할게", False),
        ("데미안만 주문할게", False),
        ("주문할 때 쿠폰 써?", False),
        ("결제하려면 어떻게 해", False),
        ("그럼 살게", False),  # 보인 책 하나를 사겠다는 말일 때가 많다
        ("주문 취소해줘", False),
    ],
)
def test_책을_말하지_않은_주문_말만_장바구니_주문이다(
    message: str, orders: bool
) -> None:
    assert evidence.orders_cart(_ctx(message)) is orders


CART_TWO = [
    {
        "isbn": P1,
        "title": "미우의 과학 모험",
        "price": 10000,
        "quantity": 1,
        "available": True,
    },
    {
        "isbn": P2,
        "title": "과학 만화 일기",
        "price": 9500,
        "quantity": 2,
        "available": True,
    },
]


def test_담아_둔_책으로_주문하겠다면_같은_수량으로_다시_담아_주문_화면을_연다() -> None:
    # 11/11 dev 시험: 앞 턴에 담고 "이제 주문할게"면 주문 화면이 한 번도 안 열렸다(8번 중 0번)
    _, data, _ = _turn(
        "이제 주문할게",
        [
            ai_calls((ToolName.CART_VIEW, {})),
            ai_calls((ToolName.UI_GUIDE, {"target": "ORDER-LIST-001"})),
            ai_text("결제는 제가 할 수 없어요. 주문내역에서 확인해 주세요."),
        ],
        cart=CART_TWO,
        point_balance=3000,
    )

    # FE 는 이 턴의 장바구니 변경에 set 으로 든 책만 주문 화면에 올린다. 수량은 그대로다
    assert sorted((a.op, a.isbn, a.quantity) for a in data.actions) == [
        ("set", P1, 1),
        ("set", P2, 2),
    ]
    assert data.checkout == Checkout(owned_coupon_id=None, use_point=3000)
    assert [(b.action, b.target) for b in data.buttons] == [("navigate", ORDER_SCREEN)]
    assert (
        "《미우의 과학 모험》, 《과학 만화 일기》, 모두 3권으로 주문 화면을 열어 둘게요."
        in data.reply
    )
    assert "포인트 3,000원을 적용해 둘게요" in data.reply
    assert "결제는 제가 할 수 없어요" not in data.reply
    assert "주문내역" not in data.reply


def test_살_수_없는_줄은_주문_화면에_넣지_않고_알린다() -> None:
    cart = [CART_TWO[0], {**CART_TWO[1], "available": False}]
    _, data, _ = _turn(
        "이제 결제해 줘",
        [ai_text("장바구니에서 결제해 주세요.")],
        cart=cart,
    )

    assert [(a.op, a.isbn, a.quantity) for a in data.actions] == [("set", P1, 1)]
    assert (
        "《과학 만화 일기》는 지금 살 수 없어서 주문 화면에 넣지 않았어요."
        in data.reply
    )


def test_살_수_있는_줄이_없으면_장바구니를_다시_담지_않는다() -> None:
    cart = [{**line, "available": False} for line in CART_TWO]
    _, data, _ = _turn(
        "이제 주문할게", [ai_text("지금은 살 수 있는 책이 없어요.")], cart=cart
    )

    assert data.actions == []
    assert data.checkout is None
    assert data.reply == "지금은 살 수 있는 책이 없어요."


def test_모델이_책을_찾았으면_장바구니를_주문_화면에_올리지_않는다() -> None:
    # 말에 남은 낱말이 없어도 모델이 책을 찾거나 담으려 했으면 사용자는 어떤 책을 말한 것이다
    _, data, _ = _turn(
        "이제 주문할게",
        [
            ai_calls((ToolName.BOOK_SEARCH, {"query": "개발 입문서"})),
            ai_text("어느 책을 주문할까요?"),
        ],
        cart=CART_TWO,
    )

    assert data.actions == []
    assert data.checkout is None


def test_장바구니가_비었으면_주문_화면을_열지_않는다() -> None:
    _, data, _ = _turn("이제 주문할게", [ai_text("장바구니가 비어 있어요.")])

    assert data.actions == []
    assert all(b.target != ORDER_SCREEN for b in data.buttons)


def test_주문_기능이_꺼져_있으면_장바구니를_다시_담지_않는다() -> None:
    _, data, _ = _turn(
        "이제 주문할게",
        [ai_text("주문은 장바구니에서 해 주세요.")],
        checkout=False,
        finder=False,
        cart=CART_TWO,
    )

    assert data.actions == []
    assert data.checkout is None


def test_모델이_장바구니_책을_다시_담으려다_막혀도_주문_화면을_연다() -> None:
    # 실제 모델(gpt-5.4-mini)이 "이제 주문할게"에 카드에 보이는 장바구니 책 두 권을 다시 담으려다 근거 부족으로
    # 막혀 "주문내역에서 확인하실 수 있어요"라고 답했다(3번 중 2번)
    _, data, _ = _turn(
        "이제 주문할게",
        [
            ai_calls(
                (ToolName.CART_ADD, {"book": {"source": "cart", "n": 1}}),
                (ToolName.CART_ADD, {"book": {"source": "cart", "n": 2}}),
            ),
            ai_text("주문내역에서 확인하실 수 있어요."),
        ],
        cart=CART_TWO,
    )

    assert sorted((a.op, a.isbn, a.quantity) for a in data.actions) == [
        ("set", P1, 1),
        ("set", P2, 2),
    ]
    assert [(b.action, b.target) for b in data.buttons] == [("navigate", ORDER_SCREEN)]
    assert "주문내역" not in data.reply


TWO_CARDS = {
    # 카드가 두 장이라 "이제 주문할게"로는 어느 카드인지 몰라 담기가 막힌다
    "context_cards": [
        {"isbn": CARD_A, "rank": 1, "match_score": 80},
        {"isbn": P3, "rank": 2, "match_score": 70},
    ],
    "offers": [
        {"isbn": CARD_A, "title": "카드 책", "price": 12000, "in_stock": True},
        {"isbn": P3, "title": "다른 카드 책", "price": 15000, "in_stock": True},
    ],
}


def test_제안하지_않은_카드_책을_담으려다_막혀도_장바구니로_주문_화면을_연다() -> None:
    # 실제 모델이 "이제 주문할게"에 장바구니에 없는 카드 1번을 담으려다 막혔다. 사용자는 책을 말하지 않았다
    _, data, _ = _turn(
        "이제 주문할게",
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": 1}})),
            ai_text("주문내역에서 확인해 주세요."),
        ],
        cart=CART_TWO,
        **TWO_CARDS,
    )

    assert sorted(a.isbn for a in data.actions) == sorted([P1, P2])
    assert [(b.action, b.target) for b in data.buttons] == [("navigate", ORDER_SCREEN)]


def test_한_권을_제안한_뒤의_주문하겠다는_말은_그_책만_주문_화면에_올린다() -> None:
    # "카드 1번 《카드 책》을 담을까요?" 뒤의 "이제 주문할게"는 그 책을 주문하겠다는 말이다. 담기가 근거를 얻어 성공하고,
    # 장바구니에 있던 책은 다시 담지 않는다
    _, data, _ = _turn(
        "이제 주문할게",
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": 1}})),
            ai_text("담았어요."),
        ],
        cart=CART_TWO,
        recent_turns=[
            {"role": "user", "text": "1번 어때?"},
            {"role": "assistant", "text": "카드 1번 《카드 책》을 담을까요?"},
        ],
        **TWO_CARDS,
    )

    assert [(a.op, a.isbn, a.quantity) for a in data.actions] == [("set", CARD_A, 1)]
    assert [(b.action, b.target) for b in data.buttons] == [("navigate", ORDER_SCREEN)]


def test_직전에_제안한_책을_담으려다_막혔으면_장바구니를_주문_화면에_올리지_않는다() -> (
    None
):
    # 두 권을 짚어 물은 뒤라 어느 책인지 몰라 담기가 막혔다. 사용자는 그 질문에 답한 것이라 장바구니 전체가 아니다
    _, data, _ = _turn(
        "이제 주문할게",
        [
            ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": 1}})),
            ai_text("어느 책을 주문할까요?"),
        ],
        cart=CART_TWO,
        recent_turns=[
            {"role": "user", "text": "둘 다 괜찮네"},
            {
                "role": "assistant",
                "text": "카드 1번 《카드 책》과 카드 2번 《다른 카드 책》 중 어느 책을 담을까요?",
            },
        ],
        **TWO_CARDS,
    )

    assert data.actions == []
    assert data.checkout is None
