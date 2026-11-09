"""사진 한 장에서 책을 찾는 전체 순서(#615).

받기 → 읽기(모델 한 번) → 책마다 찾기 → 같은 책 묶기다. 사진 종류에 따라 찾는 규칙이 다르다. 표지는 글씨가 커서
저자까지 믿고(match_cover), 책장은 책등의 작은 저자 글씨를 지어낼 때가 많아 제목만 엄격하게 본다(match_shelf).
표지 규칙을 책장에 쓰면 교육장 127권 중 57권, 책장 규칙을 쓰면 97권을 맞게 확정했다(#615 시험 결과).

나의 도서관은 후보를 보여 주지 않는다. 확정하지 못한 책은 모두 못 찾음이다.

로그에는 사진 종류와 권 수만 남긴다. 사진에서 읽은 글자는 남기지 않는다(대화 원문과 같이 다룬다).
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import asyncpg

from app.core import db, editions
from app.core.config import get_settings
from app.photo import fetch, match_cover, match_shelf, read
from app.photo.catalog import Book
from app.photo.read import BookText, PhotoType

logger = logging.getLogger(__name__)

# 한 장에서 찾는 책의 상한. 책마다 책 표를 몇 번씩 읽어서, 모델이 끝없이 적어도 일이 커지지 않게 한다.
# 시험에서 가장 많이 읽은 사진이 78권이었다
MAX_BOOKS = 100

State = Literal["FOUND", "NOT_FOUND"]


class ScanTimeoutError(Exception):
    """사진 한 장을 제한 시간 안에 다 보지 못했다. 받기·읽기·찾기 어디서 넘었든 같다."""


@dataclass(frozen=True)
class Item:
    """사진 속 책 한 권. 찾았으면 book 이 있다."""

    state: State
    # 사진에서 읽은 글자. 화면이 못 찾은 책을 보여 줄 때 쓴다
    read_text: str
    book: Book | None = None
    # 찾은 길(로그·평가용). ISBN 으로 찾은 것과 글자로 찾은 것을 가른다
    via: Literal["isbn", "text"] | None = None


@dataclass(frozen=True)
class ScanResult:
    photo_type: PhotoType
    items: list[Item]


def url_prefixes() -> list[str]:
    raw = get_settings().photo_image_url_prefixes
    return [p.strip() for p in raw.split(",") if p.strip()]


def read_text(text: BookText) -> str:
    return " ".join(part for part in (text.title, text.volume) if part)


async def find(conn: asyncpg.Connection, text: BookText, photo_type: PhotoType) -> Item:
    """책 한 권을 찾는다. 사진 종류에 맞는 규칙을 쓴다."""
    shown = read_text(text)
    if photo_type == "cover":
        result = await match_cover.match(conn, text)
        if result.kind == "found":
            return Item("FOUND", shown, result.books[0].book, result.via)
        return Item("NOT_FOUND", shown)
    book, via = await match_shelf.match(conn, text)
    if book is not None:
        return Item("FOUND", shown, book, via)
    return Item("NOT_FOUND", shown)


def merge(items: list[Item]) -> list[Item]:
    """같은 책 두 권은 하나로 둔다. 나의 도서관은 같은 책을 한 번만 담는다(user_library_books 의 UNIQUE).

    못 찾은 책은 읽은 글자가 같으면 같은 책으로 본다. 읽은 글자가 없으면(ISBN 만 보였는데 책 표에 없음 등)
    화면에 보여 줄 것이 없어 뺀다. 순서는 처음 나온 자리를 지킨다(책장이면 왼쪽부터).
    """
    seen: set[object] = set()
    merged = []
    for item in items:
        if item.book is not None:
            identity: object = ("book", item.book.book_id)
        else:
            text_key = editions.title_key(item.read_text)
            if not text_key:
                continue
            identity = ("text", text_key)
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(item)
    return merged


async def _scan(image_ref: str) -> ScanResult:
    image = await fetch.fetch(image_ref, url_prefixes())
    text = await read.read(image)
    photo_type = text.photo_type or "none"
    if photo_type == "none" or not text.books:
        return ScanResult(photo_type, [])
    books = text.books[:MAX_BOOKS]
    # 한 연결에서 차례로 찾는다. 한 사진이 연결 여러 개를 쥐면 같은 때 들어온 다른 요청이 기다린다
    items: list[Item] = []
    async with db.get_pool().acquire() as conn:
        for book in books:
            try:
                item = await find(conn, book, photo_type)
            except TimeoutError:
                # DB 조회 제한에 걸린 책만 넘긴다. 사진 전체 제한은 CancelledError 로 전달된다.
                logger.warning("사진 책 찾기: 한 권의 DB 조회 제한 시간 초과")
                item = Item("NOT_FOUND", read_text(book))
            items.append(item)
    return ScanResult(photo_type, merge(items))


async def scan(image_ref: str) -> ScanResult:
    """사진 주소 하나로 책을 찾는다.

    사진을 쓸 수 없으면 fetch.ImageError, 모델이 답하지 못하면 LLMUnavailableError(제한 시간이면 LLMTimeoutError),
    전체가 제한 시간을 넘기면 ScanTimeoutError 다. 책을 하나도 못 찾은 것은 오류가 아니라 빈 items 다.
    """
    try:
        async with asyncio.timeout(get_settings().photo_timeout_seconds):
            result = await _scan(image_ref)
    except TimeoutError as e:
        raise ScanTimeoutError() from e
    found = sum(item.state == "FOUND" for item in result.items)
    logger.info(
        "사진 책 찾기: %s, %d권 중 %d권 찾음",
        result.photo_type,
        len(result.items),
        found,
    )
    return result
