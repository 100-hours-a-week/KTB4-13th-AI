"""AI 책 분류 표 넣기 — 수집 쪽이 덤프에서 만든 파일을 book_classes 에 넣는다(#421, 설계 #395).

실행(서버). 파일을 AI 컨테이너의 표준 입력으로 흘려 넣는다. 개발 서버는 컨테이너가 bookjeok-ai-dev 다.
    docker exec -i bookjeok-ai python -m app.jobs.load_book_classes < book_classes.csv.gz
실행(로컬): uv run python -m app.jobs.load_book_classes book_classes.csv.gz
확인: 끝에 표의 행 수와 칸마다 값이 있는 행 수가 찍힌다. DB 에서 다시 보려면
    docker exec bookjeok-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT count(*) FROM book_classes"'

파일: UTF-8 CSV(gzip 이어도 된다). 첫 줄은 머리글 isbn13,class_no,reader_code,form_code,page_count.
빈 칸은 NULL 로 넣는다. 칸이 틀린 줄은 이유를 세고 버리며, 같은 isbn13 이 다시 나오면 앞의 줄만 쓴다.

매번 전량 교체한다. 한 트랜잭션에서 표를 비우고 채우므로, 중간에 실패하면 넣기 전 그대로 남는다.
넣을 줄이 없거나 버린 줄이 1%를 넘어도 되돌린다. 틀린 파일로 멀쩡한 표를 비우지 않게 하려는 것이다.
넣는 동안 이 표를 읽는 쪽은 기다리므로, BE 밤 배치와 겹치지 않는 때에 돌린다.
"""

import argparse
import asyncio
import csv
import gzip
import io
import logging
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import BinaryIO, TextIO

import asyncpg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 칸 순서. 머리글이 이와 다르면 다른 파일로 보고 멈춘다.
COLUMNS = ("isbn13", "class_no", "reader_code", "form_code", "page_count")

# 마이그레이션(db/migrations/20261016_book_classes.sql)의 검사와 같다. 여기서 먼저 걸러야
# 한 줄 때문에 전체 넣기가 실패하지 않는다.
_ISBN13 = re.compile(r"[0-9]{13}")
_CODE = re.compile(r"[0-9]")
CLASS_NO_MAX_CHARS = 20
# page_count 칸(integer)에 들어가는 가장 큰 값.
PAGE_COUNT_MAX = 2_147_483_647

_GZIP_MAGIC = b"\x1f\x8b"

# 버린 줄이 읽은 줄의 이만큼을 넘으면 넣지 않는다. 틀린 파일로 멀쩡한 표를 비우지 않게 하려는 것이다.
MAX_SKIP_RATIO = 0.01

_TRUNCATE = "TRUNCATE book_classes"
_COUNT = "SELECT count(*) FROM book_classes"

Row = tuple[str, str | None, str | None, str | None, int | None]


@dataclass
class Stats:
    """읽은 줄 수, 버린 줄의 이유별 수, 넣은 줄 중 칸마다 값이 있는 수. 머리글은 세지 않는다."""

    read: int = 0
    skipped: Counter = field(default_factory=Counter)
    filled: Counter = field(default_factory=Counter)


class RefusedError(Exception):
    """넣으면 안 되는 파일이다. 표는 넣기 전 그대로 남는다."""


def open_text(stream: BinaryIO) -> TextIO:
    """바이트 흐름을 글자로 읽는다. gzip 이면 풀고, 맨 앞의 BOM 은 버린다."""
    buffered = io.BufferedReader(stream) if not hasattr(stream, "peek") else stream
    if buffered.peek(2)[:2] == _GZIP_MAGIC:
        buffered = gzip.GzipFile(fileobj=buffered)
    return io.TextIOWrapper(buffered, encoding="utf-8-sig", newline="")


def _blank_to_none(value: str) -> str | None:
    return value or None


def _row(cells: list[str]) -> tuple[Row | None, str | None]:
    """한 줄을 넣을 값으로 바꾼다. 틀린 칸이 있으면 (None, 이유)."""
    if len(cells) != len(COLUMNS):
        return None, "칸 수"
    isbn13, class_no, reader_code, form_code, page_count = (c.strip() for c in cells)
    if not _ISBN13.fullmatch(isbn13):
        return None, "isbn13"
    if len(class_no) > CLASS_NO_MAX_CHARS:
        return None, "class_no"
    if reader_code and not _CODE.fullmatch(reader_code):
        return None, "reader_code"
    if form_code and not _CODE.fullmatch(form_code):
        return None, "form_code"
    pages = None
    if page_count:
        if not page_count.isdecimal() or not 0 < int(page_count) <= PAGE_COUNT_MAX:
            return None, "page_count"
        pages = int(page_count)
    row = (
        isbn13,
        _blank_to_none(class_no),
        _blank_to_none(reader_code),
        _blank_to_none(form_code),
        pages,
    )
    return row, None


