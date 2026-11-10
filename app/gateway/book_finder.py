"""⑦ 이 카드 밖의 책을 찾을 때 읽는 BE 공개 API(#631).

AI 는 BE 장바구니를 바꾸지 않는다(A안, #489). 여기서 부르는 것은 로그인 없이 누구나 읽는 검색과 상품 상세뿐이라
사용자 권한을 쓰지 않는다. 찾은 책을 담는 것은 지금처럼 actions 로 돌려주고, BE 가 검사해 반영한다(BE#533).

- ``GET /api/v1/search?query=&size=``: 책의 ISBN 과 상품 번호. 가격은 AI 검색의 값이라 여기서 쓰지 않는다
- ``GET /api/v1/products/{productId}``: 판매가(discountedPrice), 재고 수량, 이 상품에 쓸 수 있는 쿠폰 번호

검색 결과에 ISBN 이 실리는 것은 BE#533 부터다. ISBN 이 없는 결과는 담을 수 없어 버린다.
"""

import asyncio
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

import httpx

from app.core.isbn import ISBN13_PATTERN

_SEARCH_PATH = "/api/v1/search"
_PRODUCT_PATH = "/api/v1/products/{product_id}"
USER_AGENT = "bookjeok-ai"
_ISBN13 = re.compile(ISBN13_PATTERN)


@dataclass(frozen=True)
class FoundBook:
    """찾은 책 한 권. 가격과 재고, 쿠폰은 BE 상품 상세의 값이다."""

    isbn: str
    product_id: int
    title: str
    author: str | None
    price: int  # BE 판매가(discountedPrice)
    in_stock: bool  # 재고가 1권 이상
    # 이 상품에 쓸 수 있는 쿠폰 번호(상품이나 분류가 대상인 쿠폰). BE 가 만료 쿠폰도 같이 준다
    coupon_ids: frozenset[int]


class FinderUnavailable(Exception):
    """BE 검색을 읽지 못했다. 메시지에 검색어를 싣지 않는다."""


@runtime_checkable
class BookFinder(Protocol):
    async def find(self, query: str, size: int) -> list[FoundBook]:
        """검색어로 책을 찾는다. 담을 수 없는 책(ISBN·상품 번호가 없거나 상세를 못 읽은 책)은 빠진다.

        검색 자체가 실패하면 FinderUnavailable.
        """
        ...


def whole_won(value: object) -> int | None:
    """BE 금액(BigDecimal, JSON 으로 13500 이나 13500.00)을 원 단위 정수로. 원 아래가 있으면 None."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if amount < 0 or amount != amount.to_integral_value():
        return None
    return int(amount)


def response_data(response: httpx.Response) -> Any:
    """BE 공통 응답 {"success": true, "data": ...} 의 data. 모양이 다르면 None."""
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or body.get("success") is not True:
        return None
    return body.get("data")


class HttpBookFinder:
    """BE 공개 API 를 부른다. 검색 한 번과, 결과마다 상품 상세 한 번(동시에)."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._transport = transport  # 시험에서 가짜 응답을 넣는다

    async def find(self, query: str, size: int) -> list[FoundBook]:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            transport=self._transport,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            try:
                response = await client.get(
                    _SEARCH_PATH, params={"query": query, "size": size}
                )
            except httpx.HTTPError as e:
                raise FinderUnavailable(type(e).__name__) from None
            data = response_data(response)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise FinderUnavailable(f"search status {response.status_code}")

            hits: list[tuple[str, int, str, str | None]] = []
            for item in data["items"]:
                if not isinstance(item, dict):
                    continue
                isbn, product_id, title = (
                    item.get("isbn"),
                    item.get("productId"),
                    item.get("title"),
                )
                if (
                    isinstance(isbn, str)
                    and _ISBN13.match(isbn) is not None
                    and isinstance(product_id, int)
                    and not isinstance(product_id, bool)
                    and isinstance(title, str)
                    and title
                ):
                    author = item.get("author")
                    hits.append(
                        (
                            isbn,
                            product_id,
                            title,
                            author if isinstance(author, str) else None,
                        )
                    )
            hits = list({hit[0]: hit for hit in hits}.values())[:size]
            details = await asyncio.gather(
                *(self._product(client, product_id) for _, product_id, _, _ in hits)
            )

        found = []
        for (isbn, product_id, title, author), detail in zip(
            hits, details, strict=True
        ):
            if detail is None:
                continue  # 판매 중지 등으로 상세를 못 읽은 책은 담을 수 없다
            price, stock, coupon_ids = detail
            found.append(
                FoundBook(isbn, product_id, title, author, price, stock > 0, coupon_ids)
            )
        return found

    async def _product(
        self, client: httpx.AsyncClient, product_id: int
    ) -> tuple[int, int, frozenset[int]] | None:
        """(판매가, 재고 수량, 쿠폰 번호). 읽지 못하면 None."""
        try:
            response = await client.get(_PRODUCT_PATH.format(product_id=product_id))
        except httpx.HTTPError:
            return None
        data = response_data(response)
        if not isinstance(data, dict):
            return None
        price = whole_won(data.get("discountedPrice"))
        stock = data.get("stockQuantity")
        coupons = data.get("couponIds")
        if price is None or not isinstance(stock, int) or isinstance(stock, bool):
            return None
        coupon_ids = frozenset(
            c
            for c in (coupons if isinstance(coupons, list) else [])
            if isinstance(c, int) and not isinstance(c, bool)
        )
        return price, stock, coupon_ids


_WORD = re.compile(r"[0-9A-Za-z가-힣]{2,}")


class FakeBookFinder:
    """로컬 시험과 평가에 쓰는 가공의 서가. 검색어 낱말이 제목에 많이 들어 있는 책부터 준다."""

    def __init__(self, books: Iterable[FoundBook]) -> None:
        self._books = list(books)
        self.queries: list[str] = []

    async def find(self, query: str, size: int) -> list[FoundBook]:
        self.queries.append(query)
        words = [w.lower() for w in _WORD.findall(query)]
        scored = []
        for order, book in enumerate(self._books):
            title = re.sub(r"\s+", "", book.title).lower()
            score = sum(1 for word in words if word in title)
            if score:
                scored.append((-score, order, book))
        return [book for _, _, book in sorted(scored)[:size]]


def by_isbn(books: Sequence[FoundBook]) -> dict[str, FoundBook]:
    return {book.isbn: book for book in books}
