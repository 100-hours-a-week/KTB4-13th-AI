"""요청에 실린 장바구니로 만든 이번 턴의 복사본(app/gateway/commerce_snapshot.py) 테스트(#489).

규칙은 가짜 BE(commerce_fake.py)와 같다. 다른 점은 재고를 있고 없음으로만 아는 것이다.
"""

import asyncio

import pytest

from app.gateway.commerce import (
    MAX_CART_ITEMS,
    BookOffer,
    CartLine,
    CommerceError,
    CommerceErrorCode,
)
from app.gateway.commerce_snapshot import CartAction, SnapshotCommerce, diff_cart

UID = 1
A, B, C = "9788900000011", "9788900000028", "9788900000035"


def _line(isbn: str, quantity: int = 1, price: int | None = 15000) -> CartLine:
    return CartLine(isbn, f"책{isbn[-2:]}", price, quantity, price is not None)


def _offer(isbn: str, in_stock: bool = True) -> BookOffer:
    return BookOffer(isbn, f"책{isbn[-2:]}", 15000, in_stock)


def _run(coro):
    return asyncio.run(coro)


def _cart(snap: SnapshotCommerce) -> list[tuple[str, int]]:
    return [(line.isbn, line.quantity) for line in _run(snap.get_cart(UID))]


def _code(coro) -> CommerceErrorCode:
    with pytest.raises(CommerceError) as e:
        _run(coro)
    return e.value.code


# --- 장바구니 규칙 ---


def test_새_책을_담으면_맨_위에_온다() -> None:
    snap = SnapshotCommerce([_line(B)], [_offer(A)])

    _run(snap.add_item(UID, A, 2))

    assert _cart(snap) == [(A, 2), (B, 1)]


def test_담긴_책을_담으면_더하지_않고_요청_수량으로_바꾼다() -> None:
    snap = SnapshotCommerce([_line(A, 3)], [])

    _run(snap.add_item(UID, A, 2))

    assert _cart(snap) == [(A, 2)]


def test_가격을_모르는_책은_담지_못한다() -> None:
    snap = SnapshotCommerce([], [])

    assert _code(snap.add_item(UID, A, 1)) == CommerceErrorCode.PRODUCT_NOT_FOUND


def test_재고가_없는_책은_담지_못한다() -> None:
    snap = SnapshotCommerce([], [_offer(A, in_stock=False)])

    assert (
        _code(snap.add_item(UID, A, 1)) == CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK
    )


def test_서로_다른_책이_30권이면_새_책을_못_담는다() -> None:
    full = [_line(f"97911{n:08d}") for n in range(MAX_CART_ITEMS)]
    snap = SnapshotCommerce(full, [_offer(A)])

    assert _code(snap.add_item(UID, A, 1)) == CommerceErrorCode.CART_ITEM_LIMIT_EXCEEDED


@pytest.mark.parametrize("quantity", [0, 501])
def test_수량은_1에서_500까지다(quantity: int) -> None:
    snap = SnapshotCommerce([_line(A)], [_offer(B)])

    assert (
        _code(snap.add_item(UID, B, quantity))
        == CommerceErrorCode.INVALID_CART_ITEM_QUANTITY
    )
    assert (
        _code(snap.set_quantity(UID, A, quantity))
        == CommerceErrorCode.INVALID_CART_ITEM_QUANTITY
    )


def test_담기지_않은_책은_수량을_바꾸거나_뺄_수_없다() -> None:
    snap = SnapshotCommerce([_line(A)], [])

    assert _code(snap.set_quantity(UID, B, 2)) == CommerceErrorCode.CART_ITEM_NOT_FOUND
    assert _code(snap.remove_item(UID, B)) == CommerceErrorCode.CART_ITEM_NOT_FOUND


def test_판매_중지된_책은_수량을_바꿀_수_없지만_뺄_수는_있다() -> None:
    snap = SnapshotCommerce([_line(A, price=None)], [])

    assert _code(snap.set_quantity(UID, A, 2)) == CommerceErrorCode.PRODUCT_NOT_FOUND
    _run(snap.remove_item(UID, A))
    assert _cart(snap) == []


def test_재고가_없다고_알려진_책은_수량을_늘리지_못하고_줄이는_것은_된다() -> None:
    snap = SnapshotCommerce([_line(A, 3)], [_offer(A, in_stock=False)])

    assert (
        _code(snap.set_quantity(UID, A, 4))
        == CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK
    )
    _run(snap.set_quantity(UID, A, 1))
    assert _cart(snap) == [(A, 1)]


def test_장바구니에만_있는_책의_가격은_장바구니_줄로_답한다() -> None:
    snap = SnapshotCommerce([_line(A, price=12000)], [_offer(B)])

    offers = _run(snap.get_offers([B, A, C]))

    assert [(o.isbn, o.price) for o in offers] == [(B, 15000), (A, 12000)]


# --- 바꿀 것 목록 ---


def test_바꾼_것이_없으면_빈_목록이다() -> None:
    snap = SnapshotCommerce([_line(A)], [_offer(B)])

    _run(snap.get_cart(UID))

    assert snap.actions() == []


def test_빼기를_먼저_두고_새로_담은_책은_먼저_담은_것부터_둔다() -> None:
    # BE 가 차례로 반영해도 30권 한도에 먼저 걸리지 않고, 조회 순서(나중에 담은 책이 위)가 같아진다
    snap = SnapshotCommerce([_line(C)], [_offer(A), _offer(B)])

    _run(snap.add_item(UID, A, 1))
    _run(snap.add_item(UID, B, 2))
    _run(snap.remove_item(UID, C))

    assert snap.actions() == [
        CartAction("remove", C),
        CartAction("set", A, 1),
        CartAction("set", B, 2),
    ]


def test_수량만_바꾸면_set_하나다() -> None:
    snap = SnapshotCommerce([_line(A, 3), _line(B)], [])

    _run(snap.set_quantity(UID, A, 2))

    assert snap.actions() == [CartAction("set", A, 2)]


def test_바꿨다가_되돌리면_바꿀_것이_없다() -> None:
    snap = SnapshotCommerce([_line(A, 3)], [])

    _run(snap.set_quantity(UID, A, 2))
    _run(snap.set_quantity(UID, A, 3))

    assert snap.actions() == []


def test_되돌리기_지점으로_돌아갈_수_있다() -> None:
    snap = SnapshotCommerce([_line(A)], [_offer(B)])
    point = snap.checkpoint()

    _run(snap.add_item(UID, B, 1))
    snap.restore(point)

    assert _cart(snap) == [(A, 1)]
    assert snap.actions() == []


def test_diff_cart는_어느_장바구니에도_쓸_수_있다() -> None:
    assert diff_cart([_line(A), _line(B)], [_line(B, 2)]) == [
        CartAction("remove", A),
        CartAction("set", B, 2),
    ]
