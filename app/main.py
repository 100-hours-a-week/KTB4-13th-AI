from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

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

    # database 가 죽으면 조회 경로가 통째로 불가능해 트래픽에서 빼야 한다.
    # embedding·llm 은 아직 구현 전이라 항상 unavailable 이므로 판정에 넣지 않는다.
    # ②③ 을 만들 때 down 판정에 포함할지 다시 정한다.
    if components["database"] != "ok":
        status = "down"
    elif all(v == "ok" for v in components.values()):
        status = "ok"
    else:
        status = "degraded"

    body = {
        "status": status,
        "version": settings.release_sha,
        # 복제기가 아직 없어 측정 불가. status 를 바꾸지 않는다
        "replication_lag_seconds": None,
        "components": components,
    }

    # down 은 503. 배포 게이트와 로드밸런서가 이 코드로 인스턴스를 뺀다.
    # 명세는 본문 {"status": "down"} 만 요구하지만, 무엇이 죽었는지 알 수 있게 전체를 싣는다.
    if status == "down":
        return JSONResponse(body, status_code=503)
    return body
