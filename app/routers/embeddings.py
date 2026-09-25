"""② 텍스트 임베딩: POST /embeddings (내부 전용)

명세: docs/wiki/ai/1-model-api/spec.md ② POST /embeddings
LLM도 DB도 안 쓰는 유일한 엔드포인트라 독립적으로 완성 가능하다.

응답을 app.core.responses 로 직접 조립하는 이유: FastAPI 기본 검증 실패는 422 `{"detail": ...}` 인데
계약은 400 `{"message": "invalid_request", "data": null}` 이다. 공통 예외 핸들러가
들어오기 전까지 이 라우터가 스스로 계약을 지킨다.
"""

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.core import body, responses
from app.gateway import embedding

logger = logging.getLogger(__name__)

router = APIRouter(tags=["embeddings"])

# 명세 ②: 한 번에 1~256건. 대량 적재는 호출자가 나눠서 부른다.
MAX_TEXTS = 256
# 명세 ②: 요청 본문 약 4MB 초과는 413
MAX_BODY_BYTES = 4 * 1024 * 1024


class EmbeddingsRequest(BaseModel):
    texts: list[str]
    # 기본값 document 는 명세 값이다. 용도에 따라 붙는 접두어가 달라진다.
    purpose: Literal["query", "document"] = "document"


def parse_request(payload: Any) -> EmbeddingsRequest | None:
    """계약에 맞으면 요청 객체, 아니면 None 을 돌려준다."""
    if not isinstance(payload, dict):
        return None
    try:
        req = EmbeddingsRequest.model_validate(payload)
    except ValidationError:
        return None
    if not req.texts or len(req.texts) > MAX_TEXTS:
        return None
    # 빈 문자열은 접두어만 임베딩한 벡터가 되어 어떤 검색어와도 어중간하게 비슷해진다.
    # 조용히 쓸모없는 벡터를 만드는 대신 여기서 끊는다.
    if any(not t.strip() for t in req.texts):
        return None
    return req


@router.post("/embeddings")
async def embeddings(request: Request) -> JSONResponse:
    raw = await body.read_limited(request, MAX_BODY_BYTES)
    if raw is None:
        return responses.error(413, "payload_too_large")

    try:
        payload = json.loads(raw)
    except ValueError:
        # JSON 문법 오류와, UTF-8 이 아닌 본문(UnicodeDecodeError) 둘 다 ValueError 다.
        # JSONDecodeError 만 잡으면 뒤의 것이 500 으로 샌다.
        return responses.error(400, "invalid_request")

    req = parse_request(payload)
    if req is None:
        return responses.error(400, "invalid_request")

    try:
        vectors, dim, model = await embedding.embed(req.texts, req.purpose)
    except Exception:
        # 모델 파일이 없거나 차원이 어긋나면 여기서 터진다. 그대로 두면 FastAPI 기본
        # 500 {"detail": ...} 이 나가 계약 봉투가 깨지고, 호출자는 "임베딩이 죽었다"를
        # 구분할 수 없어 ① 키워드 전용 강등 판단도 못 한다. 원인은 로그로 남긴다.
        logger.exception("임베딩 생성 실패")
        return responses.error(503, "upstream_unavailable")

    return responses.success(
        "embed_success", {"vectors": vectors, "dim": dim, "model": model}
    )
