"""⑦ 이 책 한 권의 정보·리뷰·쿠폰을 읽는 BE 공개 API(#650).

로그인 없이 누구나 읽는 것만 부른다(사용자 권한을 쓰지 않는다, A안). 책은 BE 상품 번호로 읽는다. 카드·장바구니
책의 상품 번호는 BE 가 요청(cart[]·offers[] 의 product_id, BE#536)에 실어 보내고, 찾은 책은 검색 결과에 있다.

- ``GET /api/v1/products/{productId}``: 판매가·정가, 재고 수량, 출판사·출간일·분야, 리뷰 수·평점, 쿠폰 번호
- ``GET /api/v1/reviews?targetType=PRODUCT&targetId=&limit=``: 리뷰 본문과 별점, 스포일러 표시
- ``GET /api/v1/coupons?ids=1&ids=2``: 쿠폰 이름·종류·할인·최소 주문 금액·만료. 쉼표로 붙인 ids 는 BE 가 400 이라
  같은 칸을 되풀이해 보낸다(10/21 dev 실측)

소개글은 읽지 않는다. 소개글은 비교(book.compare)처럼 따로 읽는 호출만 다룬다(소개글 속 지시문이 도구 선택에
섞이지 않게).
"""

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import httpx

from app.gateway.book_finder import USER_AGENT, response_data, whole_won

_PRODUCT_PATH = "/api/v1/products/{product_id}"
_REVIEWS_PATH = "/api/v1/reviews"
_COUPONS_PATH = "/api/v1/coupons"
# BE 리뷰 목록 한 번에 읽는 최대(BE 상한 100)
MAX_REVIEWS = 100

CouponKind = Literal["NONE", "FIXED_AMOUNT", "PERCENT_RATE"]
_KINDS: frozenset[str] = frozenset({"NONE", "FIXED_AMOUNT", "PERCENT_RATE"})


@dataclass(frozen=True)
class ProductDetail:
    """BE 상품 상세에서 쓰는 칸. 값이 없으면 None 이다."""

    product_id: int
    title: str
    author: str | None
    publisher: str | None
    published_at: str | None  # "2023-06-25"
    category: str | None
    price: int  # 판매가(discountedPrice)
    list_price: int | None  # 정가(salePrice)
    stock_quantity: int
    review_count: int
    review_rate: float | None  # 리뷰가 없으면 None(BE 는 0 을 준다)
    # 이 상품이나 분류에 걸린 쿠폰 번호. BE 가 만료 쿠폰도 같이 준다
    coupon_ids: frozenset[int]


@dataclass(frozen=True)
class Review:
    rate: float
    content: str
    spoiler: bool


@dataclass(frozen=True)
class CouponInfo:
    coupon_id: int
    name: str
    kind: CouponKind
    # 정액은 원, 정률은 비율(0.1 = 10%)
    discount: float
    min_order_amount: int
    expired_at: str | None  # "2026-12-01T00:00:00"


class InfoUnavailable(Exception):
    """BE 를 읽지 못했다. 메시지에 사용자 말이나 책 제목을 싣지 않는다."""


@runtime_checkable
class BookInfo(Protocol):
    async def details(self, product_ids: Sequence[int]) -> dict[int, ProductDetail]:
        """상품 상세. 읽지 못한 상품은 빠진다. 모두 실패하면 빈 사전이다."""
        ...

    async def reviews(self, product_id: int, limit: int) -> list[Review]:
        """리뷰를 최근 것부터 limit 개까지. 읽지 못하면 InfoUnavailable."""
        ...

    async def coupons(self, coupon_ids: Sequence[int]) -> list[CouponInfo]:
        """쿠폰 정보. 삭제된 쿠폰은 빠진다. 읽지 못하면 InfoUnavailable."""
        ...


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def parse_detail(product_id: int, data: Any) -> ProductDetail | None:
    """상품 상세 data 를 읽는다. 담는 데 꼭 필요한 값(제목, 판매가, 재고)이 없으면 None."""
    if not isinstance(data, dict):
        return None
    title = _text(data.get("itemName"))
    price = whole_won(data.get("discountedPrice"))
    stock = _int(data.get("stockQuantity"))
    if title is None or price is None or stock is None:
        return None
    count = _int(data.get("reviewCount")) or 0
    rate = _number(data.get("reviewRate"))
    coupons = data.get("couponIds")
    return ProductDetail(
        product_id=product_id,
        title=title,
        author=_text(data.get("author")),
        publisher=_text(data.get("publisher")),
        published_at=_text(data.get("publishedAt")),
        category=_text(data.get("category")),
        price=price,
        list_price=whole_won(data.get("salePrice")),
        stock_quantity=max(0, stock),
        review_count=max(0, count),
        review_rate=rate if count > 0 and rate is not None else None,
        coupon_ids=frozenset(
            c
            for c in (coupons if isinstance(coupons, list) else [])
            if _int(c) is not None
        ),
    )


