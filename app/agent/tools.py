"""⑦ LLM 이 부르는 도구를 실행한다(#354, #358, #363). 장바구니·가격을 다루는 5개와 그 밖의 도구가 있다.

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

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import partial
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.agent import evidence, guard, selection
from app.agent.compare import CompareError, Comparer
from app.agent.errors import RefError, ToolErrorCode
from app.agent.reference import BookRef, TurnContext, resolve
from app.agent.reviews import (
    MAX_REVIEWS_READ,
    MIN_REVIEWS_FOR_SUMMARY,
    ReviewSummarizer,
    ReviewSummaryError,
)
from app.agent.schemas import MAX_CART_LINES, MAX_CONTEXT_CARDS, ToolCall, ToolName
from app.gateway.book_finder import BookFinder, FinderUnavailable
from app.gateway.book_info import (
    BookInfo,
    CouponInfo,
    InfoUnavailable,
    ProductDetail,
)
from app.gateway.commerce import (
    BookOffer,
    CartLine,
    CommerceError,
    CommerceErrorCode,
    CommerceGateway,
    OfferMemory,
    Restorable,
)

# book.search 가 한 번에 보여 주는 책 수. 많으면 LLM 이 읽을 것이 늘고 BE 상품 상세 호출도 는다
SEARCH_SIZE = 5
# 도구를 그만 쓸 마감(graph 의 시간 예산)이 코앞이거나 지난 뒤에 시작한 검색에 주는 시간. 마감은 마지막 답 쓰기
# 호출을 위해 남겨 둔 시간(30초 제한에서 10초) 앞에 있다. 검색 하나의 한도는 BE 검색 5초 + 상세 5초라, 마감 직전에
# 시작한 검색이 끝까지 기다리면 그 시간을 먹고 턴이 504 로 끝난다(#637 리뷰). 마감까지 남은 시간에 이만큼만 더 준다.
SEARCH_GRACE_SECONDS = 2.0
# book.detail 이 한 번에 보는 책 수. 책마다 BE 상품 상세를 한 번씩 부른다
MAX_DETAIL_BOOKS = 3
# BE 쿠폰의 만료 시각은 시간대 없이 한국 시각으로 온다("2026-12-01T00:00:00")
_KST = ZoneInfo("Asia/Seoul")
# 찾은 책으로 조합을 고를 때의 취향 점수 대신. 검색 순서가 앞인 책을 높게 친다
_SEARCH_SCORE_TOP = 100
_SEARCH_SCORE_STEP = 5


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


class SearchArgs(_Args):
    query: str = Field(
        min_length=1,
        max_length=50,
        description=(
            "찾을 책을 나타내는 짧은 검색어. 사용자가 말한 주제·종류 낱말만 쓴다"
            "(예: '파이썬 입문서', '개발 입문서'). 권수, 담기, 주문 같은 말은 넣지 않는다"
        ),
    )


class DetailArgs(_Args):
    books: list[BookRef] = Field(
        min_length=1,
        max_length=MAX_DETAIL_BOOKS,
        description="정보를 알아볼 책들(1~3권)",
    )


class ReviewsArgs(_Args):
    book: BookRef = Field(description="리뷰를 볼 책")


class CompareArgs(_Args):
    books: list[BookRef] = Field(
        min_length=2, max_length=3, description="소개글로 견줄 책들(2~3권)"
    )


class OrderArgs(_Args):
    books: list[BookRef] = Field(
        min_length=1,
        max_length=MAX_CART_LINES,
        description="주문할 책들. 같은 책은 한 번만 쓴다. 장바구니에 없으면 이 도구가 담는다",
    )
    quantity: int | None = Field(
        default=None,
        description=(
            "책 한 권을 여러 권 주문하라고 했을 때만 그 권수('두 권 주문할게요'면 2). "
            "말하지 않았으면 넣지 않는다(1권)"
        ),
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
    ToolName.BOOK_SEARCH: SearchArgs,
    ToolName.BOOK_DETAIL: DetailArgs,
    ToolName.BOOK_REVIEWS: ReviewsArgs,
    ToolName.SELECTION_PICK: PickArgs,
    ToolName.BOOK_COMPARE: CompareArgs,
    ToolName.UI_GUIDE: UiGuideArgs,
    ToolName.ORDER_OPEN: OrderArgs,
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


async def _write(
    ctx: TurnContext, arguments: dict[str, Any], call: Awaitable[None]
) -> _Done:
    """쓰기 호출을 하고 BE 가 거절하면 이유를 결과에 적는다. 장바구니는 바뀌지 않았다."""
    try:
        await call
    except CommerceError as e:
        return arguments, _fail(e.code.value)
    ctx.changed_cart = True
    return arguments, {"ok": True}


def delegated(ctx: TurnContext) -> bool:
    """골라서 담거나 주문하라고 맡긴 말인가(#631). graph 가 고른 책을 코드로 담을지 정할 때 쓴다."""
    return _delegated(ctx)


def picked_ref(ctx: TurnContext, isbn: str) -> dict[str, Any] | None:
    """코드가 고른 책을 cart.add 인자의 book 으로. 찾은 책이면 search n, 카드면 card rank 다."""
    for n, book in enumerate(ctx.search_results, start=1):
        if book.isbn == isbn:
            return {"source": "search", "n": n}
    for card in ctx.cards:
        if card.isbn == isbn:
            return {"source": "card", "n": card.rank}
    return None


def _over_stock(ctx: TurnContext, isbn: str, quantity: int) -> dict[str, Any] | None:
    """남은 재고보다 많은 수량이면 BE 와 같은 이유로 막은 결과, 아니면 None(#692).

    이번 턴의 장바구니(SnapshotCommerce)는 재고를 있고 없음만 받아, 20권 남은 책을 50권으로 바꿔도 "바꿨어요"라고
    답하고 BE 가 반영할 때 INSUFFICIENT_PRODUCT_STOCK 으로 거절했다(11/11 dev). 남은 수량을 아는 책(카드 책·찾은
    책)이면 BE 에 보내기 전에 막고, 답이 남은 수량을 말할 수 있게 결과에 싣는다. 모르는 책은 BE 에 맡긴다.
    """
    left = ctx.stock_of(isbn)
    if left is None or quantity <= left:
        return None
    return _fail(CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK.value, stock=left)


def _delegated(ctx: TurnContext) -> bool:
    """골라서 담거나 주문하라고 맡긴 말인가(#631). 그러면 코드가 고른 책을 확인 버튼 없이 담는다."""
    return ctx.checkout_enabled and evidence.delegates_pick(ctx)


def _separate(ctx: TurnContext) -> dict[str, Any] | None:
    """이번 턴에 조합을 골랐으면 장바구니를 바꾸지 않는 결과, 아니면 None.

    고른 조합(selection)은 확인 버튼으로 담고, 장바구니 변경(actions)은 이미 했다고 말한 것이라 BE 가 한 응답에
    같이 받지 않는다(#489). 고른 책 자체를 담으려 하면 그 전에 CONFIRMATION_REQUIRED 로 막힌다.
    골라서 담으라고 맡긴 말이면 확인 버튼 없이 담으므로 막지 않는다(#631). 응답에는 조합 대신 변경이 실린다(reply).
    """
    if ctx.picked and not _delegated(ctx):
        return _fail(ToolErrorCode.SEPARATE_TURN_REQUIRED.value)
    return None


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
    ctx: TurnContext, gateway: CommerceGateway, ref: BookRef, isbn: str, verb: str
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
    card = evidence.card_for_bare_number(ctx, ref, isbn)
    if card is not None:
        # 숫자만 말한 "2번"을 모델이 장바구니 2번째로 읽었다. 카드 2번일 수도 있어 두 책을 짚어 묻게 한다(#528)
        if card.isbn not in ctx.titles:
            offers = await gateway.get_offers([card.isbn])
            ctx.titles.update({offer.isbn: offer.title for offer in offers})
        card_ref = BookRef(source="card", n=card.rank)
        return _fail(
            ToolErrorCode.AMBIGUOUS_BOOK.value,
            books=[_named(ctx, card_ref, card.isbn), _named(ctx, ref, isbn)],
        )
    position = evidence.cart_for_bare_number(ctx, ref, isbn)
    if position is not None:
        # 방금 장바구니를 번호로 보여 준 뒤의 "2번"을 모델이 카드 2번으로 읽었다. 장바구니 2번째일 수도 있어 묻는다
        cart_ref = BookRef(source="cart", n=position)
        line = (ctx.cart_snapshot or [])[position - 1]
        return _fail(
            ToolErrorCode.AMBIGUOUS_BOOK.value,
            books=[_named(ctx, ref, isbn), _named(ctx, cart_ref, line.isbn)],
        )
    return _held(ToolErrorCode.REFERENCE_NOT_STATED, ctx, ref, isbn, verb)


def _unrequested(
    ctx: TurnContext, ref: BookRef, isbn: str, verb: str
) -> dict[str, Any] | None:
    """사용자가 장바구니를 바꾸라고 말하지 않았으면 막는 결과, 말했으면 None(#531).

    책의 근거(_unstated)를 본 뒤에 본다. 막히면 REFERENCE_NOT_STATED 처럼 모델이 고른 책을 결과에 적어, 그 책을
    짚어 "담을까요?"라고 묻게 한다. 사용자가 "네"라고 하면 행동을 물은 말에 대한 답이라 통과한다.
    """
    if evidence.states_action(ctx):
        return None
    return _held(ToolErrorCode.ACTION_NOT_STATED, ctx, ref, isbn, verb)


def _held(
    code: ToolErrorCode, ctx: TurnContext, ref: BookRef, isbn: str, verb: str
) -> dict[str, Any]:
    """막은 쓰기의 결과. 모델이 그대로 물을 문장(ask)을 코드가 만든다(#531).

    물을 말을 모델에게 맡겼더니 막힌 9번 중 7번이 "이미 담긴 책이라 담을 수 없어요"처럼 틀리거나 "'담아줘'라고
    말씀해 주세요"처럼 다시 말하게 했다(gpt-4o-mini). verb 는 도구마다 "담을까요?", "뺄까요?"처럼 준다.
    """
    book = _named(ctx, ref, isbn)
    return _fail(code.value, book=book, ask=f"{book}{_object_particle(book)} {verb}")


def _object_particle(name: str) -> str:
    """이름 뒤의 을/를. 끝 글자에 받침이 있으면 을이다. 제목을 감싼 》는 건너뛴다.
    끝이 한글이 아니면(영문, 숫자) 읽는 법을 몰라 을로 둔다."""
    last = name.rstrip("》 ")[-1:]
    if "가" <= last <= "힣":
        return "을" if (ord(last) - ord("가")) % 28 else "를"
    return "을"


def _named(ctx: TurnContext, ref: BookRef, isbn: str) -> str:
    """모델이 사용자에게 짚어 물을 수 있는 이름. ISBN 은 말하지 않는다. 제목을 모르면 번호만 쓴다."""
    if ref.source == "cart":
        where = f"장바구니 {ref.n}번째"
        title = next(
            (line.title for line in ctx.cart_snapshot or [] if line.isbn == isbn), None
        )
    elif ref.source == "focused":
        where = "지금 보고 계신 책"
        title = ctx.titles.get(isbn)
    elif ref.source == "search":
        where = f"찾은 책 {ref.n}번"
        title = ctx.titles.get(isbn)
    else:
        where = f"카드 {ref.n}번"
        title = ctx.titles.get(isbn)
    return f"{where} 《{title}》" if title else where


async def _add(ctx: TurnContext, gateway: CommerceGateway, args: AddArgs) -> _Done:
    isbn = resolve(ctx, args.book)
    quantity = 1 if args.quantity is None else args.quantity
    # 찾은 책의 제목과 권수를 말했으면("리눅스 명령어 사전 두 권") 수량은 사용자가 말한 수다. 모델이 1로 채워도
    # 코드가 고친다(11/10 평가 checkout-m08)
    copies = evidence.copies_of(ctx, isbn) if args.book.source == "search" else None
    if copies is not None:
        quantity = copies
    # 찾은 책이나 코드가 고른 책에서 "두 권 골라서"의 두 권은 서로 다른 책의 수다. 모델이 그 수를 한 책의 수량으로
    # 채워도 "두 권씩"처럼 말하지 않았으면 한 권만 담는다(#631). 다만 찾은 책의 제목을 말하고 권수를 말했으면
    # ("AI 모델 서빙 실전 3권") 그 권수는 그 책의 수량이다
    if (
        (args.book.source == "search" or isbn in ctx.picked)
        and not evidence.states_each(ctx)
        and not evidence.states_copies(ctx, isbn)
    ):
        quantity = 1
    # 사용자가 수량을 말하지 않았는데 모델이 2권 이상을 채우면 1권이다. 카드 제목을 상태에 보여 준 뒤로 모델이
    # "2번 주문해 줘"의 "2번"을 2권으로 읽었다(11/10 평가 checkout-h09 5번 중 4번). 말한 권수보다 많이 담지 않는다
    if quantity > 1 and copies is None and not evidence.said_quantity_lately(ctx):
        quantity = 1
    arguments = {"isbn": isbn, "quantity": quantity}
    # 이번 턴에 AI 가 고른 책은 사용자가 확인하기 전에 담지 않는다. 확인은 응답의 버튼으로 받는다.
    # 프롬프트로 부탁하는 것으로는 못 막아서 장바구니를 바꾸기 전에 코드가 막는다. 골라서 담으라고 맡긴 말은
    # 그 말이 확인이다(#631)
    if isbn in ctx.picked and not _delegated(ctx):
        return arguments, _fail(ToolErrorCode.CONFIRMATION_REQUIRED.value)
    if (separate := _separate(ctx)) is not None:
        return arguments, separate
    verb = "담을까요?"
    # 골라서 담으라고 맡겨 코드가 고른 책은 고른 것 자체가 그 책의 근거다(#631)
    if not (isbn in ctx.picked and _delegated(ctx)):
        blocked = await _unstated(ctx, gateway, args.book, isbn, verb)
        if blocked is not None:
            return arguments, blocked
    # 주문 화면 열기(order.open)가 담는 책은 모델이 주문하려는 말로 읽은 것이 담으라는 말이다(#664)
    if (
        not ctx.ordering
        and (blocked := _unrequested(ctx, args.book, isbn, verb)) is not None
    ):
        return arguments, blocked
    # 담기는 "요청 수량으로 바꾸기"다(BE#284). 수량을 말하지 않은 "담아줘"로 이미 3권 담긴 책이 1권이 되지 않게,
    # 담기 전에 장바구니를 다시 읽어 확인한다. 사용자가 수량을 말했으면("2권 담아줘") 그 수량으로 바꾼다(#489).
    # LLM 이 수량을 채웠어도 사용자의 말에 수량이 없으면 말하지 않은 것으로 본다
    cart = await gateway.get_cart(ctx.user_id)
    existing = next((line for line in cart if line.isbn == isbn), None)
    stated = (args.quantity is not None or copies is not None) and (
        evidence.states_quantity(ctx)
    )
    if existing is not None and (not stated or quantity == existing.quantity):
        failure = _fail(
            ToolErrorCode.ALREADY_IN_CART.value,
            title=existing.title,
            quantity=existing.quantity,
        )
        return arguments, failure
    if (short := _over_stock(ctx, isbn, quantity)) is not None:
        return arguments, short
    arguments, result = await _write(
        ctx, arguments, gateway.add_item(ctx.user_id, isbn, quantity)
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
    if (separate := _separate(ctx)) is not None:
        return arguments, separate
    verb = f"{args.quantity}권으로 바꿀까요?"
    if (blocked := await _unstated(ctx, gateway, args.book, isbn, verb)) is not None:
        return arguments, blocked
    if (blocked := _unrequested(ctx, args.book, isbn, verb)) is not None:
        return arguments, blocked
    if (short := _over_stock(ctx, isbn, args.quantity)) is not None:
        return arguments, short
    return await _write(
        ctx, arguments, gateway.set_quantity(ctx.user_id, isbn, args.quantity)
    )


async def _remove(
    ctx: TurnContext, gateway: CommerceGateway, args: RemoveArgs
) -> _Done:
    # 카드가 보일 때 숫자만 말한 "2번 빼줘"를 장바구니 2번째로 읽었으면 카드 2번 책을 뺀다(evidence.removal_card)
    book = evidence.removal_card(ctx, args.book) or args.book
    isbn = resolve(ctx, book)
    if (separate := _separate(ctx)) is not None:
        return {"isbn": isbn}, separate
    verb = "뺄까요?"
    if (blocked := await _unstated(ctx, gateway, book, isbn, verb)) is not None:
        return {"isbn": isbn}, blocked
    if (blocked := _unrequested(ctx, book, isbn, verb)) is not None:
        return {"isbn": isbn}, blocked
    return await _write(ctx, {"isbn": isbn}, gateway.remove_item(ctx.user_id, isbn))


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
                # 책 정보가 켜져 있으면 BE 가 보낸 남은 수량도 싣는다. "몇 권 남았어?"에 모델이 book_detail 대신 이
                # 도구를 고르면 "재고가 있어요"로만 답했다(11/10 실측 6번 중 3번, 설명을 고쳐도 같았다)
                **(
                    {"stock_quantity": ctx.stock[o.isbn]}
                    if ctx.book_info_enabled and o.isbn in ctx.stock
                    else {}
                ),
            }
            for o in offers
        ]
    }
    return {"isbns": isbns}, result


