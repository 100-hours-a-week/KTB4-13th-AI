"""⑦ LLM 이 부르는 도구를 실행한다(#354). BE 에 시키는 5개만 있다.

순서는 항상 같다: 실행해도 되는지 검사(guard) → 인자 확인 → 책을 ISBN 으로 확정(reference) → BE 호출.
어느 단계에서 막히든 예외를 던지지 않고 결과에 `{"ok": false, "error": "<코드>"}` 로 남긴다(명세 ⑦).
결과에는 코드 이름만 적고 사용자 발화는 싣지 않는다.

LLM 은 책을 `BookRef`(카드 몇 번, 장바구니 몇 번째, 지금 보는 책)로만 가리키고, ISBN 과 user_id 는 여기서
채운다. 기록하는 arguments 도 코드가 채운 값이다. selection.pick·book.compare·ui.guide 는 뒤 단계에서 만든다.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent import guard
from app.agent.errors import RefError, ToolErrorCode
from app.agent.reference import BookRef, TurnContext, resolve
from app.agent.schemas import MAX_CONTEXT_CARDS, ToolCall, ToolName
from app.gateway.commerce import CartLine, CommerceError, CommerceGateway


class _Args(BaseModel):
    # 모르는 칸을 막는다. LLM 이 isbn 이나 user_id 를 인자로 끼워 넣어도 BE 까지 가지 않는다.
    model_config = ConfigDict(extra="forbid", strict=True)


class ViewArgs(_Args):
    pass


class AddArgs(_Args):
    book: BookRef = Field(description="담을 책")
    # 범위(1~500)와 재고는 여기서 막지 않는다. BE 가 판단하고, 거절하면 그 이유를 결과에 적는다.
    quantity: int = Field(default=1, description="담을 수량. 말하지 않았으면 1")


class UpdateArgs(_Args):
    book: BookRef = Field(description="수량을 바꿀 책(이미 담긴 책)")
    quantity: int = Field(description="바꿀 수량. 0은 안 된다")


class RemoveArgs(_Args):
    book: BookRef = Field(description="장바구니에서 뺄 책")


class OfferArgs(_Args):
    books: list[BookRef] = Field(
        min_length=1,
        max_length=MAX_CONTEXT_CARDS,
        description="가격과 재고를 조회할 책들(1~10개)",
    )


# LLM 용 도구 설명(스키마)을 만드는 6단계가 이 인자 모델에서 만든다
ARG_MODELS: dict[ToolName, type[_Args]] = {
    ToolName.CART_VIEW: ViewArgs,
    ToolName.CART_ADD: AddArgs,
    ToolName.CART_UPDATE: UpdateArgs,
    ToolName.CART_REMOVE: RemoveArgs,
    ToolName.BOOK_OFFER: OfferArgs,
}

_Done = tuple[dict[str, Any], dict[str, Any]]  # (기록할 arguments, 결과)
_Handler = Callable[[TurnContext, CommerceGateway, Any], Awaitable[_Done]]


def _line(line: CartLine) -> dict[str, Any]:
    return {
        "isbn": line.isbn,
        "title": line.title,
        "price": line.price,
        "quantity": line.quantity,
        "available": line.available,
    }


def _fail(code: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": code, **extra}


async def _write(arguments: dict[str, Any], call: Awaitable[None]) -> _Done:
    """쓰기 호출을 하고 BE 가 거절하면 이유를 결과에 적는다. 장바구니는 바뀌지 않았다."""
    try:
        await call
    except CommerceError as e:
        return arguments, _fail(e.code.value)
    return arguments, {"ok": True}


def _total(lines: list[CartLine]) -> int:
    """구매할 수 있는 책의 합계. 판매 중지(가격 없음)나 재고가 모자란 책은 뺀다.

    LLM 이 더하면 틀리고, 틀린 숫자는 답변의 숫자 검사로도 못 걸러서 코드가 계산해 결과에 싣는다.
    """
    return sum(
        line.price * line.quantity
        for line in lines
        if line.available and line.price is not None
    )


async def _view(ctx: TurnContext, gateway: CommerceGateway, args: ViewArgs) -> _Done:
    lines = await gateway.get_cart(ctx.user_id)
    # 번호를 푸는 목록(cart_snapshot)은 여기서 바꾸지 않는다. 같은 묶음의 "장바구니 n번째"는 LLM 이 이 결과를
    # 받기 전에 본 목록으로 풀어야 한다. 묶음이 끝나면 부르는 쪽이 ctx.commit_cart_view() 로 올린다.
    ctx.pending_cart = lines
    return {}, {"items": [_line(line) for line in lines], "total": _total(lines)}


async def _add(ctx: TurnContext, gateway: CommerceGateway, args: AddArgs) -> _Done:
    isbn = resolve(ctx, args.book)
    arguments = {"isbn": isbn, "quantity": args.quantity}
    # BE 의 담기는 "요청 수량으로 바꾸기"라서 이미 2권 담긴 책을 담으면 1권이 된다. LLM 이 조회를
    # 건너뛰고 바로 담으라고 해도 막히도록, 담기 전에 BE 에서 다시 읽어 확인한다.
    cart = await gateway.get_cart(ctx.user_id)
    existing = next((line for line in cart if line.isbn == isbn), None)
    if existing is not None:
        failure = _fail(
            ToolErrorCode.ALREADY_IN_CART.value,
            title=existing.title,
            quantity=existing.quantity,
        )
        return arguments, failure
    return await _write(arguments, gateway.add_item(ctx.user_id, isbn, args.quantity))


async def _update(
    ctx: TurnContext, gateway: CommerceGateway, args: UpdateArgs
) -> _Done:
    isbn = resolve(ctx, args.book)
    arguments = {"isbn": isbn, "quantity": args.quantity}
    return await _write(
        arguments, gateway.set_quantity(ctx.user_id, isbn, args.quantity)
    )


async def _remove(
    ctx: TurnContext, gateway: CommerceGateway, args: RemoveArgs
) -> _Done:
    isbn = resolve(ctx, args.book)
    return await _write({"isbn": isbn}, gateway.remove_item(ctx.user_id, isbn))


async def _offer(ctx: TurnContext, gateway: CommerceGateway, args: OfferArgs) -> _Done:
    # 같은 책을 두 번 가리켜도 한 번만 묻는다. 받은 순서는 지킨다
    isbns = list(dict.fromkeys(resolve(ctx, ref) for ref in args.books))
    offers = await gateway.get_offers(isbns)
    result = {
        "offers": [
            {
                "isbn": o.isbn,
                "title": o.title,
                "price": o.price,
                "in_stock": o.in_stock,
            }
            for o in offers
        ]
    }
    return {"isbns": isbns}, result


_HANDLERS: dict[ToolName, _Handler] = {
    ToolName.CART_VIEW: _view,
    ToolName.CART_ADD: _add,
    ToolName.CART_UPDATE: _update,
    ToolName.CART_REMOVE: _remove,
    ToolName.BOOK_OFFER: _offer,
}


def _failed(
    name: ToolName, arguments: dict[str, Any], code: ToolErrorCode | str
) -> ToolCall:
    """실행하지 못한 호출의 기록. arguments 는 LLM 이 준 그대로다(책을 풀기 전이라 ISBN 이 없다)."""
    error = code.value if isinstance(code, ToolErrorCode) else code
    return ToolCall(name=name, arguments=arguments, result=_fail(error), grounded=True)


async def execute(
    ctx: TurnContext, gateway: CommerceGateway, name: ToolName, raw_args: Any
) -> ToolCall:
    """도구 하나를 실행하고 기록을 돌려준다. 막히거나 BE 가 거절해도 예외를 던지지 않는다.

    grounded 는 이 결과가 실제로 실행해서 얻은 값이라는 뜻으로 true 로 둔다. 답변이 이 결과를
    뒷받침하는지는 마무리 단계(7단계)가 판단해서 바꾼다.
    """
    given = raw_args if isinstance(raw_args, dict) else {}

    blocked = guard.check(ctx, name)
    if blocked is not None:
        return _failed(name, given, blocked)

    handler = _HANDLERS.get(name)
    if handler is None:
        return _failed(name, given, ToolErrorCode.TOOL_NOT_IMPLEMENTED)

    try:
        args = ARG_MODELS[name].model_validate(raw_args)
    except ValidationError:
        return _failed(name, given, ToolErrorCode.INVALID_ARGUMENTS)

    try:
        arguments, result = await handler(ctx, gateway, args)
    except RefError as e:
        return _failed(name, given, e.code)
    except CommerceError as e:  # 읽기 호출의 거절. 쓰기는 _write 가 이미 처리했다
        return _failed(name, given, e.code.value)
    return ToolCall(name=name, arguments=arguments, result=result, grounded=True)
