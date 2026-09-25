"""⑥ 취향 프로필 생성: POST /preferences/profile

명세: docs/wiki/ai/1-model-api/spec.md ⑥ POST /preferences/profile
LLM을 쓰지 않는다. 이력과 요청으로 취향 프로필을 다시 계산해 taste_profile 에 저장한다.
같은 멱등 키·같은 본문이면 저장한 응답을, 같은 키·다른 본문이면 409 를 돌려준다.
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core import body, idempotency, responses
from app.profile import service
from app.profile.schemas import ProfileRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/preferences", tags=["preferences"])

# 명세 에러표는 숫자 없이 "상한 초과"라고만 적혀 있다(#99) — memories 최대 500개,
# 각각 embedding_dim(384) 벡터가 있어 이론상 최대치가 4MB 안팎이다. 그 두 배쯤
# 여유를 둔다. 정확한 값은 명세 담당과 확인 필요.
MAX_BODY_BYTES = 8 * 1024 * 1024


def parse_request(payload: Any) -> ProfileRequest | None:
    """계약에 맞으면 요청 객체, 아니면 None 을 돌려준다."""
    if not isinstance(payload, dict):
        return None
    try:
        return ProfileRequest.model_validate(payload)
    except ValidationError:
        return None


@router.post("/profile")
async def profile(request: Request) -> JSONResponse:
    raw = await body.read_limited(request, MAX_BODY_BYTES)
    if raw is None:
        return responses.error(413, "payload_too_large")

    try:
        payload = json.loads(raw)
    except ValueError:
        # JSON 문법 오류와, UTF-8 이 아닌 본문(UnicodeDecodeError) 둘 다 ValueError 다.
        return responses.error(400, "invalid_request")

    req = parse_request(payload)
    if req is None:
        return responses.error(400, "invalid_request")

    try:
        response = await service.rebuild(req, idempotency.body_hash(payload))
    except idempotency.IdempotencyConflict:
        # 재시도가 아니라 다른 요청이다. 호출자는 재시도를 멈춰야 한다(명세 ⑥).
        return responses.error(409, "idempotency_conflict")
    except Exception:
        # 그대로 두면 FastAPI 기본 500 {"detail": ...} 이 나가 공통 응답 형식이 깨진다.
        logger.exception("취향 프로필 생성 실패")
        return responses.error(500, "internal_server_error")

    return responses.success(response["message"], response["data"])
