"""가짜 BE(FakeCommerce)가 BE 장바구니 코드(core/cart)대로 움직이는지 본다(#347).

가짜가 실제와 다르면 ⑦ 평가 숫자가 거짓이 된다. 그래서 BE가 하는 일을 하나씩 테스트로 옮겨 두고,
BE가 바뀌면 여기부터 고친다. 기준은 BE main 8faa0c1이다. 네트워크도 DB도 쓰지 않는다.
"""

import asyncio

import pytest

from app.gateway.commerce import (
    MAX_CART_ITEMS,
    BookOffer,
    CartLine,
    CommerceError,
    CommerceErrorCode,
    CommerceGateway,
)
from app.gateway.commerce_fake import FakeBook, FakeCommerce


def _isbn(n: int) -> str:
    return f"97889{n:08d}"  # 13자리 숫자


A, B, C, D = _isbn(1), _isbn(2), _isbn(3), _isbn(4)


def _fake(users=(1, 2), count=35, stock=1000) -> FakeCommerce:
    books = [
        FakeBook(_isbn(i), f"책{i}", 10000 + i, stock) for i in range(1, count + 1)
    ]
    return FakeCommerce(books, users=users)


def _run(coro):
    return asyncio.run(coro)


def _code(coro) -> CommerceErrorCode:
    """호출이 CommerceError로 실패해야 한다. 그때의 오류 코드를 돌려준다."""
    with pytest.raises(CommerceError) as exc_info:
        asyncio.run(coro)
    return exc_info.value.code


def _isbns(fake: FakeCommerce, user_id: int = 1) -> list[str]:
    return [line.isbn for line in _run(fake.get_cart(user_id))]


# --- 담기 ---


def test_담으면_조회에_보인다() -> None:
    fake = _fake()

    _run(fake.add_item(1, A, 2))

    assert _run(fake.get_cart(1)) == [CartLine(A, "책1", 10001, 2, True)]


def test_이미_담긴_책을_다시_담으면_더하지_않고_대체한다() -> None:
    # 정책 문서는 "합산"이지만 BE 코드는 대체다. 2권 담긴 책에 3권을 담으면 5권이 아니라 3권.
    fake = _fake()
    _run(fake.add_item(1, A, 2))

    _run(fake.add_item(1, A, 3))

    assert fake.cart_state(1) == {A: 3}


@pytest.mark.parametrize("quantity", [0, -1, 501])
def test_수량이_1에서_500_밖이면_거절하고_장바구니를_안_바꾼다(quantity) -> None:
    fake = _fake()
    _run(fake.add_item(1, A, 2))

    assert (
        _code(fake.add_item(1, A, quantity))
        == CommerceErrorCode.INVALID_CART_ITEM_QUANTITY
    )
    assert fake.cart_state(1) == {A: 2}


@pytest.mark.parametrize("quantity", [1, 500])
def test_수량_경계값은_담긴다(quantity) -> None:
    fake = _fake()

    _run(fake.add_item(1, A, quantity))

    assert fake.cart_state(1) == {A: quantity}


def test_재고보다_많이_담으면_거절하고_장바구니를_안_바꾼다() -> None:
    fake = _fake(stock=3)

    assert _code(fake.add_item(1, A, 4)) == CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK
    assert fake.cart_state(1) == {}

    _run(fake.add_item(1, A, 3))  # 재고와 같은 수량은 된다
    assert fake.cart_state(1) == {A: 3}


@pytest.mark.parametrize("quantity", [0, 600])
def test_수량_검사가_재고_검사보다_먼저다(quantity) -> None:
    # BE는 validateQuantity 다음에 validateAvailableStock을 부른다. 재고 3인 책에 600권을
    # 담으면 "재고 부족"이 아니라 "수량이 잘못됨"으로 거절된다.
    fake = _fake(stock=3)

    assert (
        _code(fake.add_item(1, A, quantity))
        == CommerceErrorCode.INVALID_CART_ITEM_QUANTITY
    )


def test_모르는_책을_담으면_PRODUCT_NOT_FOUND다() -> None:
    fake = _fake()

    assert _code(fake.add_item(1, _isbn(999), 1)) == CommerceErrorCode.PRODUCT_NOT_FOUND


# --- 30권 한도 ---


