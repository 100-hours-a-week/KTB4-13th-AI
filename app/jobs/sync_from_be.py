"""BE → AI 반영(#640 6단계, #710) — BE MySQL 의 책·상품을 읽어 AI v_books·v_products 줄 모양으로 바꾼다.

- BE 시각(DATETIME)은 UTC 다(BE 코드에 시간대 설정이 없고, CLOUD #3 에서 UTC 로 확인).
- 재고는 BE 처럼 product_inventory 의 보유 − 예약(줄이 없으면 20)이다. products.stock_quantity 는 BE 가 V58 뒤로
  더 바꾸지 않는다.
- 지운 책·상품은 deleted_at 으로 안다(BE V8 부터 상태 칸 대신 쓴다).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# BE ProductInventory.INITIAL_ON_HAND_QUANTITY. 재고 줄이 없는 상품을 BE 는 이만큼 있다고 본다
DEFAULT_ON_HAND = 20

_ISBN13 = re.compile(r"[0-9]{13}")


@dataclass(frozen=True)
class Book:
    book_id: int
    isbn13: str | None
    title: str
    author: str | None
    publisher: str | None
    category: str | None
    pub_year: int | None
    cover_url: str | None
    description: str | None
    price: int
    in_stock: bool
    updated_at: datetime
    deleted_at: datetime | None


@dataclass(frozen=True)
class Product:
    id: int
    book_id: int
    discounted_price: Decimal
    stock_quantity: int
    updated_at: datetime
    deleted_at: datetime | None


def _utc(value: datetime | None) -> datetime | None:
    """BE DATETIME(시간대 없음, UTC)을 aware 로."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _isbn13(value: str | None) -> str | None:
    text = (value or "").strip()
    return text if _ISBN13.fullmatch(text) else None


def _available(row: dict[str, Any]) -> int:
    """BE Product.stockQuantity 와 같다: 재고 줄이 없으면 20, 있으면 보유 − 예약."""
    on_hand = row.get("on_hand_quantity")
    if on_hand is None:
        return DEFAULT_ON_HAND
    return max(0, on_hand - (row.get("reserved_quantity") or 0))


def _won(value: Decimal) -> int:
    """할인가를 원 단위 정수로. PostgreSQL numeric::int 와 같게 반올림한다(app/core/products.py)."""
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_book(row: dict[str, Any]) -> Book:
    """BE books(+ 상품·재고) 한 줄을 v_books 한 줄로. 가격·재고는 v_books 의 옛 칸이라 채우기만 한다 — AI 는
    가격·재고를 v_products 에서 읽는다."""
    has_product = (
        row.get("product_id") is not None
        and row.get("discounted_price") is not None
        and row.get("product_deleted_at") is None
    )
    published: date | None = row.get("published_at")
    return Book(
        book_id=row["id"],
        isbn13=_isbn13(row.get("isbn13")),
        title=row["title"],
        author=row.get("author"),
        publisher=row.get("publisher"),
        category=row.get("category"),
        pub_year=published.year if published else None,
        cover_url=row.get("cover_image_url"),
        description=row.get("description"),
        price=_won(row["discounted_price"]) if has_product else 0,
        in_stock=has_product and _available(row) > 0,
        updated_at=_utc(row["updated_at"]),
        deleted_at=_utc(row.get("deleted_at")),
    )


def to_product(row: dict[str, Any]) -> Product:
    return Product(
        id=row["id"],
        book_id=row["book_id"],
        discounted_price=row["discounted_price"],
        stock_quantity=_available(row),
        updated_at=_utc(row["updated_at"]),
        deleted_at=_utc(row.get("deleted_at")),
    )
