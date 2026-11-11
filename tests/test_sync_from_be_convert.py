"""BE → AI 반영 배치의 BE 읽기·변환(app/jobs/sync_from_be.py, #710) 시험. DB 없이 돈다."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.jobs import sync_from_be as sync


def _be_time(*parts: int) -> datetime:
    """BE MySQL DATETIME 은 시간대 없이 온다(값은 UTC)."""
    return datetime(*parts, tzinfo=UTC).replace(tzinfo=None)


def _be_book(**overrides) -> dict:
    row = {
        "id": 990000001,
        "isbn13": "9788900000011",
        "title": "시험 책",
        "author": "시험 저자",
        "publisher": "시험 출판사",
        "category": "소설",
        "published_at": date(2025, 3, 1),
        "cover_image_url": "https://example.com/c.jpg",
        "description": "소개",
        "updated_at": _be_time(2026, 11, 1, 3, 0, 0),
        "deleted_at": None,
        "product_id": 880000001,
        "discounted_price": Decimal("13500.00"),
        "product_deleted_at": None,
        "on_hand_quantity": 7,
        "reserved_quantity": 2,
    }
    row.update(overrides)
    return row


def _be_product(**overrides) -> dict:
    row = {
        "id": 880000001,
        "book_id": 990000001,
        "discounted_price": Decimal("13500.00"),
        "updated_at": _be_time(2026, 11, 1, 3, 0, 0),
        "deleted_at": None,
        "on_hand_quantity": 7,
        "reserved_quantity": 2,
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ("9788900000011", "9788900000011"),
        (" 9788900000011 ", "9788900000011"),
        ("978-89-000-0001-1", None),
        ("8900000011", None),
        ("", None),
        (None, None),
    ],
)
def test_ISBN은_13자리_숫자만_싣는다(raw: str | None, kept: str | None) -> None:
    # AI 표는 isbn13 이 13자리 숫자여야 한다(CHECK). 아니면 그 줄 하나 때문에 묶음 전체가 실패한다
    assert sync.to_book(_be_book(isbn13=raw)).isbn13 == kept


def test_BE_시각은_UTC로_읽고_출간일은_연도만_싣는다() -> None:
    book = sync.to_book(_be_book(deleted_at=_be_time(2026, 11, 2, 0, 0, 0)))

    assert book.updated_at == datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)
    assert book.deleted_at == datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)
    assert book.pub_year == 2025


def test_책의_가격과_재고는_상품에서_읽고_재고는_보유에서_예약을_뺀다() -> None:
    book = sync.to_book(_be_book())

    assert book.price == 13500
    assert book.in_stock is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"product_id": None, "discounted_price": None},
        {"product_deleted_at": _be_time(2026, 11, 1, 0, 0, 0)},
        {"on_hand_quantity": 2, "reserved_quantity": 2},
    ],
)
def test_상품이_없거나_지웠거나_남은_재고가_없으면_품절이다(overrides: dict) -> None:
    book = sync.to_book(_be_book(**overrides))

    assert book.in_stock is False


def test_상품이_없는_책의_가격은_0이다() -> None:
    # v_books.price 는 비워 둘 수 없다. AI 는 가격을 v_products 에서 읽어 이 값을 쓰지 않는다
    assert sync.to_book(_be_book(product_id=None, discounted_price=None)).price == 0


def test_재고_줄이_없는_상품은_20권으로_본다() -> None:
    # BE ProductInventory.INITIAL_ON_HAND_QUANTITY 와 같다(Product.stockQuantity)
    product = sync.to_product(
        _be_product(on_hand_quantity=None, reserved_quantity=None)
    )

    assert product.stock_quantity == 20


def test_상품은_할인가와_남은_재고를_싣는다() -> None:
    product = sync.to_product(_be_product())

    assert product.discounted_price == Decimal("13500.00")
    assert product.stock_quantity == 5
    assert product.updated_at == datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)
