"""⑦ 한 묶음의 쓰기를 실행 전에 같이 따져 보고, 하나라도 안 되면 아무것도 BE 에 보내지 않는다(#488).

LLM 이 한 번에 여러 쓰기를 부르면("1번이랑 2번 담아줘") 코드는 하나씩 BE 에 보낸다. 장바구니가 27권일 때
새 책 4권을 담으라고 하면 3권만 담기고 마지막 1권이 한도로 거절된다. 이미 담긴 3권은 되돌리지 못한다.
BE 가 거절하는 일 가운데 AI 서버가 미리 알 수 있는 것(수량 범위, 판매 중지·재고 없음, 30권 한도, 담기지 않은
책)을 장바구니를 읽은 값으로 순서대로 따져 보고, 하나라도 걸리면 그 묶음의 쓰기는 모두 실행하지 않는다.

- 쓰기가 하나뿐이면 따지지 않는다. BE 의 거절은 장바구니를 바꾸지 않아서 하나씩 보내도 같다
- BE 까지 가지 못하는 호출(책을 못 가리킴, 근거 없음, AI 가 고른 책)은 묶음에 넣지 않는다. 그 책만 되묻는다
- 이미 담긴 책을 담는 호출은 장바구니를 바꾸지 않고 알리기만 해서(tools._add) 다른 쓰기를 막지 않고, 자리도
  차지하지 않는다
- 따져 보는 순서는 호출 순서다. 먼저 빼면 그 자리가 난다(30권에서 하나 빼고 새 책 담기)
- 읽기에 실패하면 미리 걸러 낼 수 없으니 그대로 하나씩 실행한다. BE 가 판단한다

한계: 미리 아는 이유만 막는다. 따져 본 뒤 실행하는 사이에 장바구니가 바뀌거나, 수량이 재고 안에 드는지(재고
수량은 안 받는다)는 BE 가 판단해서 일부만 적용될 수 있다. 서로 다른 반복 단계에 걸친 호출은 한 묶음이 아니다.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from app.agent.errors import ToolErrorCode
from app.agent.reference import TurnContext
from app.agent.schemas import ToolCall, ToolName
from app.agent.tools import WriteIntent, write_intent
from app.gateway.commerce import (
    MAX_CART_ITEMS,
    MAX_QUANTITY,
    MIN_QUANTITY,
    BookOffer,
    CommerceError,
    CommerceErrorCode,
    CommerceGateway,
)


@dataclass(frozen=True)
class Withheld:
    """실행하지 않기로 한 쓰기와 이유."""

    intent: WriteIntent
    code: str

    def call(self) -> ToolCall:
        """실행하지 않은 호출의 기록. arguments 는 코드가 채운 값이라 마무리 단계가 결과 문장을 만들 수 있다."""
        return ToolCall(
            name=self.intent.name,
            arguments=self.intent.arguments,
            result={"ok": False, "error": self.code},
            grounded=True,
        )


def _stock(offers: dict[str, BookOffer], isbn: str) -> str | None:
    # BE 가 모르거나 판매를 멈춘 책은 가격·재고 조회에서 빠진다
    offer = offers.get(isbn)
    if offer is None:
        return CommerceErrorCode.PRODUCT_NOT_FOUND.value
    if not offer.in_stock:
        return CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK.value
    return None


def _quantity(quantity: int | None) -> str | None:
    if quantity is None or not MIN_QUANTITY <= quantity <= MAX_QUANTITY:
        return CommerceErrorCode.INVALID_CART_ITEM_QUANTITY.value
    return None


def _simulate(
    intent: WriteIntent,
    cart: dict[str, int],
    offers: dict[str, BookOffer],
    expected: Callable[[str], int | None],
) -> tuple[str | None, bool]:
    """쓰기 하나를 가상의 장바구니에 적용해 본다. (거절 이유 또는 None, 장바구니를 안 바꾸는 호출인가).

    이유를 따지는 순서는 가짜 BE(commerce_fake)와 같다: 담기는 수량 → 재고 → 한도, 수량 변경은 담긴 책 →
    본 수량과 같은가 → 수량 → 재고. expected 는 수량 변경이 BE 에 실어 보낼 "본 수량"이다.
    """
    isbn = intent.isbn
    if intent.name == ToolName.CART_ADD:
        if isbn in cart:
            return None, True
        code = _quantity(intent.quantity) or _stock(offers, isbn)
        if code is None and len(cart) >= MAX_CART_ITEMS:
            code = CommerceErrorCode.CART_ITEM_LIMIT_EXCEEDED.value
        if code is None:
            assert intent.quantity is not None  # _quantity 가 보장한다
            cart[isbn] = intent.quantity
        return code, False

    if isbn not in cart:
        return CommerceErrorCode.CART_ITEM_NOT_FOUND.value, False

    if intent.name == ToolName.CART_REMOVE:
        del cart[isbn]
        return None, False

    seen = expected(isbn)
    if seen is not None and cart[isbn] != seen:
        return CommerceErrorCode.CART_QUANTITY_CHANGED.value, False
    code = _quantity(intent.quantity) or _stock(offers, isbn)
    if code is None:
        assert intent.quantity is not None
        cart[isbn] = intent.quantity
    return code, False


async def plan_writes(
    ctx: TurnContext,
    gateway: CommerceGateway,
    calls: Sequence[tuple[int, ToolName, Any]],
) -> dict[int, Withheld]:
    """한 묶음의 도구 호출(위치, 이름, 인자) 가운데 실행하지 않을 쓰기. 호출 위치로 찾는다.

    이전 묶음에서 이미 막았으면(ctx.writes_halted) 이번 턴의 쓰기는 모두 막는다.
    """
    intents = {
        index: intent
        for index, name, raw_args in calls
        if (intent := write_intent(ctx, name, raw_args)) is not None
    }

    if ctx.writes_halted:
        return {
            index: Withheld(intent, ToolErrorCode.NOT_APPLIED_WITH_OTHERS.value)
            for index, intent in intents.items()
        }
    if len(intents) < 2:
        return {}

    try:
        lines = await gateway.get_cart(ctx.user_id)
        offered = list(
            dict.fromkeys(
                intent.isbn
                for intent in intents.values()
                if intent.name != ToolName.CART_REMOVE
            )
        )
        offers = (
            {o.isbn: o for o in await gateway.get_offers(offered)} if offered else {}
        )
    except CommerceError:
        return {}

    cart = {line.isbn: line.quantity for line in lines}
    # 묶음 안의 앞선 쓰기가 만든 수량. 실제 실행에서는 tools 가 ctx.applied 에 적는 것과 같다
    made: dict[str, int | None] = {}

    def expected(isbn: str) -> int | None:
        return made[isbn] if isbn in made else ctx.expected_quantity(isbn)

    rejected: dict[int, str] = {}
    unchanged: set[int] = set()
    for index, intent in intents.items():
        code, noop = _simulate(intent, cart, offers, expected)
        if code is not None:
            rejected[index] = code
        elif noop:
            unchanged.add(index)
        else:
            made[intent.isbn] = (
                None if intent.name == ToolName.CART_REMOVE else intent.quantity
            )

    # 함께 막을 쓰기가 없으면 따질 필요가 없다. 거절될 호출은 BE 가 직접 거절한다
    siblings = [i for i in intents if i not in rejected and i not in unchanged]
    if not rejected or not siblings:
        return {}

    ctx.writes_halted = True
    withheld = {i: Withheld(intents[i], code) for i, code in rejected.items()}
    for index in siblings:
        withheld[index] = Withheld(
            intents[index], ToolErrorCode.NOT_APPLIED_WITH_OTHERS.value
        )
    return withheld
