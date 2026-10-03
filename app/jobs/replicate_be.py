"""BE MySQL의 검색·추천 원본을 AI PostgreSQL로 증분 복제한다.

systemd timer가 5분마다 이 모듈을 한 번 실행한다. 원본의 DATETIME은 KST로 해석하고
PostgreSQL timestamptz에는 UTC 시각으로 넣는다. 소프트 삭제와 주문 취소 상태도 행에
그대로 남겨 AI 조회 계층이 제외할 수 있게 한다.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import pymysql

BATCH_SIZE = int(os.getenv("REPLICATION_BATCH_SIZE", "1000"))
LOCK_ID = 824913
SOURCE_TIMEZONE = ZoneInfo(os.getenv("REPLICATION_SOURCE_TIMEZONE", "Asia/Seoul"))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC).replace(tzinfo=None)


@dataclass(frozen=True, order=True)
class Cursor:
    changed_at: datetime
    row_id: int = 0


def source_naive(value: datetime | None) -> datetime:
    """Postgres 커서를 MySQL DATETIME과 비교할 KST naive 값으로 바꾼다."""
    if value is None:
        return EPOCH
    if value.tzinfo is None:
        return value
    return value.astimezone(SOURCE_TIMEZONE).replace(tzinfo=None)


def target_utc(value: datetime | None) -> datetime | None:
    """KST 기준 MySQL DATETIME을 명시적인 UTC timestamptz 값으로 바꾼다."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=SOURCE_TIMEZONE)
    return value.astimezone(UTC)


def get_cursor(pc: psycopg.Cursor[Any], name: str) -> Cursor:
    pc.execute(
        "SELECT cursor_value, cursor_id FROM replication_state WHERE name = %s",
        (name,),
    )
    row = pc.fetchone()
    if row is None:
        return Cursor(EPOCH, 0)
    return Cursor(source_naive(row[0]), int(row[1]))


def save_cursor(pc: psycopg.Cursor[Any], name: str, cursor: Cursor) -> None:
    pc.execute(
        """
        INSERT INTO replication_state(name, cursor_value, cursor_id, updated_at)
        VALUES (%s, %s, %s, now())
        ON CONFLICT (name) DO UPDATE SET
            cursor_value = EXCLUDED.cursor_value,
            cursor_id = EXCLUDED.cursor_id,
            updated_at = EXCLUDED.updated_at
        """,
        (name, target_utc(cursor.changed_at), cursor.row_id),
    )


def _advance(current: Cursor, changed_at: datetime, row_id: int) -> Cursor:
    candidate = Cursor(source_naive(changed_at), int(row_id))
    return max(current, candidate)


def _stream_upserts(
    mysql: pymysql.Connection,
    pg: psycopg.Connection,
    *,
    name: str,
    source_query: str,
    target_query: str,
    transform: Callable[[Sequence[Any]], Sequence[Any]],
    changed_at_index: int,
    row_id_index: int,
) -> int:
    total = 0
    with pg.cursor() as pc:
        cursor = get_cursor(pc, name)

    with mysql.cursor() as mc:
        mc.execute(
            source_query,
            (cursor.changed_at, cursor.changed_at, cursor.row_id),
        )
        while rows := mc.fetchmany(BATCH_SIZE):
            with pg.cursor() as pc:
                pc.executemany(target_query, [transform(row) for row in rows])
                for row in rows:
                    cursor = _advance(
                        cursor, row[changed_at_index], row[row_id_index]
                    )
                save_cursor(pc, name, cursor)
            pg.commit()
            total += len(rows)
    print(f"{name} 증분 반영: {total}건", flush=True)
    return total


BOOKS_SOURCE = """
SELECT id, isbn13, title, author, publisher, category, published_at,
       description, cover_image_url, updated_at, deleted_at
FROM books
WHERE updated_at > %s OR (updated_at = %s AND id > %s)
ORDER BY updated_at, id
"""

