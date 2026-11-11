"""BE → AI 반영(#640 6단계, #710) — BE MySQL 에서 바뀐 책·상품을 AI v_books·v_products 로 옮긴다.

실행: uv run python -m app.jobs.sync_from_be

5분 복제가 10/27 RDS 이전 뒤 한 번도 돌지 않아 AI 쪽 책·가격이 10/22 에 멈췄다. 복제를 되살리지 않고 이 배치가
대신한다. 실행은 클라우드가 한다(AI DB 쓰기). BE_MYSQL_URL 이 비어 있으면 아무것도 하지 않고 끝난다.

- 바뀐 줄: BE updated_at 이 지난 실행의 마지막 시각(replication_state 의 be_books·be_products) 뒤인 줄. 기록이
  없으면 AI 표의 마지막 updated_at 에서 시작한다(첫 실행은 저절로 10/22 이후 전부). 옛 복제가 쓰던 books·products
  줄은 KST 시각에 +00 이 붙어 9시간 어긋나 있어 쓰지 않는다. 조금(OVERLAP) 겹쳐 다시 읽고 키로 덮어쓰므로 겹쳐도
  결과가 같고, 더 오래된 BE 줄은 새 값을 덮어쓰지 않는다.
- BE 시각(DATETIME)은 한국 시간이다. 반영 SQL(handoff/sync_catalog_to_commerce.sql)이 세션 시간대를 +09:00 으로
  두고 찍고, 앱도 한국 시간으로 쓴다(11/11 개발 BE 의 주문 created_at 이 UTC 지금보다 뒤). DB 서버 시간대는 UTC 라
  CLOUD #3 에는 UTC 로 적혔다. 옛 복제도 한국 시간으로 읽어 UTC 로 바꿨다(BE 13:34 → AI 04:34+00). AI 에는 UTC 로 둔다.
- 지운 책·상품은 줄을 남기고 deleted_at 만 채운다(옛 복제와 같다).
- 재고는 BE 처럼 product_inventory 의 보유 − 예약(줄이 없으면 20)이다. products.stock_quantity 는 BE 가 V58 뒤로
  더 바꾸지 않는다. 시각이 바뀌지 않은 상품의 재고 변화는 #711 이 맞춘다.
- 값이 같은 줄은 다시 쓰지 않는다 — 겹쳐 다시 읽은 줄(10/22 적재는 125만 권이 한 시각)을 새로 쓰면 표가 부풀고
  캐시를 밀어낸다. isbn13 은 겹치면 안 되므로 다른 책이 이미 가진 ISBN 은 비운다.
- 끝 검증: 이번에 옮긴 책 몇십 권을 BE 와 다시 맞춰 보고, 다르면 진행 기록을 남기지 않고 오류로 끝난다. 표 전체를
  세지 않는다 — 작은 DB 에서 매일 210만 줄을 읽으면 검색이 쓰는 캐시를 밀어낸다.
"""

import asyncio
import logging
import re
import ssl
import sys
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
from zoneinfo import ZoneInfo

import asyncpg
import pymysql
import pymysql.cursors

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 지난 실행의 마지막 시각에서 이만큼 앞부터 다시 읽는다. 같은 시각에 커밋된 줄을 놓치지 않으려는 여유다
OVERLAP = timedelta(minutes=10)
# BE 에서 한 번에 받아 AI 에 쓰는 줄 수
FETCH_SIZE = 2000
# 끝 검증에서 BE 와 다시 맞춰 보는 책 수(이번에 옮긴 것 중 마지막 것들)
SAMPLE_SIZE = 50
# BE ProductInventory.INITIAL_ON_HAND_QUANTITY. 재고 줄이 없는 상품을 BE 는 이만큼 있다고 본다
DEFAULT_ON_HAND = 20

BOOKS_CURSOR = "be_books"
PRODUCTS_CURSOR = "be_products"

_ISBN13 = re.compile(r"[0-9]{13}")
# BE 가 시각을 찍는 시간대(위 설명)
BE_TZ = ZoneInfo("Asia/Seoul")


