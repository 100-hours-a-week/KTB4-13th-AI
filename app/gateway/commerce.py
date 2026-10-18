"""⑦ 쇼핑 에이전트가 BE의 장바구니·상품을 부르는 경계(#344 3단계, #347).

도구(app/agent/tools)는 이 Protocol에만 기대고, 진짜 BE 클라이언트(11단계)와 메모리 가짜
(commerce_fake.py)가 같은 모양을 따른다. 그래서 가짜로 만든 평가 숫자가 진짜 BE에서도 같은
의미를 갖는다.

동작 기준은 BE 코드(KTB4-13th-BE main 8faa0c1, core/cart)다. 위키 정책 문서와 어긋나는 곳이
있어 코드를 따랐다(#347). 책은 ISBN으로만 가리킨다(#307). BE 코드는 상품 번호와 장바구니 항목
번호 기준이라, ISBN을 그 번호로 바꾸는 일은 BE의 내부 입구가 맡는 것으로 가정한다(계약 단계에서
확정).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

# BE CartItem.MIN_QUANTITY / MAX_QUANTITY, Cart.MAX_ITEM_COUNT 와 같은 값.
# 수량 0은 "삭제"가 아니라 거절이다(BE CART_RULE 2.2).
MIN_QUANTITY = 1
MAX_QUANTITY = 500
MAX_CART_ITEMS = 30


class CommerceErrorCode(StrEnum):
    """BE ErrorCode의 이름을 그대로 쓴다. 값이 이름과 같아서 로그에 이름만 남는다."""

    CART_NOT_FOUND = "CART_NOT_FOUND"
    CART_ITEM_NOT_FOUND = "CART_ITEM_NOT_FOUND"
    CART_ITEM_LIMIT_EXCEEDED = "CART_ITEM_LIMIT_EXCEEDED"
    INVALID_CART_ITEM_QUANTITY = "INVALID_CART_ITEM_QUANTITY"
    INSUFFICIENT_PRODUCT_STOCK = "INSUFFICIENT_PRODUCT_STOCK"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"


class CommerceError(Exception):
    """BE가 요청을 거절했다. 장바구니는 바뀌지 않았다.

    메시지는 오류 코드 이름뿐이다. 책 제목이나 사용자 발화를 싣지 않아 로그에 남아도 안전하다.
    """

    def __init__(self, code: CommerceErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True)
class CartLine:
    """장바구니 한 줄. 조회가 돌려주는 순서는 BE와 같다(최근에 만든 항목이 위)."""

    isbn: str
    title: str
    # 지금 판매가(BE discountedPrice). 판매 중지 상품은 None이라 합계에 그대로 더하면 안 된다.
    price: int | None
    quantity: int
    # 구매할 수 있나(BE isAvailableForPurchase). 재고가 담은 수량보다 적거나 판매 중지면 False.
    # 재고가 줄어도 담긴 수량은 바뀌지 않는다.
    available: bool


@dataclass(frozen=True)
class BookOffer:
    """가격·재고 조회 결과 한 권."""

    isbn: str
    title: str
    price: int
    in_stock: bool


@runtime_checkable
class CommerceGateway(Protocol):
    """⑦이 BE에 시키는 다섯 가지. 모두 (user_id, isbn) 기준이다.

    실패는 CommerceError로 던진다. 실패한 호출은 장바구니를 바꾸지 않는다.
    """

    async def get_cart(self, user_id: int) -> list[CartLine]:
        """장바구니를 BE 순서대로 돌려준다. 장바구니가 없으면 빈 목록."""
        ...

    async def add_item(self, user_id: int, isbn: str, quantity: int) -> None:
        """담는다. 이미 담긴 책이면 더하지 않고 quantity로 **대체**한다."""
        ...

    async def set_quantity(self, user_id: int, isbn: str, quantity: int) -> None:
        """담긴 책의 수량을 quantity로 바꾼다. 담기지 않은 책은 CART_ITEM_NOT_FOUND."""
        ...

    async def remove_item(self, user_id: int, isbn: str) -> None:
        """장바구니에서 뺀다. 담기지 않은 책은 CART_ITEM_NOT_FOUND."""
        ...

    async def get_offers(self, isbns: Sequence[str]) -> list[BookOffer]:
        """ISBN마다 가격·재고를 돌려준다. 모르는 책은 결과에서 빠진다."""
        ...


@runtime_checkable
class Restorable(Protocol):
    """한 번에 시킨 쓰기를 되돌릴 수 있는 장바구니(#489). 요청으로 받은 복사본과 가짜 BE 가 따른다."""

    def checkpoint(self) -> object:
        """지금 상태. restore 에 그대로 넘긴다."""
        ...

    def restore(self, point: object) -> None:
        """checkpoint 때의 상태로 되돌린다."""
        ...