async def _pick(ctx: TurnContext, gateway: CommerceGateway, args: PickArgs) -> _Done:
    """직전 카드 안에서 예산과 권수에 맞는 조합을 코드가 고른다. 장바구니는 건드리지 않는다.

    이번 턴에 book.search 로 책을 찾았으면 카드가 아니라 찾은 책 안에서 고른다(#631). 사용자가 새 책을 원한 것이다.

    가격과 재고는 LLM 이 읽어 오게 하지 않고 여기서 BE 에서 직접 읽는다(LLM 이 틀린 값을 넘길 수 없다).
    이미 장바구니에 있는 책은 고르지 않는다. 확인 버튼으로 담을 때 BE 의 담기가 "요청 수량으로 바꾸기"라서
    2권 담긴 책이 1권이 될 수 있기 때문이다.

    이번 턴에 장바구니를 이미 바꿨으면 고르지 않는다. 고른 조합(selection)과 장바구니 변경(actions)을 BE 가 한
    응답에 같이 받지 않는다(#489).
    """
    arguments = {"budget_total": args.budget_total, "count": args.count}
    if not evidence.states_budget(ctx):
        # 예산은 사용자 말에서만 온다. 다음에 할 일을 결과에 적어 모델이 짐작한 예산으로 다시 부르지 않게 한다
        return arguments, _fail(
            ToolErrorCode.BUDGET_NOT_STATED.value, next=_budget_hint(ctx)
        )
    if ctx.changed_cart:
        return arguments, _fail(ToolErrorCode.SEPARATE_TURN_REQUIRED.value)
    in_cart = {line.isbn for line in await gateway.get_cart(ctx.user_id)}
    candidates: dict[str, selection.Candidate] = {}
    if ctx.search_results:
        for n, book in enumerate(ctx.search_results, start=1):
            if (
                book.in_stock
                and book.isbn not in in_cart
                and book.isbn not in candidates
            ):
                score = max(0, _SEARCH_SCORE_TOP - _SEARCH_SCORE_STEP * (n - 1))
                candidates[book.isbn] = selection.Candidate(
                    book.isbn, book.title, book.price, score, n
                )
        return _picked_result(ctx, arguments, candidates, args)
    ordered = sorted(ctx.cards, key=lambda card: card.rank)
    offers = {o.isbn: o for o in await gateway.get_offers([c.isbn for c in ordered])}
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
    return _picked_result(ctx, arguments, candidates, args)