class CheckFailed(RuntimeError):
    """옮긴 뒤 BE 와 다시 맞춰 보니 값이 다르다. 진행 기록을 남기지 않는다."""


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


@dataclass(frozen=True)
class Summary:
    books: int
    products: int
    isbn_dropped: int


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


def since_of(last: datetime | None) -> datetime | None:
    """다시 읽을 시작 시각. 마지막 시각이 없으면(처음) None = 전부."""
    return None if last is None else last - OVERLAP


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

# BE 에서 품절인 상품(보유 − 예약 ≤ 0). 재고 줄이 없는 상품은 BE 가 20 으로 보므로 품절이 아니다
_BE_SOLD_OUT = """
SELECT i.product_id AS id, i.on_hand_quantity, i.reserved_quantity
FROM product_inventory i
WHERE i.on_hand_quantity - i.reserved_quantity <= 0
"""
# 한 번에 번호로 읽는 상품 수
_IDS_CHUNK = 1000


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

    def sold_out(self) -> dict[int, int]:
        """BE 에서 품절인 상품 번호 → 남은 재고(0)."""
        with self._conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(_BE_SOLD_OUT)
            return {r["id"]: _available(r) for r in cursor.fetchall()}

    def available_by_ids(self, ids: list[int]) -> dict[int, int]:
        """상품 번호 → 남은 재고. 재고 줄이 없으면 20(BE 와 같다)."""
        found: dict[int, int] = {}
        with self._conn.cursor(pymysql.cursors.DictCursor) as cursor:
            for start in range(0, len(ids), _IDS_CHUNK):
                chunk = ids[start : start + _IDS_CHUNK]
                marks = ", ".join(["%s"] * len(chunk))
                cursor.execute(_BE_PRODUCTS + f" WHERE p.id IN ({marks})", chunk)
                found.update({r["id"]: _available(r) for r in cursor.fetchall()})
        return found

    def close(self) -> None:
        self._conn.close()


