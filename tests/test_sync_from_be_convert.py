"""BE → AI 반영 배치의 BE 읽기·변환(app/jobs/sync_from_be.py, #710) 시험. DB 없이 돈다."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.jobs import sync_from_be as sync


def _be_time(*parts: int) -> datetime:
    """BE MySQL DATETIME 은 시간대 없이 한국 시간으로 온다."""
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


def test_BE_시각은_한국_시간으로_읽어_UTC로_바꾸고_출간일은_연도만_싣는다() -> None:
    book = sync.to_book(_be_book(deleted_at=_be_time(2026, 11, 2, 9, 0, 0)))

    # BE 11/1 03:00(한국) = 10/31 18:00 UTC
    assert book.updated_at == datetime(2026, 10, 31, 18, 0, 0, tzinfo=UTC)
    assert book.deleted_at == datetime(2026, 11, 2, 0, 0, 0, tzinfo=UTC)
    assert book.updated_at.tzinfo is UTC
    assert book.pub_year == 2025


def test_책_줄에는_가격·재고를_싣지_않는다() -> None:
    # v_books.price·in_stock 은 옛 칸이다. AI 는 가격·재고를 v_products 에서 읽는다
    book = sync.to_book(_be_book())

    assert not hasattr(book, "price") and not hasattr(book, "in_stock")


def test_재고_줄이_없는_상품은_20권으로_본다() -> None:
    # BE ProductInventory.INITIAL_ON_HAND_QUANTITY 와 같다(Product.stockQuantity)
    product = sync.to_product(
        _be_product(on_hand_quantity=None, reserved_quantity=None)
    )

    assert product.stock_quantity == 20


def test_예약이_보유만큼이면_남은_재고는_0이다() -> None:
    assert (
        sync.to_product(
            _be_product(on_hand_quantity=2, reserved_quantity=2)
        ).stock_quantity
        == 0
    )


def test_상품은_할인가와_남은_재고를_싣는다() -> None:
    product = sync.to_product(_be_product())

    assert product.discounted_price == Decimal("13500.00")
    assert product.stock_quantity == 5
    assert product.updated_at == datetime(2026, 10, 31, 18, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "mysql://reader:p%40ss@be-db:3307/bookjeok",
            {
                "host": "be-db",
                "port": 3307,
                "user": "reader",
                "password": "p@ss",
                "database": "bookjeok",
            },
        ),
        (
            "mysql://reader@be-db/bookjeok",
            {
                "host": "be-db",
                "port": 3306,
                "user": "reader",
                "password": "",
                "database": "bookjeok",
            },
        ),
    ],
)
def test_BE_주소를_접속_인자로_바꾼다(url: str, expected: dict) -> None:
    args = sync.connect_args(url)

    assert {key: args[key] for key in expected} == expected
    assert args["ssl"] is None


def test_ssl_mode_REQUIRED면_암호화해서_붙는다() -> None:
    # mysql 클라이언트의 --ssl-mode=REQUIRED 와 같다: 암호화하되 인증서는 따지지 않는다(RDS)
    args = sync.connect_args("mysql://reader:x@be-db:3306/bookjeok?ssl-mode=REQUIRED")

    assert args["ssl"] is not None