BOOKS_TARGET = """
INSERT INTO v_books (
    book_id, isbn, title, author, publisher, category, published_at, pub_year,
    description, cover_url, price, in_stock, updated_at, deleted_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, EXTRACT(YEAR FROM %s)::integer,
        %s, %s, 0, false, %s, %s)
ON CONFLICT (book_id) DO UPDATE SET
    isbn = EXCLUDED.isbn,
    title = EXCLUDED.title,
    author = EXCLUDED.author,
    publisher = EXCLUDED.publisher,
    category = EXCLUDED.category,
    published_at = EXCLUDED.published_at,
    pub_year = EXCLUDED.pub_year,
    description = EXCLUDED.description,
    cover_url = EXCLUDED.cover_url,
    updated_at = EXCLUDED.updated_at,
    deleted_at = EXCLUDED.deleted_at
"""


def _book_row(row: Sequence[Any]) -> Sequence[Any]:
    return (
        *row[:7],
        row[6],
        row[7],
        row[8],
        target_utc(row[9]),
        target_utc(row[10]),
    )


PRODUCTS_SOURCE = """
SELECT id, book_id, sale_price, discounted_price, stock_quantity,
       updated_at, deleted_at
FROM products
WHERE updated_at > %s OR (updated_at = %s AND id > %s)
ORDER BY updated_at, id
"""

PRODUCTS_TARGET = """
INSERT INTO v_products (
    id, book_id, sale_price, discounted_price, stock_quantity, status,
    updated_at, deleted_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO UPDATE SET
    book_id = EXCLUDED.book_id,
    sale_price = EXCLUDED.sale_price,
    discounted_price = EXCLUDED.discounted_price,
    stock_quantity = EXCLUDED.stock_quantity,
    status = EXCLUDED.status,
    updated_at = EXCLUDED.updated_at,
    deleted_at = EXCLUDED.deleted_at
"""


def _product_row(row: Sequence[Any]) -> Sequence[Any]:
    deleted_at = row[6]
    return (
        *row[:5],
        "DELETED" if deleted_at is not None else "ACTIVE",
        target_utc(row[5]),
        target_utc(deleted_at),
    )


POPULARITY_SOURCE = """
SELECT p.book_id, s.sales_quantity, s.review_rate, s.review_count,
       s.refreshed_at, s.product_id
FROM product_popularity_snapshots s
JOIN products p ON p.id = s.product_id
WHERE s.refreshed_at > %s OR (s.refreshed_at = %s AND s.product_id > %s)
ORDER BY s.refreshed_at, s.product_id
"""

POPULARITY_TARGET = """
INSERT INTO v_book_popularity (book_id, sales, rating_avg, rating_count, as_of)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (book_id) DO UPDATE SET
    sales = EXCLUDED.sales,
    rating_avg = EXCLUDED.rating_avg,
    rating_count = EXCLUDED.rating_count,
    as_of = EXCLUDED.as_of
"""


def _popularity_row(row: Sequence[Any]) -> Sequence[Any]:
    return (*row[:4], target_utc(row[4]))


def _table_exists(mysql: pymysql.Connection, table: str) -> bool:
    with mysql.cursor() as mc:
        mc.execute(
            """
            SELECT EXISTS(
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = DATABASE() AND table_name = %s
            )
            """,
            (table,),
        )
        row = mc.fetchone()
    return bool(row and row[0])