# price·in_stock 은 옛 칸이라 새 줄에만 0·false 로 넣고(서버 표와 같다) 고치지 않는다(to_book 설명)
_UPSERT_BOOKS = """
INSERT INTO v_books (book_id, isbn13, title, author, publisher, category, pub_year, cover_url,
                     description, price, in_stock, updated_at, deleted_at)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, 0, false, $10, $11)
ON CONFLICT (book_id) DO UPDATE SET
    isbn13 = EXCLUDED.isbn13, title = EXCLUDED.title, author = EXCLUDED.author,
    publisher = EXCLUDED.publisher, category = EXCLUDED.category, pub_year = EXCLUDED.pub_year,
    cover_url = EXCLUDED.cover_url, description = EXCLUDED.description,
    updated_at = EXCLUDED.updated_at, deleted_at = EXCLUDED.deleted_at
WHERE v_books.updated_at <= EXCLUDED.updated_at
  AND (v_books.isbn13, v_books.title, v_books.author, v_books.publisher, v_books.category,
       v_books.pub_year, v_books.cover_url, v_books.description, v_books.updated_at,
       v_books.deleted_at)
      IS DISTINCT FROM
      (EXCLUDED.isbn13, EXCLUDED.title, EXCLUDED.author, EXCLUDED.publisher, EXCLUDED.category,
       EXCLUDED.pub_year, EXCLUDED.cover_url, EXCLUDED.description, EXCLUDED.updated_at,
       EXCLUDED.deleted_at)
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


async def _last(conn: asyncpg.Connection, cursor: str, table: str) -> datetime | None:
    saved = await conn.fetchval(
        "SELECT cursor_value FROM replication_state WHERE name = $1", cursor
    )
    if saved is not None:
        return saved
    # 처음: AI 표의 마지막 시각. 한 번만 표를 훑는다
    return await conn.fetchval(f"SELECT max(updated_at) FROM {table}")


async def _save(conn: asyncpg.Connection, cursor: str, value: datetime) -> None:
    await conn.execute(
        "INSERT INTO replication_state (name, cursor_value) VALUES ($1, $2)"
        " ON CONFLICT (name) DO UPDATE SET cursor_value = EXCLUDED.cursor_value",
        cursor,
        value,
    )


async def check(conn: asyncpg.Connection, source: Any, sample: list[int]) -> None:
    """옮긴 책을 BE 와 다시 맞춰 본다. 확인하는 사이 BE 에서 다시 바뀐 책은 다음 실행이 옮기므로 넘어간다."""
    if not sample:
        return
    be = {r["id"]: to_book(r) for r in source.books_by_ids(sample)}
    ai = {
        r["book_id"]: r
        for r in await conn.fetch(
            "SELECT book_id, isbn13, title, author, publisher, updated_at, deleted_at"
            " FROM v_books WHERE book_id = ANY($1::int[])",
            sample,
        )
    }
    wrong = []
    for book_id in sample:
        want = be.get(book_id)
        got = ai.get(book_id)
        if want is None:
            continue
        if got is None:
            wrong.append(book_id)
            continue
        if want.updated_at > got["updated_at"]:
            continue
        same = (
            (got["title"], got["author"], got["publisher"])
            == (want.title, want.author, want.publisher)
            and got["isbn13"] in (want.isbn13, None)
            and (got["deleted_at"] is None) == (want.deleted_at is None)
        )
        if not same:
            wrong.append(book_id)
    if wrong:
        raise CheckFailed(
            f"옮긴 책 {len(sample)}권 중 {len(wrong)}권이 BE 와 다르다(예: {wrong[:5]})"
        )


async def run(conn: asyncpg.Connection, source: Any) -> Summary:
    """바뀐 책·상품을 옮기고, 맞춰 본 뒤 진행 기록을 남긴다."""
    books_since = since_of(await _last(conn, BOOKS_CURSOR, "v_books"))
    products_since = since_of(await _last(conn, PRODUCTS_CURSOR, "v_products"))

    n_books = dropped = 0
    last_book: datetime | None = None
    sample: deque[int] = deque(maxlen=SAMPLE_SIZE)
    for batch in source.books_since(books_since, FETCH_SIZE):
        books = [to_book(r) for r in batch]
        dropped += await upsert_books(conn, books)
        n_books += len(books)
        last_book = max([last_book, *(b.updated_at for b in books)], key=_order)
        sample.extend(b.book_id for b in books)
        logger.info("책 %d권 옮김(마지막 %s)", n_books, last_book)

    n_products = 0
    last_product: datetime | None = None
    for batch in source.products_since(products_since, FETCH_SIZE):
        products = [to_product(r) for r in batch]
        await upsert_products(conn, products)
        n_products += len(products)
        last_product = max(
            [last_product, *(p.updated_at for p in products)], key=_order
        )

    await check(conn, source, list(sample))
    if last_book is not None:
        await _save(conn, BOOKS_CURSOR, last_book)
    if last_product is not None:
        await _save(conn, PRODUCTS_CURSOR, last_product)
    return Summary(books=n_books, products=n_products, isbn_dropped=dropped)


def _order(value: datetime | None) -> datetime:
    return value or datetime.min.replace(tzinfo=UTC)


async def main() -> int:
    """끝난 상태 번호: 0 끝(할 일 없음 포함), 1 끝 검증 실패."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    settings = get_settings()
    if not settings.be_mysql_url:
        logger.info("BE_MYSQL_URL 이 비어 있다 — 아무것도 하지 않는다")
        return 0
    source = BeSource(settings.be_mysql_url)
    conn = await asyncpg.connect(settings.database_url)
    try:
        summary = await run(conn, source)
    except CheckFailed:
        logger.exception("끝 검증 실패 — 진행 기록을 남기지 않았다")
        return 1
    finally:
        await conn.close()
        source.close()
    logger.info(
        "끝. 책 %d권 · 상품 %d개 · 다른 책이 가진 ISBN 이라 비운 것 %d권",
        summary.books,
        summary.products,
        summary.isbn_dropped,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