def parse_reviews(data: Any) -> list[Review]:
    if not isinstance(data, dict) or not isinstance(data.get("content"), list):
        raise InfoUnavailable("reviews shape")
    out = []
    for item in data["content"]:
        if not isinstance(item, dict):
            continue
        rate = _number(item.get("rate"))
        content = item.get("content")
        if rate is None or not isinstance(content, str) or not content.strip():
            continue
        out.append(Review(rate, content.strip(), item.get("isSpoiler") is True))
    return out


def parse_coupons(data: Any) -> list[CouponInfo]:
    if not isinstance(data, list):
        raise InfoUnavailable("coupons shape")
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        coupon_id = _int(item.get("id"))
        name = _text(item.get("name"))
        kind = item.get("type")
        discount = _number(item.get("discount"))
        minimum = whole_won(item.get("minOrderAmount"))
        if (
            coupon_id is None
            or name is None
            or kind not in _KINDS
            or discount is None
            or minimum is None
        ):
            continue
        out.append(
            CouponInfo(
                coupon_id,
                name,
                kind,  # type: ignore[arg-type]
                discount,
                minimum,
                _text(item.get("expiredAt")),
            )
        )
    return out


class HttpBookInfo:
    """BE 공개 API 를 부른다. 상세는 상품마다 한 번씩 동시에 부른다."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport  # 시험에서 가짜 응답을 넣는다

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
            headers={"User-Agent": USER_AGENT},
        )

    async def details(self, product_ids: Sequence[int]) -> dict[int, ProductDetail]:
        ids = list(dict.fromkeys(product_ids))
        if not ids:
            return {}
        async with self._client() as client:
            found = await asyncio.gather(*(self._detail(client, pid) for pid in ids))
        return {d.product_id: d for d in found if d is not None}

    async def _detail(
        self, client: httpx.AsyncClient, product_id: int
    ) -> ProductDetail | None:
        try:
            response = await client.get(_PRODUCT_PATH.format(product_id=product_id))
        except httpx.HTTPError:
            return None
        return parse_detail(product_id, response_data(response))

    async def reviews(self, product_id: int, limit: int) -> list[Review]:
        params = {
            "targetType": "PRODUCT",
            "targetId": product_id,
            "limit": max(1, min(limit, MAX_REVIEWS)),
        }
        async with self._client() as client:
            try:
                response = await client.get(_REVIEWS_PATH, params=params)
            except httpx.HTTPError as e:
                raise InfoUnavailable(type(e).__name__) from None
        return parse_reviews(response_data(response))

    async def coupons(self, coupon_ids: Sequence[int]) -> list[CouponInfo]:
        ids = list(dict.fromkeys(coupon_ids))
        if not ids:
            return []
        async with self._client() as client:
            try:
                response = await client.get(_COUPONS_PATH, params={"ids": ids})
            except httpx.HTTPError as e:
                raise InfoUnavailable(type(e).__name__) from None
        return parse_coupons(response_data(response))


class FakeBookInfo:
    """로컬 시험과 평가에 쓰는 가공의 상품 정보. 부른 상품 번호를 남긴다."""

    def __init__(
        self,
        details: Iterable[ProductDetail] = (),
        reviews: dict[int, list[Review]] | None = None,
        coupons: Iterable[CouponInfo] = (),
        *,
        fail: bool = False,
    ) -> None:
        self._details = {d.product_id: d for d in details}
        self._reviews = reviews or {}
        self._coupons = {c.coupon_id: c for c in coupons}
        self._fail = fail
        self.detail_calls: list[list[int]] = []

    async def details(self, product_ids: Sequence[int]) -> dict[int, ProductDetail]:
        self.detail_calls.append(list(product_ids))
        if self._fail:
            return {}
        return {pid: self._details[pid] for pid in product_ids if pid in self._details}

    async def reviews(self, product_id: int, limit: int) -> list[Review]:
        if self._fail:
            raise InfoUnavailable("fake")
        return self._reviews.get(product_id, [])[:limit]

    async def coupons(self, coupon_ids: Sequence[int]) -> list[CouponInfo]:
        if self._fail:
            raise InfoUnavailable("fake")
        return [self._coupons[c] for c in coupon_ids if c in self._coupons]
