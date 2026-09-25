# syntax=docker/dockerfile:1

# 의존성 빌드 단계
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/root/.cache/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --locked --no-dev --no-install-project

# 임베딩 모델을 이미지에 넣는다(설계 AI-7: 시작할 때 내려받지 않는다). 실행 사용자는 홈이 없어
# 모델을 받아 둘 곳이 없고, 외부망이 막히면 내려받기 재시도로 기동이 수십 분 멈춘다(#184).
# 설정의 EMBEDDING_MODEL 과 같아야 한다. 다르면 기동 로그에 실패가 남고 벡터 기능이 꺼진다.
ARG EMBEDDING_MODEL=intfloat/multilingual-e5-small
ENV HF_HOME=/app/hf
RUN /app/.venv/bin/python -c "import sys; from sentence_transformers import SentenceTransformer; SentenceTransformer(sys.argv[1], device='cpu')" "$EMBEDDING_MODEL"


# 실행 단계
FROM python:3.12-slim

# 모델은 이미지 안의 것만 쓴다. 없으면 내려받으러 가지 않고 곧바로 실패한다.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    HF_HOME=/app/hf \
    HF_HUB_OFFLINE=1

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/hf /app/hf
COPY app ./app
COPY main.py ./main.py

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]