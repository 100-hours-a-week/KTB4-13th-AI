"""⑥ 취향 프로필 생성: POST /preferences/profile

명세: docs/wiki/ai/1-model-api/spec.md ⑥ POST /preferences/profile
LLM을 쓰지 않는다. 지금은 요청 검사와 응답 모양까지만 있고, 프로필이 없는 상태
(cold_start)로 답한다. 이력 읽기·취향 벡터 계산·저장은 후속 이슈에서 붙인다(#89).
"""

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.core import responses
from app.profile.schemas import ProfileRequest

router = APIRouter(prefix="/preferences", tags=["preferences"])


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
    try:
        payload = json.loads(await request.body())
    except ValueError:
        # JSON 문법 오류와, UTF-8 이 아닌 본문(UnicodeDecodeError) 둘 다 ValueError 다.
        return responses.error(400, "invalid_request")

    if parse_request(payload) is None:
        return responses.error(400, "invalid_request")

    # 계산·저장이 붙기 전이라 저장된 프로필이 없다. 0 은 "프로필 없음"이라 첫 실제 판(1)과 겹치지 않는다.
    return responses.success(
        "profile_success", {"cold_start": True, "profile_version": 0}
    )
