"""BE → AI 반영(#640 6단계, #710) — BE MySQL 의 책·상품을 읽어 AI v_books·v_products 로 옮긴다.

- BE 시각(DATETIME)은 한국 시간이다. 반영 SQL(handoff/sync_catalog_to_commerce.sql)이 세션 시간대를 +09:00 으로
  두고 찍고, 앱도 한국 시간으로 쓴다(11/11 개발 BE 의 주문 created_at 이 UTC 지금보다 뒤). DB 서버 시간대는 UTC 라
  CLOUD #3 에는 UTC 로 적혔다. 옛 복제도 한국 시간으로 읽어 UTC 로 바꿨다(BE 13:34 → AI 04:34+00). AI 에는 UTC 로 둔다.
- 재고는 BE 처럼 product_inventory 의 보유 − 예약(줄이 없으면 20)이다. products.stock_quantity 는 BE 가 V58 뒤로
  더 바꾸지 않는다.
- 지운 책·상품은 deleted_at 으로 안다(BE V8 부터 상태 칸 대신 쓴다). AI 표에는 줄을 남기고 deleted_at 만 채운다
  (옛 복제와 같다).
- AI 표에는 키로 덮어쓰고, 더 오래된 BE 줄은 새 값을 덮어쓰지 않는다. 값이 같은 줄은 다시 쓰지 않는다 — 겹쳐 다시
  읽은 줄(10/22 적재는 125만 권이 한 시각)을 새로 쓰면 표가 부풀고 캐시를 밀어낸다. isbn13 은 겹치면 안 되므로 다른 책이 이미 가진
  ISBN 은 비운다.
"""

import re
import ssl
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import asyncpg
import pymysql
import pymysql.cursors

# BE ProductInventory.INITIAL_ON_HAND_QUANTITY. 재고 줄이 없는 상품을 BE 는 이만큼 있다고 본다
DEFAULT_ON_HAND = 20

_ISBN13 = re.compile(r"[0-9]{13}")
# BE 가 시각을 찍는 시간대(위 설명)
BE_TZ = ZoneInfo("Asia/Seoul")


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


def _from_be(value: datetime | None) -> datetime | None:
    """BE DATETIME(시간대 없음, 한국 시간)을 UTC aware 로."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=BE_TZ)
    return value.astimezone(UTC)


def _isbn13(value: str | None) -> str | None:
    text = (value or "").strip()
    return text if _ISBN13.fullmatch(text) else None


def _available(row: dict[str, Any]) -> int:
    """BE Product.stockQuantity 와 같다: 재고 줄이 없으면 20, 있으면 보유 − 예약."""
    on_hand = row.get("on_hand_quantity")
    if on_hand is None:
        return DEFAULT_ON_HAND
    return max(0, on_hand - (row.get("reserved_quantity") or 0))


def to_book(row: dict[str, Any]) -> Book:
    """BE books 한 줄을 v_books 한 줄로. v_books 의 price·in_stock 은 옛 칸이라 싣지 않는다 — AI 는 가격·재고를
    v_products 에서 읽고(app/core/products.py), 서버 표의 두 칸은 모두 0·false 다(11/11 개발 AI DB 210만 권)."""
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
        updated_at=_from_be(row["updated_at"]),
        deleted_at=_from_be(row.get("deleted_at")),
    )


def to_product(row: dict[str, Any]) -> Product:
    return Product(
        id=row["id"],
        book_id=row["book_id"],
        discounted_price=row["discounted_price"],
        stock_quantity=_available(row),
        updated_at=_from_be(row["updated_at"]),
        deleted_at=_from_be(row.get("deleted_at")),
    )


def connect_args(url: str) -> dict[str, Any]:
    """mysql://<계정>:<비밀번호>@<호스트>:<포트>/<DB>[?ssl-mode=REQUIRED] 를 pymysql 접속 인자로."""
    parsed = urlparse(url)
    query = {k.lower(): v[-1] for k, v in parse_qs(parsed.query).items()}
    context = None
    if query.get("ssl-mode", "").upper() == "REQUIRED":
        # mysql 클라이언트의 --ssl-mode=REQUIRED 와 같다: 암호화하되 인증서는 따지지 않는다
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": parsed.path.lstrip("/"),
        "ssl": context,
        "charset": "utf8mb4",
        "connect_timeout": 15,
        "read_timeout": 600,
    }


_BE_BOOKS = """
SELECT b.id, b.isbn13, b.title, b.author, b.publisher, b.category, b.published_at,
       b.cover_image_url, b.description, b.updated_at, b.deleted_at
FROM books b
"""
_BE_PRODUCTS = """
SELECT p.id, p.book_id, p.discounted_price, p.updated_at, p.deleted_at,
       i.on_hand_quantity, i.reserved_quantity
FROM products p
LEFT JOIN product_inventory i ON i.product_id = p.id
"""


