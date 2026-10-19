"""⑦ LLM 이 부르는 도구를 실행한다(#354, #358, #363). 장바구니·가격을 다루는 5개와 그 밖의 3개가 있다.

장바구니·가격 도구는 요청으로 받은 장바구니의 복사본을 읽고 바꾼다. BE 는 부르지 않고, 바뀐 것은 턴이 끝날 때
actions 로 돌려준다(#489).

AI 서버 안의 도구는 화면 안내(ui.guide), 조합 고르기(selection.pick, 코드 계산), 비교(book.compare,
소개글을 읽는 별도 호출)다.

순서는 항상 같다: 실행해도 되는지 검사(guard) → 인자 확인 → 책을 ISBN 으로 확정(reference) → 쓰기면 사용자의 말이
그 책의 근거인지 확인(evidence, 막히면 모델이 짚어 물을 책을 결과에 적는다) → 장바구니 호출.
어느 단계에서 막히든 예외를 던지지 않고 결과에 `{"ok": false, "error": "<코드>"}` 로 남긴다(명세 ⑦).
결과에는 코드 이름만 적고 사용자 발화는 싣지 않는다.

LLM 은 책을 `BookRef`(카드 몇 번, 장바구니 몇 번째, 지금 보는 책)로만 가리키고, ISBN 과 user_id 는 여기서
채운다. 기록하는 arguments 도 코드가 채운 값이다.
"""

from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent import evidence, guard, selection
from app.agent.compare import CompareError, Comparer
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
    # 범위(1~500)와 재고는 여기서 막지 않는다. 장바구니가 판단하고, 거절하면 그 이유를 결과에 적는다.
    # 말하지 않았으면 비운다. 이미 담긴 책은 수량을 말했을 때만 그 수량으로 바꾼다(tools._add)
    quantity: int | None = Field(
        default=None, description="사용자가 말한 수량. 말하지 않았으면 넣지 않는다(1권)"
    )


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


class PickArgs(_Args):
    budget_total: int = Field(
        ge=1, description="예산(원). 고른 책의 가격 합계가 이 값 이하여야 한다"
    )
    count: int = Field(
        ge=1,
        le=MAX_CONTEXT_CARDS,
        description="고를 책의 권수. 같은 책은 한 번만 고른다",
    )


class CompareArgs(_Args):
    books: list[BookRef] = Field(
        min_length=2, max_length=3, description="소개글로 견줄 책들(2~3권)"
    )


# ui.guide 로 안내할 수 있는 화면(FE 화면 식별자 → 버튼 글자). 목록에 없는 화면은 받지 않는다
UI_TARGETS: dict[str, str] = {
    "CART-001": "장바구니 보기",
    "ORDER-LIST-001": "주문내역",
}


class UiGuideArgs(_Args):
    target: Literal["CART-001", "ORDER-LIST-001"] = Field(
        description=(
            "이동을 안내할 화면. CART-001=장바구니, "
            "ORDER-LIST-001=주문내역(주문한 책의 배송 상태도 여기서 본다)"
        )
    )


