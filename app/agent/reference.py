"""⑦ LLM 이 가리킨 책을 ISBN 으로 확정한다(#354).

LLM 은 책을 ISBN 으로 부르지 않는다. 어느 목록의 몇 번째인지만 말하고, ISBN 은 이 모듈이 요청과 장바구니
조회 결과에서 찾아 채운다. LLM 이 지어낸 번호가 BE 까지 가지 못하게 하려는 것이다. 가리킬 수 없으면
(없는 번호, 같은 번호가 둘, 조회 전의 장바구니 번호, 지목한 책 없음) 실행하지 않고 RefError 를 던진다.
"""

from dataclasses import dataclass
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.errors import RefError, ToolErrorCode
from app.agent.schemas import AgentActRequest, ContextCard, ToolName
from app.gateway.commerce import CartLine


class BookRef(BaseModel):
    """LLM 이 도구 인자로 주는 "그 책". {"source": "card", "n": 1} 처럼 쓴다."""

    # 모르는 칸을 막아서 LLM 이 isbn 을 끼워 넣어도 받지 않는다. "1" 같은 문자열 번호도 받지 않는다.
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["card", "cart", "focused"] = Field(
        description=(
            "어느 목록의 책인가. card=직전에 보여 준 추천 카드, cart=마지막으로 조회한 장바구니, "
            "focused=사용자가 지금 보고 있는 책"
        )
    )
    n: int | None = Field(
        default=None,
        ge=1,
        description="그 목록의 몇 번째인지(1부터). focused 에는 쓰지 않는다",
    )

    @model_validator(mode="after")
    def _n_matches_source(self) -> Self:
        if self.source == "focused" and self.n is not None:
            raise ValueError("focused 는 번호가 없다")
        if self.source != "focused" and self.n is None:
            raise ValueError(f"{self.source} 는 번호가 필요하다")
        return self


@dataclass
class TurnContext:
    """한 요청(턴) 동안의 상태. LLM 에게는 보이지 않는다.

    user_id 와 로그인 여부, 직전 카드는 요청에서 온 값을 그대로 쓰고 LLM 이 바꿀 수 없다.
    """

    user_id: int
    logged_in: bool
    cards: list[ContextCard]
    focused_isbn: str | None
    allow_tools: list[ToolName] | None
    # LLM 이 결과를 받아 본 장바구니(BE 조회 순서, 나중에 담은 책이 1번). 본 적이 없으면 None.
    # 장바구니 번호는 LLM 이 본 이 목록 기준으로만 푼다. 그 뒤에 쓰기가 있어도 다시 만들지 않는다.
    # 그래야 한 번에 "1번 담고 장바구니 3번째 빼줘"를 불러도 번호가 밀리지 않는다.
    cart_snapshot: list[CartLine] | None = None
    # 방금 조회했지만 LLM 이 아직 받아 보지 못한 장바구니. 같은 묶음의 뒤 호출이 이 목록으로 번호를 풀면
    # LLM 이 본 적 없는 목록의 n번째가 실행된다. 묶음이 끝나 결과를 LLM 에게 돌려줄 때 commit_cart_view 로 올린다.
    pending_cart: list[CartLine] | None = None

    def commit_cart_view(self) -> None:
        """한 묶음의 도구 결과를 LLM 에게 돌려줄 때 부른다. 이 묶음에서 조회한 장바구니가 다음 번호의 기준이 된다."""
        if self.pending_cart is not None:
            self.cart_snapshot = self.pending_cart
            self.pending_cart = None

    @classmethod
    def from_request(cls, req: AgentActRequest) -> Self:
        return cls(
            user_id=req.user_id,
            logged_in=req.user_context.logged_in,
            cards=list(req.context_cards),
            focused_isbn=req.focused_isbn,
            allow_tools=None if req.allow_tools is None else list(req.allow_tools),
        )


def resolve(ctx: TurnContext, ref: BookRef) -> str:
    """가리킨 책의 ISBN. 하나로 정해지지 않으면 RefError."""
    if ref.source == "card":
        # 사용자에게 보인 번호는 rank 다. 목록의 위치가 아니다
        isbns = {card.isbn for card in ctx.cards if card.rank == ref.n}
        if not isbns:
            raise RefError(ToolErrorCode.BOOK_NOT_IN_CARDS)
        if len(isbns) > 1:
            raise RefError(ToolErrorCode.AMBIGUOUS_BOOK)
        return next(iter(isbns))

    if ref.source == "cart":
        if ctx.cart_snapshot is None:
            raise RefError(ToolErrorCode.CART_NOT_LOADED)
        assert ref.n is not None  # BookRef 가 보장한다
        if ref.n > len(ctx.cart_snapshot):
            raise RefError(ToolErrorCode.BOOK_NOT_IN_CART)
        return ctx.cart_snapshot[ref.n - 1].isbn

    if ctx.focused_isbn is None:
        raise RefError(ToolErrorCode.NO_FOCUSED_BOOK)
    return ctx.focused_isbn
