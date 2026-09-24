"""AI 전용 PostgreSQL 연결 풀.

풀은 앱이 뜰 때 한 번 만들고 내려갈 때 닫는다.
요청마다 새로 연결하면 접속 비용(수십 ms)이 매 요청에 붙는다.
"""

import logging

import asyncpg

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None

# ① 검색이 기대는 글자 조각 색인. 마이그레이션을 손으로 적용해서(위키 #160) 빠져도 에러가
# 나지 않고 검색이 표 전체를 훑어 느려지기만 한다. 그래서 기동할 때 확인한다(#154).
SEARCH_INDEXES = (
    "v_books_title_trgm_idx",
    "v_books_author_trgm_idx",
    "v_books_title_nospace_trgm_idx",
)


async def connect() -> None:
    global _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        # /health 가 DB를 기다리다 멈추면 멀쩡한 서버가 죽은 걸로 오판된다
        command_timeout=5,
        server_settings={"timezone": settings.tz},
    )


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB 풀이 없습니다. 앱 시작 시 connect()를 호출해야 합니다.")
    return _pool


async def missing_search_indexes(conn: asyncpg.Connection) -> list[str]:
    """SEARCH_INDEXES 중 없거나 깨진(재색인 중) 색인 이름."""
    rows = await conn.fetch(
        "SELECT name FROM unnest($1::text[]) WITH ORDINALITY AS s(name, n)"
        " LEFT JOIN pg_index i ON i.indexrelid = to_regclass('public.' || name)"
        " WHERE i.indisvalid IS NOT TRUE ORDER BY n",
        list(SEARCH_INDEXES),
    )
    return [r["name"] for r in rows]


async def report_missing_search_indexes(conn: asyncpg.Connection) -> None:
    """빠진 검색 색인이 있으면 ERROR 로 남긴다. 서버는 그대로 뜬다.

    색인이 없어도 검색 결과는 같고 느릴 뿐이라, 기동을 막으면 오히려 전체가 멈춘다.
    확인 자체가 실패해도 같은 이유로 로그만 남긴다.
    """
    try:
        missing = await missing_search_indexes(conn)
    except Exception:
        # 확인이 안 돼도 기동은 계속한다
        logger.exception("검색 색인을 확인하지 못했습니다")
        return
    if missing:
        logger.error(
            "검색 색인이 없거나 깨졌습니다: %s. 검색이 표 전체를 훑어 느려집니다."
            " db/migrations 의 마이그레이션을 적용하세요(위키 #160).",
            ", ".join(missing),
        )


async def check_database() -> bool:
    """⑧ /health 의 database 칸. 가볍게 왕복 한 번만 한다."""
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:  # noqa: BLE001 — /health는 어떤 실패든 unavailable로 보고한다
        return False
    return True


async def check_vector_index() -> bool:
    """⑧ /health 의 vector_index 칸. HNSW 인덱스가 있고 유효한지 본다.

    to_regclass 는 인덱스가 없으면 NULL을 돌려주므로 조회 결과가 비고,
    재색인 중이라 인덱스가 깨져 있으면 indisvalid 가 false 다.
    """
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            valid = await conn.fetchval(
                "SELECT indisvalid FROM pg_index "
                "WHERE indexrelid = to_regclass('public.book_embeddings_hnsw_idx')"
            )
    except Exception:  # noqa: BLE001 — /health는 어떤 실패든 unavailable로 보고한다
        return False
    return bool(valid)
