from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core import db
from app.core.config import get_settings


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱이 뜰 때
    await db.connect()
    yield
    # 앱이 내려갈 때
    await db.disconnect()


app = FastAPI(lifespan=lifespan)


@app.get("/health")
async def health():
    settings = get_settings()

    components = {
        "gateway": "ok",
        "database": "ok" if await db.check_database() else "unavailable",
        "vector_index": "ok" if await db.check_vector_index() else "unavailable",
        # ② 를 만들면 실제 점검으로 바꾼다
        "embedding": "unavailable",
        # ③ 을 만들면 실제 점검으로 바꾼다
        "llm": "unavailable",
    }

    status = "ok" if all(v == "ok" for v in components.values()) else "degraded"

    return {
        "status": status,
        "version": settings.release_sha,
        # 복제기가 아직 없어 측정 불가. status 를 바꾸지 않는다
        "replication_lag_seconds": None,
        "components": components,
    }