def _purchases_source(has_cancel: bool) -> str:
    if has_cancel:
        cancel_join = """
        LEFT JOIN (
            SELECT order_item_id,
                   SUM(canceled_quantity) AS canceled_quantity,
                   MAX(canceled_at) AS latest_canceled_at
            FROM cancel
            GROUP BY order_item_id
        ) c ON c.order_item_id = oi.id
        """
        canceled_quantity = "LEAST(oi.quantity, COALESCE(c.canceled_quantity, 0))"
        canceled_at = "COALESCE(c.latest_canceled_at, o.canceled_at)"
        changed_at = (
            "GREATEST(o.updated_at, oi.updated_at, "
            "COALESCE(c.latest_canceled_at, '1970-01-01 00:00:00'))"
        )
    else:
        cancel_join = ""
        canceled_quantity = (
            "CASE WHEN oi.status = 'CANCELED' OR o.status = 'CANCELED' "
            "THEN oi.quantity ELSE 0 END"
        )
        canceled_at = "o.canceled_at"
        changed_at = "GREATEST(o.updated_at, oi.updated_at)"

    return f"""
    SELECT o.user_id, p.book_id, o.paid_at, oi.id, o.id, p.id, oi.quantity,
           {canceled_quantity}, oi.status, {canceled_at},
           COALESCE(oi.deleted_at, o.deleted_at), {changed_at} AS changed_at
    FROM orders o
    JOIN order_item oi ON oi.order_id = o.id
    JOIN products p ON p.id = oi.product_id
    {cancel_join}
    WHERE o.paid_at IS NOT NULL
      AND ({changed_at} > %s OR ({changed_at} = %s AND oi.id > %s))
    ORDER BY changed_at, oi.id
    """


PURCHASES_TARGET = """
INSERT INTO v_user_purchases (
    user_id, book_id, purchased_at, order_item_id, order_id, product_id,
    quantity, canceled_quantity, order_status, canceled_at, deleted_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (order_item_id) DO UPDATE SET
    user_id = EXCLUDED.user_id,
    book_id = EXCLUDED.book_id,
    purchased_at = EXCLUDED.purchased_at,
    order_id = EXCLUDED.order_id,
    product_id = EXCLUDED.product_id,
    quantity = EXCLUDED.quantity,
    canceled_quantity = EXCLUDED.canceled_quantity,
    order_status = EXCLUDED.order_status,
    canceled_at = EXCLUDED.canceled_at,
    deleted_at = EXCLUDED.deleted_at
"""


def _purchase_row(row: Sequence[Any]) -> Sequence[Any]:
    values = list(row[:11])
    for index in (2, 9, 10):
        values[index] = target_utc(values[index])
    return tuple(values)


REVIEWS_SOURCE = """
SELECT r.user_id, p.book_id, r.rating, r.created_at, r.id, r.order_item_id,
       (r.active_flag <> 0), r.deleted_at, r.updated_at
FROM reviews r
JOIN order_item oi ON oi.id = r.order_item_id
JOIN products p ON p.id = oi.product_id
WHERE r.updated_at > %s OR (r.updated_at = %s AND r.id > %s)
ORDER BY r.updated_at, r.id
"""

REVIEWS_TARGET = """
INSERT INTO v_user_reviews (
    user_id, book_id, rating, created_at, review_id, order_item_id,
    active_flag, deleted_at, updated_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (review_id) DO UPDATE SET
    user_id = EXCLUDED.user_id,
    book_id = EXCLUDED.book_id,
    rating = EXCLUDED.rating,
    created_at = EXCLUDED.created_at,
    order_item_id = EXCLUDED.order_item_id,
    active_flag = EXCLUDED.active_flag,
    deleted_at = EXCLUDED.deleted_at,
    updated_at = EXCLUDED.updated_at
"""


def _review_row(row: Sequence[Any]) -> Sequence[Any]:
    values = list(row)
    for index in (3, 7, 8):
        values[index] = target_utc(values[index])
    return tuple(values)


