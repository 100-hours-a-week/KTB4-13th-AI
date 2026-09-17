"""AI 전용 PostgreSQL 연결 풀.

풀은 앱이 뜰 때 한 번 만들고 내려갈 때 닫는다.
요청마다 새로 연결하면 접속 비용(수십 ms)이 매 요청에 붙는다.
"""

import asyncpg

from app.core.config import get_settings

_pool: asyncpg.Pool | None = None


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


async def check_database() -> bool:
    """⑧ /health 의 database 칸. 가볍게 왕복 한 번만 한다."""
    if _pool is None:
        return False
    try:
        async with _pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:
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
    except Exception:
        return False
    return bool(valid)
