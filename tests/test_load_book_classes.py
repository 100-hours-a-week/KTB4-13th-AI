"""AI 책 분류 표 넣기 작업 테스트(#421).

대부분 DB 없이 돈다 — 연결을 가짜로 바꿔 끼운다. 맨 아래의 실제 DB 테스트만 PostgreSQL 이 필요하다.
"""

import asyncio
import gzip
import io
import os
from pathlib import Path

import asyncpg
import pytest

from app.jobs import load_book_classes as job

HEADER = "isbn13,class_no,reader_code,form_code,page_count"
OLD_ROW = ("9780000000002", "911", "0", "1", 100)


class FakeConn:
    """표를 목록으로 흉내 낸다. 트랜잭션에서 예외가 나면 들어가기 전 표로 되돌린다.

    받은 명령은 순서대로 log 에 적는다.
    """

    def __init__(self, table: list[tuple]) -> None:
        self.table = list(table)
        self.log: list[str] = []

    def transaction(self):
        conn = self

        class _Transaction:
            async def __aenter__(self) -> None:
                conn.log.append("begin")
                conn.saved = list(conn.table)

            async def __aexit__(self, exc_type, exc, tb) -> bool:
                if exc_type is None:
                    conn.log.append("commit")
                else:
                    conn.log.append("rollback")
                    conn.table = conn.saved
                return False

        return _Transaction()

    async def execute(self, sql: str) -> None:
        self.log.append(sql)
        if sql.startswith("TRUNCATE"):
            self.table = []

    async def copy_records_to_table(self, table: str, *, records, columns) -> None:
        self.log.append(f"COPY {table} {','.join(columns)}")
        self.table.extend(records)

    async def fetchval(self, sql: str) -> int:
        return len(self.table)


def _replace(conn: FakeConn, *lines: str):
    return asyncio.run(job.replace(conn, [HEADER, *lines]))


def _parse(*lines: str):
    return job.parse([HEADER, *lines])


def test_칸을_받은_그대로_넣고_빈_칸은_NULL로_둔다() -> None:
    rows, stats = _parse("9788937462788,813.7,0,3,312", "9788937462795,,,,")

    assert rows == [
        ("9788937462788", "813.7", "0", "3", 312),
        ("9788937462795", None, None, None, None),
    ]
    assert stats.read == 2
    assert not stats.skipped


def test_앞뒤_공백은_지운다() -> None:
    rows, _ = _parse(" 9788937462788 , 813.7 ,0, 3 , 312 ")

    assert rows == [("9788937462788", "813.7", "0", "3", 312)]


def test_머리글이_다르면_멈춘다() -> None:
    with pytest.raises(ValueError, match="머리글"):
        job.parse(["isbn,class_no,reader_code,form_code,page_count"])


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("978893746278,813.7,0,3,312", "isbn13"),
        ("97889374627ab,813.7,0,3,312", "isbn13"),
        ("9788937462788,813.712345678901234567,0,3,312", "class_no"),
        ("9788937462788,813.7,01,3,312", "reader_code"),
        ("9788937462788,813.7,0,x,312", "form_code"),
        ("9788937462788,813.7,0,3,0", "page_count"),
        ("9788937462788,813.7,0,3,삼백", "page_count"),
        ("9788937462788,813.7,0,3,2147483648", "page_count"),
        ("9788937462788,813.7,0,3", "칸 수"),
    ],
)
def test_칸이_틀린_줄은_이유를_세고_버린다(line: str, reason: str) -> None:
    rows, stats = _parse(line, "9788937462795,,,,")

    assert rows == [("9788937462795", None, None, None, None)]
    assert stats.read == 2
    assert stats.skipped == {reason: 1}


def test_같은_ISBN이_다시_나오면_앞의_줄만_쓴다() -> None:
    rows, stats = _parse("9788937462788,813.7,0,3,312", "9788937462788,911,1,1,100")

    assert rows == [("9788937462788", "813.7", "0", "3", 312)]
    assert stats.skipped == {"중복": 1}


def test_gzip_파일도_읽는다() -> None:
    raw = f"{HEADER}\n9788937462788,813.7,0,3,312\n".encode()

    lines = list(job.open_text(io.BytesIO(gzip.compress(raw))))

    assert job.parse(lines)[0] == [("9788937462788", "813.7", "0", "3", 312)]


