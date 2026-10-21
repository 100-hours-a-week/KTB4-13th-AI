"""관심사별 홈 추천(#395)이 실제로 켜졌는지 — 설정이 켜져 있어도 새 표·칸이 없으면 끈 채로 돈다(#420).

켜기는 설정의 기본값을 바꾸는 PR로 한다. main 에 머지하면 개발에 바로 나가고 다음 운영 배포에도 실린다.
그때 운영 DB 에 마이그레이션(20261016_book_classes.sql, 20261016_taste_profile_interests.sql)과 책 분류
데이터가 아직 없으면, ⑥ 과 홈이 없는 표·칸을 읽다가 요청마다 실패한다. 그래서 서버가 뜰 때 한 번 확인해
두고, 하나라도 없으면 꺼진 것처럼 돈다. 마이그레이션이 들어간 뒤 서버가 다시 뜨면 켜진다.
"""

import logging

import asyncpg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 20261016_taste_profile_interests.sql 이 더하는 칸
INTEREST_COLUMNS = (
    "interest_scores",
    "interest_vectors",
    "conditions",
    "book_first_seen",
    "feed",
    "feed_version",
    "feed_prev",
    "feed_partial",
)

# 확인하기 전에는 설정을 그대로 따른다. 서버가 뜰 때 check 가 정하고, 테스트는 설정만 바꿔 켠다.
_schema_ready = True


def on() -> bool:
    """관심사별 홈 추천을 돌릴지. 설정이 켜져 있고 새 표·칸이 있을 때만 참이다."""
    return get_settings().interest_feed and _schema_ready


async def missing(conn: asyncpg.Connection) -> list[str]:
    """새 표·칸 중 없는 것. 책 분류 표가 비어 있어도 없는 것으로 본다(데이터는 따로 적재한다)."""
    rows = await conn.fetch(
        "SELECT name FROM unnest($1::text[]) AS c(name)"
        " WHERE NOT EXISTS (SELECT 1 FROM information_schema.columns i"
        "  WHERE i.table_schema = 'public' AND i.table_name = 'taste_profile'"
        "  AND i.column_name = c.name)",
        list(INTEREST_COLUMNS),
    )
    absent = [f"taste_profile.{r['name']}" for r in rows]
    if await conn.fetchval("SELECT to_regclass('public.book_classes') IS NULL"):
        absent.append("book_classes")
    elif not await conn.fetchval("SELECT EXISTS (SELECT 1 FROM book_classes)"):
        absent.append("book_classes 데이터")
    return absent


async def check(conn: asyncpg.Connection) -> None:
    """설정이 켜져 있으면 새 표·칸을 확인해, 없으면 끄고 WARNING 으로 남긴다. 서버는 그대로 뜬다.

    확인 자체가 실패해도 끈다. 켠 채로 두면 요청마다 실패할 수 있다.
    """
    global _schema_ready
    if not get_settings().interest_feed:
        return
    try:
        absent = await missing(conn)
    except Exception:
        _schema_ready = False
        logger.exception("관심사별 홈 추천의 표·칸을 확인하지 못해 끈 채로 돈다(#420)")
        return
    _schema_ready = not absent
    if not absent:
        logger.info("관심사별 홈 추천이 켜져 있다(#395)")
        return
    logger.warning(
        "관심사별 홈 추천이 설정으로 켜져 있지만 %s 가 없어 끈 채로 돈다."
        " 마이그레이션과 책 분류 데이터를 넣은 뒤 서버를 다시 띄우면 켜진다(#420).",
        ", ".join(absent),
    )