def _budget_hint(ctx: TurnContext) -> str:
    """금액 없이 예산 고르기를 부른 모델에게 줄 다음 할 일.

    담으라고 몰지 않는다. "1번이랑 2번 중에 나은 거 담아줘"(평가 compare-03)는 어느 책인지 AI 가 고르는 말이라
    담으면 안 되는데, 예전엔 모델이 이 말에 예산 고르기를 불러 확인 버튼으로 우연히 안전하게 끝났다(#400).
    """
    if ctx.search_results:
        return (
            "사용자가 금액을 말하지 않았다. 예산을 지어내 다시 부르지 않는다. 찾은 책에서 담는다: 책 제목과 "
            "권수를 말했으면 그 책을 그 권수(quantity)로, 주제로 그렸으면 재고가 있는 서로 다른 책을 말한 "
            "권수만큼"
        )
    return (
        "사용자가 금액을 말하지 않았다. 예산을 지어내 다시 부르지 않는다. 사용자가 한 권을 정해 담으라고 "
        "했으면 그 책만 cart_add 로 담는다. 어느 책인지 네가 골라야 하는 말이면 담지 말고 사용자에게 고르게 한다"
    )


def _picked_result(
    ctx: TurnContext,
    arguments: dict[str, Any],
    candidates: dict[str, selection.Candidate],
    args: PickArgs,
) -> _Done:
    """후보 안에서 조합을 골라 결과로 만든다."""
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


