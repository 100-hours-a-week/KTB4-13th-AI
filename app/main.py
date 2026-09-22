import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core import db
from app.core.config import get_settings
from app.gateway import embedding
from app.routers import agent, chat, embeddings, extractions, feed, profile, search

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 앱이 뜰 때
    await db.connect()
    # 임베딩 모델을 미리 읽어 둔다. 첫 요청에서 읽으면 그 요청만 수 초 걸리고,
    # 모델 파일이 없거나 차원이 어긋난 것도 첫 호출에서야 드러난다.
    try:
        await asyncio.to_thread(embedding.load_model)
    except Exception:
        # 모델이 없어도 DB 작업과 ⑧ /health 는 계속 돌아야 한다.
        # 상태는 /health 의 embedding 칸이 unavailable 로 알린다.
        logger.exception("임베딩 모델 예열 실패")
    yield
    # 앱이 내려갈 때
    await db.disconnect()


app = FastAPI(lifespan=lifespan)

# --- 로컬 개발 전용 CORS 시작 — dev/ 테스트 화면이 브라우저에서 직접 이 서버를
# fetch() 하기 위함. DEV_CORS_ORIGINS가 비어있으면(운영 기본값) 아무 효과 없다.
# BE 연동을 시작하면 이 블록과 dev/ 폴더를 함께 삭제한다.
_dev_cors_origins = get_settings().dev_cors_origins
if _dev_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in _dev_cors_origins.split(",")],
        allow_methods=["POST"],
        allow_headers=["*"],
    )
# --- 로컬 개발 전용 CORS 끝 ---

app.include_router(agent.router)
app.include_router(chat.router)
app.include_router(extractions.router)
app.include_router(search.router)
app.include_router(embeddings.router)
app.include_router(feed.router)
app.include_router(profile.router)


@app.get("/health")
async def health():
    settings = get_settings()

    components = {
        "gateway": "ok",
        "database": "ok" if await db.check_database() else "unavailable",
        "vector_index": "ok" if await db.check_vector_index() else "unavailable",
        # 모델이 메모리에 올라와 있는지만 본다. 점검마다 실제로 임베딩을 돌리면
        # 헬스체크가 CPU 를 잡아먹어 정작 요청 처리가 느려진다.
        "embedding": "ok" if embedding.is_loaded() else "unavailable",
        # ③ 을 만들면 실제 점검으로 바꾼다
        "llm": "unavailable",
    }

    # database 가 죽으면 조회 경로가 통째로 불가능해 트래픽에서 빼야 한다.
    # embedding 이 없으면 ① 은 키워드 전용(X-Degraded: keyword-only)으로,
    # ④ 는 규칙 점수만으로 강등해 계속 응답하므로 down 이 아니라 degraded 다.
    # llm 은 아직 구현 전이라 항상 unavailable 이다. ③ 을 만들 때 다시 정한다.
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