def iter_rows(lines: Iterable[str], stats: Stats) -> Iterator[Row]:
    """머리글을 확인하고 넣을 줄을 하나씩 내준다. 읽은 결과는 stats 에 센다.

    126만 줄을 한꺼번에 들고 있지 않으려고 하나씩 내준다. 운영 서버는 메모리가 빠듯하다.
    같은 ISBN 을 가리려고 기억하는 것도 글자 대신 숫자로 둔다.
    """
    reader = csv.reader(lines)
    header = [c.strip() for c in next(reader, [])]
    if tuple(header) != COLUMNS:
        raise ValueError(f"머리글이 다릅니다: {header} (기대: {list(COLUMNS)})")

    seen: set[int] = set()
    for cells in reader:
        stats.read += 1
        row, reason = _row(cells)
        if row is None:
            stats.skipped[reason] += 1
            continue
        key = int(row[0])
        if key in seen:
            stats.skipped["중복"] += 1
            continue
        seen.add(key)
        for name, value in zip(COLUMNS[1:], row[1:], strict=True):
            if value is not None:
                stats.filled[name] += 1
        yield row


def parse(lines: Iterable[str]) -> tuple[list[Row], Stats]:
    """iter_rows 를 끝까지 돌린 결과. 작은 파일과 테스트에 쓴다."""
    stats = Stats()
    return list(iter_rows(lines, stats)), stats


def check(stats: Stats) -> None:
    """다 읽은 뒤 넣어도 되는지 본다. 안 되면 RefusedError."""
    skipped = sum(stats.skipped.values())
    if stats.read == skipped:
        raise RefusedError("넣을 줄이 없습니다")
    if skipped / stats.read > MAX_SKIP_RATIO:
        raise RefusedError(
            f"버린 줄이 {skipped}/{stats.read}로 {MAX_SKIP_RATIO:.0%}를 넘습니다: "
            f"{dict(stats.skipped)}"
        )


async def replace(conn: asyncpg.Connection, lines: Iterable[str]) -> tuple[int, Stats]:
    """한 트랜잭션에서 표를 비우고 파일의 줄로 채운다. 넣은 뒤의 행 수와 읽은 결과를 돌려준다.

    줄을 하나씩 흘려 넣고, 다 넣은 뒤 커밋하기 전에 check 로 본다. 통과하지 못하거나 중간에
    실패하면 트랜잭션이 되돌려져 표는 넣기 전 그대로다.
    """
    stats = Stats()
    async with conn.transaction():
        await conn.execute(_TRUNCATE)
        await conn.copy_records_to_table(
            "book_classes", records=iter_rows(lines, stats), columns=list(COLUMNS)
        )
        check(stats)
        count = await conn.fetchval(_COUNT)
    return count, stats


def _input(path: str | None) -> BinaryIO:
    if path in (None, "-"):
        return sys.stdin.buffer
    # main 의 with 가 닫는다.
    return open(path, "rb")


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    parser = argparse.ArgumentParser(
        description="수집 쪽이 만든 책 분류 파일을 book_classes 에 넣는다(전량 교체)"
    )
    parser.add_argument(
        "path", nargs="?", help="파일 경로. 없거나 - 이면 표준 입력에서 읽는다"
    )
    args = parser.parse_args(argv)

    # 서버의 연결 풀은 /health 용으로 5초 제한이 걸려 있어 쓰지 않는다(embed_books 와 같음).
    conn = await asyncpg.connect(get_settings().database_url)
    try:
        with open_text(_input(args.path)) as text:
            count, stats = await replace(conn, text)
    except (RefusedError, ValueError) as exc:
        logger.error("넣지 않았습니다. 표는 넣기 전 그대로입니다: %s", exc)
        return 1
    finally:
        await conn.close()
    logger.info(
        "끝. book_classes %d행(읽은 줄 %d, 버린 줄 %s). 값이 있는 행: %s",
        count,
        stats.read,
        dict(stats.skipped) or "없음",
        dict(stats.filled),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