async def _search(
    finder: BookFinder,
    time_left: Callable[[], float] | None,
    ctx: TurnContext,
    gateway: CommerceGateway,
    args: SearchArgs,
) -> _Done:
    """카드 밖의 책을 BE 공개 검색으로 찾는다(#631). 장바구니는 건드리지 않는다.

    찾은 책은 "search n"으로 가리킨다. 번호는 이번 턴의 검색 전체에 이어 붙는다. 마지막 검색 기준으로 매겼더니
    "알고리즘 책이랑 리눅스 책 하나씩"에서 두 검색의 책이 둘 다 1번이 되어 같은 책을 두 번 담았다(평가 checkout-h04).
    앞에서 찾은 책이 다시 나오면 처음 번호를 그대로 쓴다. 가격·재고는 BE 상품 상세의 값이라, 장바구니 복사본에
    받아 두면 cart.add 가 카드 책처럼 담는다. 담은 결과는 actions 로 BE 에 가고 BE 가 다시 검사한다(BE#533).

    time_left 는 도구를 그만 쓸 마감까지 남은 초다. 마감이 지났으면 검색하지 않고, 아니면 남은 시간(+여유)
    안에 못 끝낸 검색을 끊는다. 둘 다 BE 를 읽지 못했을 때와 같이 SEARCH_FAILED 다. 한 반복에서 검색이 차례로 돌며
    마감을 넘겨 턴이 504 로 끝나는 것을 막는다.
    """
    arguments = {"query": args.query}
    budget: float | None = None
    if time_left is not None:
        left = time_left()
        if left <= 0:
            return arguments, _fail(ToolErrorCode.SEARCH_FAILED.value)
        budget = left + SEARCH_GRACE_SECONDS
    try:
        async with asyncio.timeout(budget):
            books = await finder.find(args.query, SEARCH_SIZE)
    except (FinderUnavailable, TimeoutError):
        return arguments, _fail(ToolErrorCode.SEARCH_FAILED.value)
    known = {book.isbn for book in ctx.search_results}
    ctx.search_results = [
        *ctx.search_results,
        *(book for book in books if book.isbn not in known),
    ]
    round_no = max(ctx.search_round.values(), default=0) + 1
    for book in books:
        ctx.search_round.setdefault(book.isbn, round_no)
    ctx.found.update({book.isbn: book for book in books})
    ctx.titles.update({book.isbn: book.title for book in books})
    if isinstance(gateway, OfferMemory):
        gateway.remember_offers(
            [BookOffer(b.isbn, b.title, b.price, b.in_stock) for b in books]
        )
    return arguments, {
        "books": [
            {
                "isbn": book.isbn,
                "title": book.title,
                "author": book.author,
                "price": book.price,
                "in_stock": book.in_stock,
            }
            for book in books
        ]
    }