def _prepare_schema(pg: psycopg.Connection) -> None:
    with pg.cursor() as pc:
        pc.execute(
            """
            CREATE TABLE IF NOT EXISTS replication_state (
                name text PRIMARY KEY,
                cursor_value timestamptz NOT NULL,
                cursor_id bigint NOT NULL DEFAULT 0,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        pc.execute(
            "ALTER TABLE replication_state "
            "ADD COLUMN IF NOT EXISTS cursor_id bigint NOT NULL DEFAULT 0"
        )
        pc.execute(
            "ALTER TABLE replication_state "
            "ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now()"
        )
        pc.execute(
            """
            CREATE TABLE IF NOT EXISTS replication_status (
                singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                last_started_at timestamptz,
                last_succeeded_at timestamptz,
                last_error text
            )
            """
        )
    pg.commit()


def _mark_started(pg: psycopg.Connection) -> None:
    with pg.cursor() as pc:
        pc.execute(
            """
            INSERT INTO replication_status(singleton, last_started_at, last_error)
            VALUES (true, now(), NULL)
            ON CONFLICT (singleton) DO UPDATE SET
                last_started_at = EXCLUDED.last_started_at,
                last_error = NULL
            """
        )
    pg.commit()


def _mark_finished(pg: psycopg.Connection, error: str | None = None) -> None:
    with pg.cursor() as pc:
        if error is None:
            pc.execute(
                """
                UPDATE replication_status
                SET last_succeeded_at = now(), last_error = NULL
                WHERE singleton
                """
            )
        else:
            pc.execute(
                "UPDATE replication_status SET last_error = %s WHERE singleton",
                (error[:2000],),
            )
    pg.commit()


def _mysql_connection() -> pymysql.Connection:
    return pymysql.connect(
        host=os.environ["MYSQL_HOST"],
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DB"],
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=240,
        cursorclass=pymysql.cursors.SSCursor,
    )


def _pg_connection() -> psycopg.Connection:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return psycopg.connect(database_url, connect_timeout=10)
    return psycopg.connect(
        host=os.environ["PGHOST"],
        port=int(os.getenv("PGPORT", "5432")),
        user=os.environ["PGUSER"],
        password=os.environ["PGPASSWORD"],
        dbname=os.environ["PGDATABASE"],
        connect_timeout=10,
    )


def replicate_once() -> dict[str, int]:
    mysql = _mysql_connection()
    pg = _pg_connection()
    try:
        _prepare_schema(pg)
        with pg.cursor() as pc:
            pc.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_ID,))
            locked = bool(pc.fetchone()[0])
        if not locked:
            print("이미 복제 작업이 실행 중입니다.", flush=True)
            return {}

        _mark_started(pg)
        try:
            counts = {
                "books": _stream_upserts(
                    mysql,
                    pg,
                    name="books",
                    source_query=BOOKS_SOURCE,
                    target_query=BOOKS_TARGET,
                    transform=_book_row,
                    changed_at_index=9,
                    row_id_index=0,
                ),
                "products": _stream_upserts(
                    mysql,
                    pg,
                    name="products",
                    source_query=PRODUCTS_SOURCE,
                    target_query=PRODUCTS_TARGET,
                    transform=_product_row,
                    changed_at_index=5,
                    row_id_index=0,
                ),
                "popularity": _stream_upserts(
                    mysql,
                    pg,
                    name="popularity",
                    source_query=POPULARITY_SOURCE,
                    target_query=POPULARITY_TARGET,
                    transform=_popularity_row,
                    changed_at_index=4,
                    row_id_index=5,
                ),
                "purchases": _stream_upserts(
                    mysql,
                    pg,
                    name="purchases",
                    source_query=_purchases_source(_table_exists(mysql, "cancel")),
                    target_query=PURCHASES_TARGET,
                    transform=_purchase_row,
                    changed_at_index=11,
                    row_id_index=3,
                ),
                "reviews": _stream_upserts(
                    mysql,
                    pg,
                    name="reviews",
                    source_query=REVIEWS_SOURCE,
                    target_query=REVIEWS_TARGET,
                    transform=_review_row,
                    changed_at_index=8,
                    row_id_index=4,
                ),
            }
        except Exception as exc:
            pg.rollback()
            _mark_finished(pg, repr(exc))
            raise
        _mark_finished(pg)
        print("증분 복제 완료", flush=True)
        return counts
    finally:
        mysql.close()
        pg.close()


if __name__ == "__main__":
    replicate_once()
