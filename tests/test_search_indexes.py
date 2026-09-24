"""기동할 때 검색 색인을 확인하는지 본다.

색인은 마이그레이션을 손으로 적용해야 해서(위키 #160) 빠져도 에러가 나지 않고 검색만
느려진다. 뒷부분은 실제 PostgreSQL 이 있어야 해서 SEARCH_TEST_DATABASE_URL 에 주소를 주면
돈다. 색인을 지우는 테스트는 트랜잭션을 되돌려 흔적을 남기지 않는다.
"""

import asyncio
import logging
import os

import asyncpg
import pytest

from app.core import db


def test_빠진_색인이_있으면_이름과_조치를_ERROR로_남긴다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _missing(conn) -> list[str]:
        return ["v_books_author_trgm_idx"]

    monkeypatch.setattr(db, "missing_search_indexes", _missing)

    with caplog.at_level(logging.ERROR, logger="app.core.db"):
        asyncio.run(db.report_missing_search_indexes(None))

    assert "v_books_author_trgm_idx" in caplog.text
    assert "마이그레이션" in caplog.text


def test_색인이_다_있으면_아무것도_남기지_않는다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def _none(conn) -> list[str]:
        return []

    monkeypatch.setattr(db, "missing_search_indexes", _none)

    with caplog.at_level(logging.WARNING, logger="app.core.db"):
        asyncio.run(db.report_missing_search_indexes(None))

    assert caplog.text == ""


def test_확인하다_실패해도_서버를_멈추지_않는다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # 색인이 없어도 검색 결과는 같고 느릴 뿐이다. 확인 쿼리가 실패했다고 기동을 막지 않는다.
    async def _boom(conn) -> list[str]:
        raise RuntimeError("DB 끊김")

    monkeypatch.setattr(db, "missing_search_indexes", _boom)

    with caplog.at_level(logging.ERROR, logger="app.core.db"):
        asyncio.run(db.report_missing_search_indexes(None))

    assert "검색 색인" in caplog.text


# ---------------------------------------------------------------------------
# 실제 DB 가 필요한 테스트
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


def _run_in_rollback(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


@needs_db
def test_로컬_DB에는_빠진_검색_색인이_없다() -> None:
    assert _run_in_rollback(db.missing_search_indexes) == []


@needs_db
def test_색인을_지우면_그_이름이_빠진_것으로_나온다() -> None:
    async def _check(conn: asyncpg.Connection) -> list[str]:
        await conn.execute("DROP INDEX v_books_author_trgm_idx")
        return await db.missing_search_indexes(conn)

    assert _run_in_rollback(_check) == ["v_books_author_trgm_idx"]