async def _bounded(
    time_left: Callable[[], float] | None, start: Callable[[], Awaitable[Any]]
) -> Any:
    """BE 공개 API 호출을 도구를 그만 쓸 마감(+여유) 안에서 끝낸다. 마감이 지났으면 부르지 않고 TimeoutError.

    호출은 start 로 받아 마감을 본 뒤에 시작한다(먼저 만들어 두면 마감이 지나도 이미 BE 를 부르고 있다).
    """
    budget: float | None = None
    if time_left is not None:
        left = time_left()
        if left <= 0:
            raise TimeoutError
        budget = left + SEARCH_GRACE_SECONDS
    async with asyncio.timeout(budget):
        return await start()


def _unexpired(coupon: CouponInfo, now: datetime) -> bool:
    """만료되지 않은 쿠폰인가. 만료 시각을 못 읽으면 남긴다(결제 때 BE 가 다시 본다)."""
    if coupon.expired_at is None:
        return True
    try:
        expires = datetime.fromisoformat(coupon.expired_at)
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=_KST)
    return expires > now


def _coupon_view(coupon: CouponInfo, owned: set[int]) -> dict[str, Any]:
    """쿠폰 하나를 결과에 싣는 모양. 정액은 원(discount_won), 정률은 퍼센트(discount_percent)다."""
    out: dict[str, Any] = {"name": coupon.name, "owned": coupon.coupon_id in owned}
    if coupon.kind == "FIXED_AMOUNT":
        out["discount_won"] = int(coupon.discount)
    else:
        out["discount_percent"] = round(coupon.discount * 100, 1)
    out["min_order_amount"] = coupon.min_order_amount
    return out