# LLM 용 도구 설명(스키마)을 만드는 6단계가 이 인자 모델에서 만든다
ARG_MODELS: dict[ToolName, type[_Args]] = {
    ToolName.CART_VIEW: ViewArgs,
    ToolName.CART_ADD: AddArgs,
    ToolName.CART_UPDATE: UpdateArgs,
    ToolName.CART_REMOVE: RemoveArgs,
    ToolName.BOOK_OFFER: OfferArgs,
    ToolName.SELECTION_PICK: PickArgs,
    ToolName.BOOK_COMPARE: CompareArgs,
    ToolName.UI_GUIDE: UiGuideArgs,
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


def view_result(lines: list[CartLine]) -> dict[str, Any]:
    """cart.view 의 결과. 한 묶음을 되돌린 뒤 그 묶음의 조회 결과를 다시 만들 때도 쓴다(graph)."""
    return {"items": [_line(line) for line in lines], "total": _total(lines)}


async def _view(ctx: TurnContext, gateway: CommerceGateway, args: ViewArgs) -> _Done:
    lines = await gateway.get_cart(ctx.user_id)
    # 번호를 푸는 목록(cart_snapshot)은 여기서 바꾸지 않는다. 같은 묶음의 "장바구니 n번째"는 LLM 이 이 결과를
    # 받기 전에 본 목록으로 풀어야 한다. 묶음이 끝나면 부르는 쪽이 ctx.commit_cart_view() 로 올린다.
    ctx.pending_cart = lines
    return {}, view_result(lines)


async def _unstated(
    ctx: TurnContext, gateway: CommerceGateway, ref: BookRef, isbn: str
) -> dict[str, Any] | None:
    """사용자의 말에 이 책의 근거가 없으면 막는 결과, 있으면 None.

    최근 대화가 있으면 대화에서 이야기한 책을 알아보는 데 카드 제목이 다 필요하다. 막기 전에만 한 번 읽어 다시
    본다(#418). 그래도 막히면 모델이 고른 책을 결과에 적어, 모델이 그 책 한 권을 짚어 "담을까요?"라고 묻게
    한다. 다시 말해 달라고 하면 사용자가 같은 말을 되풀이해야 한다(#416 측정). 사용자가 "네"라고 하면 직전에
    한 권을 짚어 제안한 말에 대한 동의라서 근거가 된다(_from_history).
    """
    if evidence.has_evidence(ctx, ref, isbn):
        return None
    if evidence.needs_card_titles(ctx):
        missing = [card.isbn for card in ctx.cards if card.isbn not in ctx.titles]
        offers = await gateway.get_offers(list(dict.fromkeys(missing)))
        ctx.titles.update({offer.isbn: offer.title for offer in offers})
        if evidence.has_evidence(ctx, ref, isbn):
            return None
    return _fail(ToolErrorCode.REFERENCE_NOT_STATED.value, book=_named(ctx, ref, isbn))


def _named(ctx: TurnContext, ref: BookRef, isbn: str) -> str:
    """모델이 사용자에게 짚어 물을 수 있는 이름. ISBN 은 말하지 않는다. 제목을 모르면 번호만 쓴다."""
    if ref.source == "cart":
        where = f"장바구니 {ref.n}번째"
        title = next(
            (line.title for line in ctx.cart_snapshot or [] if line.isbn == isbn), None
        )
    else:
        where = f"카드 {ref.n}번"
        title = ctx.titles.get(isbn)
    return f"{where} 《{title}》" if title else where


async def _add(ctx: TurnContext, gateway: CommerceGateway, args: AddArgs) -> _Done:
    isbn = resolve(ctx, args.book)
    quantity = 1 if args.quantity is None else args.quantity
    arguments = {"isbn": isbn, "quantity": quantity}
    # 이번 턴에 AI 가 고른 책은 사용자가 확인하기 전에 담지 않는다. 확인은 응답의 버튼으로 받는다.
    # 프롬프트로 부탁하는 것으로는 못 막아서 장바구니를 바꾸기 전에 코드가 막는다
    if isbn in ctx.picked:
        return arguments, _fail(ToolErrorCode.CONFIRMATION_REQUIRED.value)
    if (blocked := await _unstated(ctx, gateway, args.book, isbn)) is not None:
        return arguments, blocked
    # 담기는 "요청 수량으로 바꾸기"다(BE#284). 수량을 말하지 않은 "담아줘"로 이미 3권 담긴 책이 1권이 되지 않게,
    # 담기 전에 장바구니를 다시 읽어 확인한다. 사용자가 수량을 말했으면("2권 담아줘") 그 수량으로 바꾼다(#489).
    # LLM 이 수량을 채웠어도 사용자의 말에 수량이 없으면 말하지 않은 것으로 본다
    cart = await gateway.get_cart(ctx.user_id)
    existing = next((line for line in cart if line.isbn == isbn), None)
    stated = args.quantity is not None and evidence.states_quantity(ctx)
    if existing is not None and (not stated or quantity == existing.quantity):
        failure = _fail(
            ToolErrorCode.ALREADY_IN_CART.value,
            title=existing.title,
            quantity=existing.quantity,
        )
        return arguments, failure
    arguments, result = await _write(
        arguments, gateway.add_item(ctx.user_id, isbn, quantity)
    )
    if existing is not None and result.get("ok") is True:
        # 결과 문장이 "담았어요"가 아니라 "수량을 N권으로 바꿨어요"가 되게 알린다
        result = {**result, "replaced": True, "previous": existing.quantity}
    return arguments, result


async def _update(
    ctx: TurnContext, gateway: CommerceGateway, args: UpdateArgs
) -> _Done:
    isbn = resolve(ctx, args.book)
    arguments = {"isbn": isbn, "quantity": args.quantity}
    if (blocked := await _unstated(ctx, gateway, args.book, isbn)) is not None:
        return arguments, blocked
    return await _write(
        arguments, gateway.set_quantity(ctx.user_id, isbn, args.quantity)
    )


async def _remove(
    ctx: TurnContext, gateway: CommerceGateway, args: RemoveArgs
) -> _Done:
    isbn = resolve(ctx, args.book)
    if (blocked := await _unstated(ctx, gateway, args.book, isbn)) is not None:
        return {"isbn": isbn}, blocked
    return await _write({"isbn": isbn}, gateway.remove_item(ctx.user_id, isbn))


async def _offer(ctx: TurnContext, gateway: CommerceGateway, args: OfferArgs) -> _Done:
    # 같은 책을 두 번 가리켜도 한 번만 묻는다. 받은 순서는 지킨다
    isbns = list(dict.fromkeys(resolve(ctx, ref) for ref in args.books))
    offers = await gateway.get_offers(isbns)
    # 카드 책의 제목은 여기서 알게 된다. 사용자가 제목으로 말했는지 볼 때 쓴다(evidence)
    ctx.titles.update({o.isbn: o.title for o in offers})
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


async def _pick(ctx: TurnContext, gateway: CommerceGateway, args: PickArgs) -> _Done:
    """직전 카드 안에서 예산과 권수에 맞는 조합을 코드가 고른다. 장바구니는 건드리지 않는다.

    가격과 재고는 LLM 이 읽어 오게 하지 않고 여기서 BE 에서 직접 읽는다(LLM 이 틀린 값을 넘길 수 없다).
    이미 장바구니에 있는 책은 고르지 않는다. 확인 버튼으로 담을 때 BE 의 담기가 "요청 수량으로 바꾸기"라서
    2권 담긴 책이 1권이 될 수 있기 때문이다.
    """
    arguments = {"budget_total": args.budget_total, "count": args.count}
    ordered = sorted(ctx.cards, key=lambda card: card.rank)
    in_cart = {line.isbn for line in await gateway.get_cart(ctx.user_id)}
    offers = {o.isbn: o for o in await gateway.get_offers([c.isbn for c in ordered])}
    candidates: dict[str, selection.Candidate] = {}
    for card in ordered:
        offer = offers.get(card.isbn)
        # 품절이거나 BE 가 모르는 책, 이미 담긴 책은 고르지 않는다. 같은 책이 카드에 두 번 있어도 한 번만 센다
        if (
            offer is not None
            and offer.in_stock
            and card.isbn not in in_cart
            and card.isbn not in candidates
        ):
            candidates[card.isbn] = selection.Candidate(
                card.isbn, offer.title, offer.price, card.match_score, card.rank
            )

    considered = selection.alternatives(len(candidates), args.count)
    chosen = selection.choose(list(candidates.values()), args.budget_total, args.count)
    if chosen is None:
        failure = _fail(
            ToolErrorCode.NO_COMBINATION.value, alternatives_considered=considered
        )
        return arguments, failure

    ctx.picked.update(c.isbn for c in chosen)
    return arguments, {
        "ok": True,
        "chosen": [
            {
                "isbn": c.isbn,
                "title": c.title,
                "price": c.price,
                "match_score": c.match_score,
            }
            for c in chosen
        ],
        "total_price": sum(c.price for c in chosen),
        "alternatives_considered": considered,
    }


async def _compare(
    comparer: Comparer, ctx: TurnContext, gateway: CommerceGateway, args: CompareArgs
) -> _Done:
    """소개글을 읽는 별도 호출이 만든 요약만 돌려준다. 소개글 원문은 결과에 싣지 않는다."""
    isbns = list(dict.fromkeys(resolve(ctx, ref) for ref in args.books))
    arguments = {"isbns": isbns}
    if len(isbns) < 2:  # 같은 책을 두 번 가리켰다
        return arguments, _fail(ToolErrorCode.INVALID_ARGUMENTS.value)
    try:
        summary = await comparer.compare(isbns)
    except CompareError as e:
        return arguments, _fail(e.code.value)
    return arguments, {"ok": True, "basis": "description", "summary": summary}


async def _ui_guide(
    ctx: TurnContext, gateway: CommerceGateway, args: UiGuideArgs
) -> _Done:
    """화면 이동 버튼만 붙인다. BE 를 부르지 않고, 버튼은 마무리 단계(reply)가 이 기록으로 만든다."""
    return {"target": args.target}, {"ok": True, "label": UI_TARGETS[args.target]}


_HANDLERS: dict[ToolName, _Handler] = {
    ToolName.CART_VIEW: _view,
    ToolName.CART_ADD: _add,
    ToolName.CART_UPDATE: _update,
    ToolName.CART_REMOVE: _remove,
    ToolName.BOOK_OFFER: _offer,
    ToolName.SELECTION_PICK: _pick,
    ToolName.UI_GUIDE: _ui_guide,
}


def _failed(
    name: ToolName, arguments: dict[str, Any], code: ToolErrorCode | str
) -> ToolCall:
    """실행하지 못한 호출의 기록. arguments 는 LLM 이 준 그대로다(책을 풀기 전이라 ISBN 이 없다)."""
    error = code.value if isinstance(code, ToolErrorCode) else code
    return ToolCall(name=name, arguments=arguments, result=_fail(error), grounded=True)


async def execute(
    ctx: TurnContext,
    gateway: CommerceGateway,
    name: ToolName,
    raw_args: Any,
    *,
    comparer: Comparer | None = None,
) -> ToolCall:
    """도구 하나를 실행하고 기록을 돌려준다. 막히거나 BE 가 거절해도 예외를 던지지 않는다.

    comparer 는 book.compare 만 쓴다. 없으면 그 도구는 TOOL_NOT_IMPLEMENTED 로 막힌다.

    grounded 는 이 결과가 실제로 실행해서 얻은 값이라는 뜻으로 true 로 둔다. 답변이 이 결과를
    뒷받침하는지는 마무리 단계(7단계)가 판단해서 바꾼다.
    """
    given = raw_args if isinstance(raw_args, dict) else {}

    blocked = guard.check(ctx, name)
    if blocked is not None:
        return _failed(name, given, blocked)

    handler = _HANDLERS.get(name)
    if name == ToolName.BOOK_COMPARE and comparer is not None:
        handler = partial(_compare, comparer)
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