class BeSource:
    """BE MySQL 을 읽기 전용으로 읽는다."""

    def __init__(self, url: str) -> None:
        self._conn = pymysql.connect(**connect_args(url), autocommit=True)
        with self._conn.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")

    def _stream(
        self, sql: str, alias: str, since: datetime | None, size: int
    ) -> Iterator[list[dict]]:
        where = "" if since is None else f" WHERE {alias}.updated_at >= %s"
        query = f"{sql}{where} ORDER BY {alias}.updated_at, {alias}.id"
        # BE 칸은 한국 시간이라 시작 시각도 한국 시간으로 바꿔 넘긴다
        params = (
            () if since is None else (since.astimezone(BE_TZ).replace(tzinfo=None),)
        )
        with self._conn.cursor(pymysql.cursors.SSDictCursor) as cursor:
            cursor.execute(query, params)
            while rows := cursor.fetchmany(size):
                yield list(rows)

    def books_since(self, since: datetime | None, size: int) -> Iterator[list[dict]]:
        return self._stream(_BE_BOOKS, "b", since, size)

    def products_since(self, since: datetime | None, size: int) -> Iterator[list[dict]]:
        return self._stream(_BE_PRODUCTS, "p", since, size)

    def books_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        marks = ", ".join(["%s"] * len(ids))
        with self._conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(_BE_BOOKS + f" WHERE b.id IN ({marks})", ids)
            return list(cursor.fetchall())

    def close(self) -> None:
        self._conn.close()


_UPSERT_BOOKS = """
INSERT INTO v_books (book_id, isbn13, title, author, publisher, category, pub_year, cover_url,
                     description, price, in_stock, updated_at, deleted_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
ON CONFLICT (book_id) DO UPDATE SET
    isbn13 = EXCLUDED.isbn13, title = EXCLUDED.title, author = EXCLUDED.author,
    publisher = EXCLUDED.publisher, category = EXCLUDED.category, pub_year = EXCLUDED.pub_year,
    cover_url = EXCLUDED.cover_url, description = EXCLUDED.description, price = EXCLUDED.price,
    in_stock = EXCLUDED.in_stock, updated_at = EXCLUDED.updated_at, deleted_at = EXCLUDED.deleted_at
WHERE v_books.updated_at <= EXCLUDED.updated_at
  AND (v_books.isbn13, v_books.title, v_books.author, v_books.publisher, v_books.category,
       v_books.pub_year, v_books.cover_url, v_books.description, v_books.price, v_books.in_stock,
       v_books.updated_at, v_books.deleted_at)
      IS DISTINCT FROM
      (EXCLUDED.isbn13, EXCLUDED.title, EXCLUDED.author, EXCLUDED.publisher, EXCLUDED.category,
       EXCLUDED.pub_year, EXCLUDED.cover_url, EXCLUDED.description, EXCLUDED.price, EXCLUDED.in_stock,
       EXCLUDED.updated_at, EXCLUDED.deleted_at)
"""
_UPSERT_PRODUCTS = """
INSERT INTO v_products (id, book_id, discounted_price, stock_quantity, updated_at, deleted_at)
VALUES ($1, $2, $3, $4, $5, $6)
ON CONFLICT (id) DO UPDATE SET
    book_id = EXCLUDED.book_id, discounted_price = EXCLUDED.discounted_price,
    stock_quantity = EXCLUDED.stock_quantity, updated_at = EXCLUDED.updated_at,
    deleted_at = EXCLUDED.deleted_at
WHERE v_products.updated_at <= EXCLUDED.updated_at
  AND (v_products.book_id, v_products.discounted_price, v_products.stock_quantity,
       v_products.updated_at, v_products.deleted_at)
      IS DISTINCT FROM
      (EXCLUDED.book_id, EXCLUDED.discounted_price, EXCLUDED.stock_quantity,
       EXCLUDED.updated_at, EXCLUDED.deleted_at)
"""


async def upsert_books(conn: asyncpg.Connection, books: list[Book]) -> int:
    """책을 키로 덮어쓴다. 다른 책이 이미 가진 ISBN 은 비우고 그 수를 돌려준다(isbn13 은 겹치면 안 된다)."""
    isbns = [b.isbn13 for b in books if b.isbn13]
    owners = {
        r["isbn13"]: r["book_id"]
        for r in await conn.fetch(
            "SELECT isbn13, book_id FROM v_books WHERE isbn13 = ANY($1::text[])", isbns
        )
    }
    dropped = 0
    rows = []
    for book in books:
        isbn = book.isbn13
        if isbn is not None:
            owner = owners.get(isbn)
            if owner is not None and owner != book.book_id:
                isbn = None
                dropped += 1
            else:
                owners[isbn] = book.book_id
        rows.append(
            (
                book.book_id,
                isbn,
                book.title,
                book.author,
                book.publisher,
                book.category,
                book.pub_year,
                book.cover_url,
                book.description,
                book.price,
                book.in_stock,
                book.updated_at,
                book.deleted_at,
            )
        )
    await conn.executemany(_UPSERT_BOOKS, rows)
    return dropped


async def upsert_products(conn: asyncpg.Connection, products: list[Product]) -> None:
    await conn.executemany(
        _UPSERT_PRODUCTS,
        [
            (
                p.id,
                p.book_id,
                p.discounted_price,
                p.stock_quantity,
                p.updated_at,
                p.deleted_at,
            )
            for p in products
        ],
    )