def _detail_view(
    isbn: str, detail: ProductDetail, coupons: list[dict[str, Any]] | None
) -> dict[str, Any]:
    book: dict[str, Any] = {
        "isbn": isbn,
        "title": detail.title,
        "author": detail.author,
        "publisher": detail.publisher,
        "published_at": detail.published_at,
        "category": detail.category,
        "price": detail.price,
        "list_price": detail.list_price,
        "stock_quantity": detail.stock_quantity,
        "in_stock": detail.stock_quantity > 0,
        # 리뷰 수·별점은 싣지 않는다. 상품 상세의 값이 리뷰 목록보다 늦어 리뷰가 있는 책을 "아직 리뷰가 없다"고
        # 답했다(11/10 dev 실측). 리뷰는 book.reviews 가 목록으로 센다
    }
    if coupons is None:
        book["coupons_unknown"] = True  # 쿠폰 정보를 못 읽었다. 없다고 말하면 안 된다
    else:
        book["coupons"] = coupons
    return book


async def _detail(
    info: BookInfo,
    time_left: Callable[[], float] | None,
    ctx: TurnContext,
    gateway: CommerceGateway,
    args: DetailArgs,
) -> _Done:
    """책의 정보(출판사·출간일·분야·정가·재고 수량)와 쓸 수 있는 쿠폰을 BE 공개 API 로 읽는다(#650).

    장바구니는 건드리지 않는다. 소개글은 싣지 않는다(소개글 속 지시문이 도구를 고르는 LLM 에게 가지 않게).
    쿠폰은 BE 가 상품에 걸어 둔 것 중 만료되지 않은 것만 싣고, 사용자가 이미 가진 쿠폰이면 owned 로 표시한다.
    상품 번호를 모르는 책(product_id 를 보내지 않는 예전 BE)은 missing 으로 알린다.
    """
    isbns = list(dict.fromkeys(resolve(ctx, ref) for ref in args.books))
    arguments: dict[str, Any] = {"isbns": isbns}
    ids = {isbn: ctx.product_id(isbn) for isbn in isbns}
    known = {isbn: pid for isbn, pid in ids.items() if pid is not None}
    if not known:
        return arguments, _fail(ToolErrorCode.PRODUCT_UNKNOWN.value)
    try:
        details = await _bounded(time_left, lambda: info.details(list(known.values())))
    except TimeoutError:
        details = {}
    found = {isbn: details[pid] for isbn, pid in known.items() if pid in details}
    if not found:
        return arguments, _fail(ToolErrorCode.INFO_FAILED.value)

    coupon_ids = sorted({c for d in found.values() for c in d.coupon_ids})
    coupons: dict[int, CouponInfo] | None = {}
    if coupon_ids:
        try:
            listed = await _bounded(time_left, lambda: info.coupons(coupon_ids))
            now = datetime.now(_KST)
            coupons = {c.coupon_id: c for c in listed if _unexpired(c, now)}
        except (InfoUnavailable, TimeoutError):
            coupons = None
    owned = {c.coupon_id for c in ctx.owned_coupons}

    books = []
    for isbn, detail in found.items():
        ctx.titles[isbn] = detail.title
        shown = (
            None
            if coupons is None
            else [
                _coupon_view(coupons[c], owned)
                for c in sorted(detail.coupon_ids)
                if c in coupons and coupons[c].kind != "NONE"
            ]
        )
        books.append(_detail_view(isbn, detail, shown))
    result: dict[str, Any] = {"books": books}
    missing = [isbn for isbn in isbns if isbn not in found]
    if missing:
        result["missing"] = missing
    return arguments, result


