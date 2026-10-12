"""메모리에 장바구니를 들고 BE 코드(core/cart)대로 움직이는 가짜(#344 3단계, #347).

개발과 평가에 같이 쓴다. 평가는 도구를 부른 순서가 아니라 마지막 장바구니 상태가 기대와 같은지로
채점하므로, 그 상태를 읽는 cart_state()를 둔다.

BE처럼 삭제한 항목을 지우지 않고 표시만 한다. 그래야 삭제 후 다시 담았을 때 원래 자리로 돌아오는
BE의 조회 순서(created_at 내림차순)가 같이 재현된다.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.gateway.commerce import (
    MAX_CART_ITEMS,
    MAX_QUANTITY,
    MIN_QUANTITY,
    BookOffer,
    CartLine,
    CommerceError,
    CommerceErrorCode,
)


@dataclass
class FakeBook:
    isbn: str
    title: str
    price: int
    stock: int
    # False면 판매 중지. 장바구니에는 남지만 가격이 없다(BE GetCartUseCase).
    active: bool = True


@dataclass
class _Row:
    seq: int  # 만든 순서. BE의 created_at·id 내림차순 조회를 대신한다
    quantity: int
    deleted: bool = False


class FakeCommerce:
    def __init__(self, books: Iterable[FakeBook], users: Iterable[int] = ()) -> None:
        self._books = {book.isbn: book for book in books}
        # BE는 가입할 때 장바구니를 만든다. 여기 없는 사용자는 장바구니가 없는 사용자다.
        self._carts: dict[int, dict[str, _Row]] = {user_id: {} for user_id in users}
        self._seq = 0

    # --- CommerceGateway ---

    async def get_cart(self, user_id: int) -> list[CartLine]:
        rows = self._carts.get(user_id, {})
        active = [(isbn, row) for isbn, row in rows.items() if not row.deleted]
        active.sort(key=lambda pair: pair[1].seq, reverse=True)
        return [self._line(isbn, row) for isbn, row in active]

    async def add_item(self, user_id: int, isbn: str, quantity: int) -> None:
        cart = self._carts.get(user_id)
        if cart is None:
            raise CommerceError(CommerceErrorCode.CART_NOT_FOUND)
        _validate_quantity(quantity)
        self._validate_stock(isbn, quantity)

        row = cart.get(isbn)
        # 새 책도, 삭제했던 책을 되살리는 것도 활성 책이 30권이면 못 넣는다
        adds_new_slot = row is None or row.deleted
        if adds_new_slot and _active_count(cart) >= MAX_CART_ITEMS:
            raise CommerceError(CommerceErrorCode.CART_ITEM_LIMIT_EXCEEDED)
        if row is None:
            self._seq += 1
            cart[isbn] = _Row(seq=self._seq, quantity=quantity)
            return
        row.deleted = False  # 되살려도 seq는 그대로라 원래 자리로 돌아온다
        row.quantity = quantity

    async def set_quantity(self, user_id: int, isbn: str, quantity: int) -> None:
        row = self._active_row(user_id, isbn)
        _validate_quantity(quantity)
        self._validate_stock(isbn, quantity)
        row.quantity = quantity

    async def remove_item(self, user_id: int, isbn: str) -> None:
        self._active_row(user_id, isbn).deleted = True

    async def get_offers(self, isbns: Sequence[str]) -> list[BookOffer]:
        offers: list[BookOffer] = []
        for isbn in dict.fromkeys(isbns):  # 중복은 한 번만, 받은 순서 유지
            book = self._books.get(isbn)
            if book is not None and book.active:
                offers.append(BookOffer(isbn, book.title, book.price, book.stock > 0))
        return offers

    # --- 평가·테스트용. CommerceGateway에는 없다 ---

    def cart_state(self, user_id: int) -> dict[str, int]:
        """마지막 장바구니 상태 {isbn: 수량}. 평가가 기대 상태와 비교한다."""
        rows = self._carts.get(user_id, {})
        return {isbn: row.quantity for isbn, row in rows.items() if not row.deleted}

    def set_stock(self, isbn: str, stock: int) -> None:
        self._books[isbn].stock = stock

    def discontinue(self, isbn: str) -> None:
        self._books[isbn].active = False

    # --- 내부 ---

    def _active_row(self, user_id: int, isbn: str) -> _Row:
        row = self._carts.get(user_id, {}).get(isbn)
        if row is None or row.deleted:
            raise CommerceError(CommerceErrorCode.CART_ITEM_NOT_FOUND)
        return row

    def _validate_stock(self, isbn: str, quantity: int) -> None:
        book = self._books.get(isbn)
        # 판매 중지 상품이 PRODUCT_NOT_FOUND인지는 BE 코드에서 확인하지 못한 가정이다(#347)
        if book is None or not book.active:
            raise CommerceError(CommerceErrorCode.PRODUCT_NOT_FOUND)
        if book.stock < quantity:
            raise CommerceError(CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK)

    def _line(self, isbn: str, row: _Row) -> CartLine:
        book = self._books[isbn]
        return CartLine(
            isbn=isbn,
            title=book.title,
            price=book.price if book.active else None,
            quantity=row.quantity,
            available=book.active and book.stock >= row.quantity,
        )


def _validate_quantity(quantity: int) -> None:
    if not MIN_QUANTITY <= quantity <= MAX_QUANTITY:
        raise CommerceError(CommerceErrorCode.INVALID_CART_ITEM_QUANTITY)


def _active_count(cart: dict[str, _Row]) -> int:
    return sum(1 for row in cart.values() if not row.deleted)