def _fill(fake: FakeCommerce, count: int = MAX_CART_ITEMS) -> None:
    for i in range(1, count + 1):
        _run(fake.add_item(1, _isbn(i), 1))


def test_서로_다른_책은_30권까지만_담긴다() -> None:
    fake = _fake()
    _fill(fake)

    assert (
        _code(fake.add_item(1, _isbn(31), 1))
        == CommerceErrorCode.CART_ITEM_LIMIT_EXCEEDED
    )
    assert len(fake.cart_state(1)) == MAX_CART_ITEMS


def test_30권이_차도_이미_담긴_책의_수량은_바꿀_수_있다() -> None:
    fake = _fake()
    _fill(fake)

    _run(fake.add_item(1, A, 5))  # 대체
    _run(fake.set_quantity(1, B, 7))

    assert fake.cart_state(1)[A] == 5
    assert fake.cart_state(1)[B] == 7


def test_책을_빼면_한도에_자리가_난다() -> None:
    fake = _fake()
    _fill(fake)
    _run(fake.remove_item(1, A))

    _run(fake.add_item(1, _isbn(31), 1))

    assert len(fake.cart_state(1)) == MAX_CART_ITEMS


# --- 수량 변경 ---


def test_수량_변경은_담긴_책만_된다() -> None:
    fake = _fake()

    assert _code(fake.set_quantity(1, A, 2)) == CommerceErrorCode.CART_ITEM_NOT_FOUND

    _run(fake.add_item(1, A, 1))
    _run(fake.set_quantity(1, A, 4))
    assert fake.cart_state(1) == {A: 4}


def test_수량_0은_삭제가_아니라_거절이다() -> None:
    fake = _fake()
    _run(fake.add_item(1, A, 2))

    assert (
        _code(fake.set_quantity(1, A, 0))
        == CommerceErrorCode.INVALID_CART_ITEM_QUANTITY
    )
    assert fake.cart_state(1) == {A: 2}


def test_수량_변경도_재고를_다시_본다() -> None:
    fake = _fake(stock=3)
    _run(fake.add_item(1, A, 2))

    assert (
        _code(fake.set_quantity(1, A, 4))
        == CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK
    )
    assert fake.cart_state(1) == {A: 2}


# --- 빼기 ---


def test_빼면_조회에서_사라진다() -> None:
    fake = _fake()
    _run(fake.add_item(1, A, 1))
    _run(fake.add_item(1, B, 1))

    _run(fake.remove_item(1, A))

    assert _isbns(fake) == [B]
    assert fake.cart_state(1) == {B: 1}


def test_담기지_않은_책이나_이미_뺀_책을_빼면_CART_ITEM_NOT_FOUND다() -> None:
    fake = _fake()
    assert _code(fake.remove_item(1, A)) == CommerceErrorCode.CART_ITEM_NOT_FOUND

    _run(fake.add_item(1, A, 1))
    _run(fake.remove_item(1, A))
    assert _code(fake.remove_item(1, A)) == CommerceErrorCode.CART_ITEM_NOT_FOUND


# --- 조회 순서: "장바구니 2번째"가 맞게 풀리려면 BE와 같아야 한다 ---


def test_새로_담은_책이_맨_위다() -> None:
    fake = _fake()
    for isbn in (A, B, C):
        _run(fake.add_item(1, isbn, 1))

    assert _isbns(fake) == [C, B, A]


def test_수량을_바꿔도_자리는_그대로다() -> None:
    fake = _fake()
    for isbn in (A, B, C):
        _run(fake.add_item(1, isbn, 1))

    _run(fake.set_quantity(1, A, 5))  # 수량 변경
    _run(fake.add_item(1, B, 9))  # 이미 담긴 책을 다시 담아 대체

    assert _isbns(fake) == [C, B, A]


def test_뺐다가_다시_담으면_원래_자리로_돌아온다() -> None:
    # BE는 삭제를 표시만 하고, 다시 담으면 그 항목을 되살린다. 조회 순서가 created_at이라 맨 위가
    # 아니라 처음 담았던 자리다.
    fake = _fake()
    for isbn in (A, B, C):
        _run(fake.add_item(1, isbn, 1))
    _run(fake.remove_item(1, A))
    _run(fake.add_item(1, D, 1))
    assert _isbns(fake) == [D, C, B]

    _run(fake.add_item(1, A, 1))

    assert _isbns(fake) == [D, C, B, A]