async def _reviews(
    info: BookInfo,
    summarizer: ReviewSummarizer | None,
    time_left: Callable[[], float] | None,
    ctx: TurnContext,
    gateway: CommerceGateway,
    args: ReviewsArgs,
) -> _Done:
    """한 책의 리뷰 수·별점과, 리뷰가 넉넉하면 따로 읽는 호출이 쓴 요약(#650).

    리뷰 원문은 결과에 싣지 않는다(다른 사용자가 쓴 글이라 지시문이 섞일 수 있다). 스포일러로 표시된 리뷰는
    요약에 쓰지 않는다. 스포일러가 아닌 리뷰가 MIN_REVIEWS_FOR_SUMMARY 개보다 적으면 요약하지 않는다. 한두
    사람의 말을 독자 전체의 평으로 부풀리지 않으려는 것이다.
    """
    isbn = resolve(ctx, args.book)
    arguments = {"isbn": isbn}
    pid = ctx.product_id(isbn)
    if pid is None:
        return arguments, _fail(ToolErrorCode.PRODUCT_UNKNOWN.value)
    try:
        details, reviews = await _bounded(
            time_left,
            lambda: asyncio.gather(
                info.details([pid]), info.reviews(pid, MAX_REVIEWS_READ)
            ),
        )
    except (InfoUnavailable, TimeoutError):
        return arguments, _fail(ToolErrorCode.INFO_FAILED.value)
    detail = details.get(pid)
    if detail is None:
        return arguments, _fail(ToolErrorCode.INFO_FAILED.value)
    ctx.titles[isbn] = detail.title
    readable = [r for r in reviews if not r.spoiler]
    count, rate = detail.review_count, detail.review_rate
    if len(reviews) > count:
        # 상품 상세의 리뷰 수·평점이 리뷰 목록보다 늦다(11/10 dev: 리뷰 1개인 책의 reviewCount 0). 그대로 쓰면 리뷰가
        # 있는데 "아직 없다"고 답한다. 목록에서 읽은 수와 그 평균을 쓴다
        count = len(reviews)
        rate = sum(r.rate for r in reviews) / count
    result: dict[str, Any] = {
        "ok": True,
        "isbn": isbn,
        "title": detail.title,
        "review_count": count,
        "review_rate": None if rate is None else round(rate, 1),
        "summary": None,
    }
    if len(readable) < MIN_REVIEWS_FOR_SUMMARY:
        result["note"] = "TOO_FEW_REVIEWS"  # 요약하지 않았다. 수와 별점만 말한다
        return arguments, result
    if summarizer is None:
        return arguments, _fail(ToolErrorCode.REVIEW_SUMMARY_FAILED.value)
    try:
        result["summary"] = await summarizer.summarize(detail.title, readable)
    except ReviewSummaryError as e:
        return arguments, _fail(e.code.value)
    return arguments, result


async def _ui_guide(
    ctx: TurnContext, gateway: CommerceGateway, args: UiGuideArgs
) -> _Done:
    """화면 이동 버튼만 붙인다. BE 를 부르지 않고, 버튼은 마무리 단계(reply)가 이 기록으로 만든다."""
    return {"target": args.target}, {"ok": True, "label": UI_TARGETS[args.target]}