def test_BOM이_붙은_파일도_읽는다() -> None:
    raw = f"﻿{HEADER}\n9788937462788,,,,\n".encode()

    lines = list(job.open_text(io.BytesIO(raw)))

    assert job.parse(lines)[0] == [("9788937462788", None, None, None, None)]


def test_한_트랜잭션에서_표를_비우고_파일의_줄로_채운다() -> None:
    conn = FakeConn([OLD_ROW])

    count, stats = _replace(conn, "9788937462788,813.7,0,3,312", "9788937462795,,,,")

    assert count == 2
    assert conn.table == [
        ("9788937462788", "813.7", "0", "3", 312),
        ("9788937462795", None, None, None, None),
    ]
    assert conn.log == [
        "begin",
        "TRUNCATE book_classes",
        "COPY book_classes isbn13,class_no,reader_code,form_code,page_count",
        "commit",
    ]
    assert stats.filled == {
        "class_no": 1,
        "reader_code": 1,
        "form_code": 1,
        "page_count": 1,
    }


def test_버린_줄이_1퍼센트_이하면_넣는다() -> None:
    good = [f"978{n:010d},,,," for n in range(199)]
    conn = FakeConn([OLD_ROW])

    count, stats = _replace(conn, *good, "bad,,,,")

    assert count == 199
    assert stats.skipped == {"isbn13": 1}


def test_버린_줄이_1퍼센트를_넘으면_되돌려_표를_그대로_둔다() -> None:
    conn = FakeConn([OLD_ROW])

    with pytest.raises(job.RefusedError, match="버린 줄"):
        _replace(conn, "9788937462788,,,,", "bad,,,,")

    assert conn.table == [OLD_ROW]
    assert conn.log[-1] == "rollback"


def test_넣을_줄이_없으면_되돌린다() -> None:
    conn = FakeConn([OLD_ROW])

    with pytest.raises(job.RefusedError, match="넣을 줄이 없습니다"):
        _replace(conn)

    assert conn.table == [OLD_ROW]


def test_머리글이_다르면_되돌린다() -> None:
    conn = FakeConn([OLD_ROW])

    with pytest.raises(ValueError, match="머리글"):
        asyncio.run(job.replace(conn, ["isbn,a,b,c,d", "9788937462788,,,,"]))

    assert conn.table == [OLD_ROW]
    assert conn.log[-1] == "rollback"


_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_MIGRATION = (
    Path(__file__).parent.parent / "db" / "migrations" / "20261016_book_classes.sql"
)


@pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)
def test_실제_DB에서_전량_교체하고_틀린_파일은_되돌린다() -> None:
    # 마이그레이션의 정의 그대로 임시 표를 만든다. 임시 표가 같은 이름의 표를 가려서,
    # 이 연결 안의 넣기는 임시 표에만 닿는다.
    ddl = _MIGRATION.read_text(encoding="utf-8").replace(
        "CREATE TABLE IF NOT EXISTS book_classes", "CREATE TEMP TABLE book_classes"
    )

    async def _go() -> tuple[list, list]:
        conn = await asyncpg.connect(_DB_URL)
        try:
            await conn.execute(ddl)
            await job.replace(
                conn, [HEADER, "9788937462788,813.7,0,3,312", "9788937462795,,,,"]
            )
            await job.replace(conn, [HEADER, "9788937462801,911,1,1,100"])
            first = await conn.fetch("SELECT * FROM book_classes ORDER BY isbn13")
            with pytest.raises(job.RefusedError):
                await job.replace(conn, [HEADER, "bad,,,,"])
            # 넣는 도중(COPY 중)에 멈춰도 되돌려진다.
            with pytest.raises(ValueError, match="머리글"):
                await job.replace(conn, ["isbn,a,b,c,d", "9788937462788,,,,"])
            second = await conn.fetch("SELECT isbn13 FROM book_classes")
            return [tuple(r)[:5] for r in first], [r["isbn13"] for r in second]
        finally:
            await conn.close()

    first, second = asyncio.run(_go())

    assert first == [("9788937462801", "911", "1", "1", 100)]
    assert second == ["9788937462801"]
