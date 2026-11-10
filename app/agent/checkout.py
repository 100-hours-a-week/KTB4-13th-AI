"""⑦ 주문 화면에 미리 채울 쿠폰과 포인트를 고른다(#631). LLM 이 아니라 코드가 계산한다.

BE 결제(KTB4-13th-BE PaymentService.create, PaymentDiscount)와 같은 규칙이다.
- 주문 하나에 쿠폰은 한 장이다. 주문한 책 중 하나라도 그 쿠폰의 대상이면 쓸 수 있다
- 할인은 대상 책이 아니라 주문 전체 금액에 붙는다. 정액은 그 금액, 정률은 주문 금액 × 비율이다. BE 는 정률의
  원 아래를 자르지 않는다(TODO BE#327). 고를 때는 그대로 견주고, 포인트는 원 단위로 내림한다
- 주문 금액이 최소 주문 금액보다 작으면 쓸 수 없다. 할인이 주문 금액보다 크면 결제가 거절된다
- 포인트는 잔액 안에서, 쿠폰을 뺀 금액까지 쓴다

주문은 이번 턴에 담은 책뿐이다. FE 가 이번에 담은 책만 주문 화면에 넣는다(BE#533).
어느 책이 쿠폰 대상인지는 BE 상품 상세의 쿠폰 번호로만 안다. 지금은 이번 턴에 찾은 책(book.search)만 그 번호가
있다. 카드·장바구니 책은 요청에 상품 번호가 없어서 대상을 모르고, 대상이 아닌 것으로 본다(쿠폰을 비워 두면
사용자가 주문 화면에서 고른다).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from app.agent.schemas import OwnedCouponIn


@dataclass(frozen=True)
class OrderLine:
    """주문에 들어갈 책 한 줄."""

    isbn: str
    price: int
    quantity: int
    # 이 책에 쓸 수 있는 쿠폰 번호(coupon_id). 모르면 빈 집합
    coupon_ids: frozenset[int]


@dataclass(frozen=True)
class Plan:
    """주문 화면에 채울 값."""

    amount: int  # 주문 금액
    coupon: OwnedCouponIn | None
    discount: int  # 쿠폰 할인(원 아래 내림, 안내 문장용)
    use_point: int


def discount_of(coupon: OwnedCouponIn, amount: int) -> Decimal:
    """주문 금액 amount 에 이 쿠폰을 쓰면 깎이는 금액. 쿠폰 종류 NONE 은 0이다."""
    if coupon.type == "FIXED_AMOUNT":
        return Decimal(str(coupon.discount))
    if coupon.type == "PERCENT_RATE":
        # 0.1 이 0.1000000000000000055… 로 곱해지지 않게 문자열로 바꿔 계산한다
        return Decimal(amount) * Decimal(str(coupon.discount))
    return Decimal(0)


def usable(coupon: OwnedCouponIn, lines: Sequence[OrderLine], amount: int) -> bool:
    """이 주문에 이 쿠폰을 쓸 수 있는가."""
    if not any(coupon.coupon_id in line.coupon_ids for line in lines):
        return False
    if amount < coupon.min_order_amount:
        return False
    discount = discount_of(coupon, amount)
    return 0 < discount <= amount


def plan(
    lines: Sequence[OrderLine],
    coupons: Sequence[OwnedCouponIn],
    point_balance: int | None,
) -> Plan:
    """할인이 가장 큰 쿠폰 한 장과 쓸 수 있는 포인트 전부. 할인이 같으면 보유 쿠폰 번호가 작은 쪽이다."""
    amount = sum(line.price * line.quantity for line in lines)
    best: OwnedCouponIn | None = None
    best_discount = Decimal(0)
    for coupon in sorted(coupons, key=lambda c: c.owned_coupon_id):
        if not usable(coupon, lines, amount):
            continue
        discount = discount_of(coupon, amount)
        if discount > best_discount:
            best, best_discount = coupon, discount
    left = (Decimal(amount) - best_discount).to_integral_value(rounding=ROUND_FLOOR)
    use_point = max(0, min(point_balance or 0, int(left)))
    shown = int(best_discount.to_integral_value(rounding=ROUND_FLOOR))
    return Plan(amount, best, shown, use_point)
