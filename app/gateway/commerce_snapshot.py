"""BE 가 요청에 실어 보낸 장바구니와 가격·재고로 만든, 이번 턴만 쓰는 장바구니(#489, A안).

AI 는 BE 장바구니를 바꾸지 않는다. 이 복사본 위에서 ⑦ 을 그대로 돌리고, 끝나면 처음과 끝의 차이를 "바꿀 것
목록(actions)"으로 돌려준다. 검증과 실제 변경은 BE 가 한다(BE#284). AI 가 BE 를 부르지 않으므로 서비스 토큰만으로
아무 사용자의 장바구니를 바꿀 수 있는 길이 없다.

규칙은 가짜 BE(commerce_fake.py)와 같다: 담기는 요청 수량으로 바꾸기, 수량 1~500, 서로 다른 책 30권, 나중에 담은
책이 위. 다만 재고는 수량이 아니라 있고 없음만 받는다. 재고보다 많이 담는 것은 BE 가 반영할 때 거절한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass, replace

from app.gateway.commerce import (
    MAX_CART_ITEMS,
    MAX_QUANTITY,
    MIN_QUANTITY,
    BookOffer,
    CartLine,
    CommerceError,
    CommerceErrorCode,
)


@dataclass(frozen=True)
class CartAction:
    """BE 가 반영할 변경 하나. set 은 그 책을 quantity 권으로(없으면 새로 담음), remove 는 빼기."""

    op: str  # "set" | "remove"
    isbn: str
    quantity: int | None = None


class SnapshotCommerce:
    """CommerceGateway 와 같은 모양이라 도구는 이것이 복사본인지 모른다. user_id 는 받기만 하고 쓰지 않는다
    (요청 하나가 사용자 한 명의 장바구니만 싣는다)."""

    def __init__(self, cart: Sequence[CartLine], offers: Sequence[BookOffer]) -> None:
        self._start = list(cart)
        self._lines = list(cart)
        # 가격·재고는 받은 것만 안다. 카드 책은 offers 에, 장바구니 책은 cart 에 있다
        self._offers = {offer.isbn: offer for offer in offers}

    # --- CommerceGateway ---

    async def get_cart(self, user_id: int) -> list[CartLine]:
        return list(self._lines)

    async def add_item(self, user_id: int, isbn: str, quantity: int) -> None:
        _validate_quantity(quantity)
        existing = self._find(isbn)
        if existing is not None:
            self._check_sellable(existing, quantity)
            self._lines[self._lines.index(existing)] = replace(
                existing, quantity=quantity
            )
            return
        offer = self._offers.get(isbn)
        if offer is None:
            raise CommerceError(CommerceErrorCode.PRODUCT_NOT_FOUND)
        if not offer.in_stock:
            raise CommerceError(CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK)
        if len(self._lines) >= MAX_CART_ITEMS:
            raise CommerceError(CommerceErrorCode.CART_ITEM_LIMIT_EXCEEDED)
        # BE 조회 순서는 나중에 담은 책이 위다
        self._lines.insert(0, CartLine(isbn, offer.title, offer.price, quantity, True))

    async def set_quantity(self, user_id: int, isbn: str, quantity: int) -> None:
        existing = self._find(isbn)
        if existing is None:
            raise CommerceError(CommerceErrorCode.CART_ITEM_NOT_FOUND)
        _validate_quantity(quantity)
        self._check_sellable(existing, quantity)
        self._lines[self._lines.index(existing)] = replace(existing, quantity=quantity)

    async def remove_item(self, user_id: int, isbn: str) -> None:
        existing = self._find(isbn)
        if existing is None:
            raise CommerceError(CommerceErrorCode.CART_ITEM_NOT_FOUND)
        self._lines.remove(existing)

    async def get_offers(self, isbns: Sequence[str]) -> list[BookOffer]:
        found: list[BookOffer] = []
        for isbn in dict.fromkeys(isbns):  # 중복은 한 번만, 받은 순서 유지
            offer = self._offers.get(isbn)
            line = self._find_in(self._start, isbn)
            if offer is None and line is not None and line.price is not None:
                # 장바구니에만 있는 책은 장바구니 줄의 값으로 답한다. 구매할 수 있으면 재고가 있다
                offer = BookOffer(isbn, line.title, line.price, line.available)
            if offer is not None:
                found.append(offer)
        return found

    # --- 이번 턴의 변경 ---

    def actions(self) -> list[CartAction]:
        return diff_cart(self._start, self._lines)

    def checkpoint(self) -> object:
        return list(self._lines)

    def restore(self, point: object) -> None:
        self._lines = list(point)  # type: ignore[call-overload]

    # --- 내부 ---

    def _find(self, isbn: str) -> CartLine | None:
        return self._find_in(self._lines, isbn)

    @staticmethod
    def _find_in(lines: Sequence[CartLine], isbn: str) -> CartLine | None:
        return next((line for line in lines if line.isbn == isbn), None)

    def _check_sellable(self, line: CartLine, quantity: int) -> None:
        """판매 중지 책(가격 없음)은 수량을 바꿀 수 없다. 수량을 늘릴 때 재고가 없다고 알려진 책도 막는다."""
        if line.price is None:
            raise CommerceError(CommerceErrorCode.PRODUCT_NOT_FOUND)
        offer = self._offers.get(line.isbn)
        if quantity > line.quantity and offer is not None and not offer.in_stock:
            raise CommerceError(CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK)


def diff_cart(start: Sequence[CartLine], end: Sequence[CartLine]) -> list[CartAction]:
    """처음과 끝 장바구니의 차이. 빼기를 먼저 둬서 30권 한도에 걸리지 않게 하고, 새로 담은 책은 먼저 담은 것부터
    둬서 BE 가 차례로 반영해도 조회 순서(나중에 담은 책이 위)가 같아진다."""
    before = {line.isbn: line.quantity for line in start}
    after = {line.isbn: line.quantity for line in end}
    removes = [CartAction("remove", isbn) for isbn in before if isbn not in after]
    sets = [
        CartAction("set", line.isbn, line.quantity)
        for line in reversed(end)
        if before.get(line.isbn) != line.quantity
    ]
    return removes + sets


def _validate_quantity(quantity: int) -> None:
    if not MIN_QUANTITY <= quantity <= MAX_QUANTITY:
        raise CommerceError(CommerceErrorCode.INVALID_CART_ITEM_QUANTITY)