async def _order(ctx: TurnContext, gateway: CommerceGateway, args: OrderArgs) -> _Done:
    """주문하겠다는 책을 짚는다(#664). 턴이 끝나면 graph 가 이 책들로 주문 화면을 연다.

    주문하려는 말인지는 모델이 정한다. 낱말 목록(evidence.wants_order)만으로는 "주문을 해주라고", "주문시켜줘"를
    놓쳐 담기만 하고 주문 화면이 열리지 않았다(11/11 dev). 주문 화면은 결제가 아니라 사용자가 확인하고 결제 버튼을
    눌러야 주문된다.

    장바구니에 없는 책은 여기서 담는다. 프롬프트와 도구 설명에 "먼저 cart_add 로 담는다"고 적어도 실제 모델이 담지
    않고 이 도구만 불러 74번 중 12번 주문이 안 됐다(11/11 평가). 담기는 cart.add 와 같은 길이라 책의 근거 검사는
    그대로다. 장바구니를 바꾸라는 말인지(ACTION_NOT_STATED)는 보지 않는다. 모델이 주문하려는 말로 읽은 것이 그
    말이다("1번 결제창 띄워줘"). 하나라도 못 담으면 담은 것도 되돌리고, 그 이유(되물을 말 ask 포함)를 그대로 돌려준다.
    """
    if not ctx.logged_in:
        return {}, _fail(ToolErrorCode.LOGIN_REQUIRED.value)
    pairs = list({resolve(ctx, ref): ref for ref in args.books}.items())
    isbns = [isbn for isbn, _ in pairs]
    arguments = {"isbns": isbns}
    cart = {line.isbn for line in await gateway.get_cart(ctx.user_id)}
    point = gateway.checkpoint() if isinstance(gateway, Restorable) else None
    changed = ctx.changed_cart
    added: list[str] = []
    ctx.ordering = True
    try:
        for isbn, ref in pairs:
            if isbn in cart:
                continue
            # 권수는 책이 한 권일 때만 그 책의 수량이다. 여러 책이면 서로 다른 책의 수라서 한 권씩이다
            quantity = args.quantity if len(pairs) == 1 else None
            _, result = await _add(ctx, gateway, AddArgs(book=ref, quantity=quantity))
            if result.get("ok") is not True:
                if added and point is not None:
                    gateway.restore(point)
                    ctx.changed_cart = changed
                return arguments, result
            added.append(isbn)
    finally:
        ctx.ordering = False
    for isbn in isbns:
        if isbn not in ctx.order_isbns:
            ctx.order_isbns.append(isbn)
    titles = {line.isbn: line.title for line in await gateway.get_cart(ctx.user_id)}
    return arguments, {
        "ok": True,
        "titles": [titles.get(isbn) for isbn in isbns],
        "added": added,
    }


_HANDLERS: dict[ToolName, _Handler] = {
    ToolName.CART_VIEW: _view,
    ToolName.CART_ADD: _add,
    ToolName.CART_UPDATE: _update,
    ToolName.CART_REMOVE: _remove,
    ToolName.BOOK_OFFER: _offer,
    ToolName.SELECTION_PICK: _pick,
    ToolName.UI_GUIDE: _ui_guide,
    ToolName.ORDER_OPEN: _order,
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
    finder: BookFinder | None = None,
    time_left: Callable[[], float] | None = None,
    info: BookInfo | None = None,
    summarizer: ReviewSummarizer | None = None,
) -> ToolCall:
    """도구 하나를 실행하고 기록을 돌려준다. 막히거나 BE 가 거절해도 예외를 던지지 않는다.

    comparer 는 book.compare 만 쓴다. 없으면 그 도구는 TOOL_NOT_IMPLEMENTED 로 막힌다.
    finder 는 book.search 만 쓴다(#631). 없으면(기능이 꺼져 있으면) 같은 이유로 막힌다.
    info 는 book.detail·book.reviews 가 쓰는 BE 공개 상품 정보이고, summarizer 는 리뷰 요약을 쓰는 따로 부르는 호출이다
    (#650). info 가 없으면 두 도구는 막힌다.
    time_left 는 BE 공개 API 를 읽는 도구(book.search·book.detail·book.reviews)가 본다. 도구를 그만 쓸 마감까지 남은
    초를 돌려주는 함수이고, 없으면 시간을 보지 않는다.

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
    if name == ToolName.BOOK_SEARCH and finder is not None:
        handler = partial(_search, finder, time_left)
    if name == ToolName.BOOK_DETAIL and info is not None:
        handler = partial(_detail, info, time_left)
    if name == ToolName.BOOK_REVIEWS and info is not None:
        handler = partial(_reviews, info, summarizer, time_left)
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