def test_되살린_책의_수량은_요청값으로_대체된다() -> None:
    fake = _fake()
    _run(fake.add_item(1, A, 5))
    _run(fake.remove_item(1, A))

    _run(fake.add_item(1, A, 2))

    assert fake.cart_state(1) == {A: 2}


def test_되살리기도_30권_한도를_본다() -> None:
    fake = _fake()
    _fill(fake)
    _run(fake.remove_item(1, A))
    _run(fake.add_item(1, _isbn(31), 1))  # 빈 자리를 다른 책이 채웠다

    assert _code(fake.add_item(1, A, 1)) == CommerceErrorCode.CART_ITEM_LIMIT_EXCEEDED
    assert A not in fake.cart_state(1)


# --- 사용자 ---


def test_장바구니가_없는_사용자는_담기만_CART_NOT_FOUND다() -> None:
    fake = _fake(users=(1,))

    assert _code(fake.add_item(99, A, 1)) == CommerceErrorCode.CART_NOT_FOUND
    # 조회는 빈 목록, 수량 변경과 빼기는 항목이 없다는 오류다(BE 코드가 그렇게 갈린다)
    assert _run(fake.get_cart(99)) == []
    assert _code(fake.set_quantity(99, A, 1)) == CommerceErrorCode.CART_ITEM_NOT_FOUND
    assert _code(fake.remove_item(99, A)) == CommerceErrorCode.CART_ITEM_NOT_FOUND


def test_사용자끼리_장바구니가_섞이지_않는다() -> None:
    fake = _fake()
    _run(fake.add_item(1, A, 2))

    assert _run(fake.get_cart(2)) == []
    assert _code(fake.set_quantity(2, A, 1)) == CommerceErrorCode.CART_ITEM_NOT_FOUND
    assert _code(fake.remove_item(2, A)) == CommerceErrorCode.CART_ITEM_NOT_FOUND

    _run(fake.add_item(2, A, 7))
    assert fake.cart_state(1) == {A: 2}
    assert fake.cart_state(2) == {A: 7}


# --- 재고가 줄거나 판매가 중지된 뒤의 조회 ---


def test_재고가_줄어도_담긴_수량은_그대로이고_구매_가능만_꺼진다() -> None:
    fake = _fake()
    _run(fake.add_item(1, A, 5))

    fake.set_stock(A, 2)

    assert _run(fake.get_cart(1)) == [CartLine(A, "책1", 10001, 5, False)]
    assert fake.cart_state(1) == {A: 5}

    fake.set_stock(A, 5)
    assert _run(fake.get_cart(1))[0].available is True


def test_판매_중지된_책은_장바구니에_남지만_가격이_없다() -> None:
    fake = _fake()
    _run(fake.add_item(1, A, 2))

    fake.discontinue(A)

    assert _run(fake.get_cart(1)) == [CartLine(A, "책1", None, 2, False)]
    assert _code(fake.add_item(2, A, 1)) == CommerceErrorCode.PRODUCT_NOT_FOUND
    assert _run(fake.get_offers([A])) == []


# --- 가격·재고 조회 ---


def test_가격과_재고를_돌려준다() -> None:
    fake = _fake()

    assert _run(fake.get_offers([A])) == [BookOffer(A, "책1", 10001, True)]


def test_재고가_0이면_in_stock이_False다() -> None:
    fake = _fake(stock=0)

    assert _run(fake.get_offers([A]))[0].in_stock is False


def test_모르는_책은_결과에서_빠지고_중복은_한_번만_순서를_지킨다() -> None:
    fake = _fake()

    offers = _run(fake.get_offers([B, _isbn(999), A, B]))

    assert [offer.isbn for offer in offers] == [B, A]


# --- 계약 ---


def test_FakeCommerce는_CommerceGateway_모양을_따른다() -> None:
    assert isinstance(_fake(), CommerceGateway)


def test_오류_메시지는_코드_이름뿐이다() -> None:
    # 책 제목·사용자 발화를 싣지 않아 로그에 남아도 안전하다
    fake = _fake(stock=0)

    with pytest.raises(CommerceError) as exc_info:
        _run(fake.add_item(1, A, 1))

    assert str(exc_info.value) == "INSUFFICIENT_PRODUCT_STOCK"
