"""나의 도서관 사진으로 책 찾기: POST /shelf/scans (#615)

계약: AI #615, BE #509. BE 가 S3 에 올라간 사진 주소를 보내면, 사진 속 책(표지 한 권 또는 책장 여러 권)을
찾아 책마다 찾음·못 찾음으로 돌려준다. 사진도 결과도 저장하지 않는다. 사용자가 고른 책을 담는 것은 BE 다.

책을 하나도 못 찾은 것은 오류가 아니라 200 에 빈 items 다.
"""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core import body, responses
from app.gateway.llm import LLMTimeoutError, LLMUnavailableError
from app.photo import fetch, scan
from app.photo.schemas import parse_request, to_data

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shelf", tags=["shelf"])

# 칸이 두 개(사용자 번호·사진 주소)뿐이라 정상 요청은 몇 KB 다. 상한 없이 통째로 읽지 않게 넉넉히 잡는다(#99)
MAX_BODY_BYTES = 16 * 1024

# 사진을 쓸 수 없는 까닭 → (상태, message). 계약의 오류표(#615)
_IMAGE_ERRORS = {
    "blocked": (400, "invalid_request"),
    "unreachable": (404, "image_not_found"),
    "too_large": (413, "payload_too_large"),
    "unsupported": (422, "image_unreadable"),
}


@router.post("/scans")
async def create_scan(request: Request) -> JSONResponse:
    raw = await body.read_limited(request, MAX_BODY_BYTES)
    if raw is None:
        return responses.error(413, "payload_too_large")

    try:
        payload = json.loads(raw)
    except ValueError:
        # JSON 문법 오류와, UTF-8 이 아닌 본문(UnicodeDecodeError) 둘 다 ValueError 다
        return responses.error(400, "invalid_request")

    req = parse_request(payload)
    if req is None:
        return responses.error(400, "invalid_request")

    try:
        result = await scan.scan(req.image_ref)
    except fetch.ImageError as exc:
        # 사진 주소에는 서명이 붙어 있어 로그에 남기지 않는다. 까닭만 남긴다
        logger.warning("사진 책 찾기: 사진을 쓸 수 없음 (%s)", exc.reason)
        status, message = _IMAGE_ERRORS[exc.reason]
        return responses.error(status, message)
    except scan.ScanTimeoutError:
        logger.warning("사진 책 찾기: 제한 시간 초과")
        return responses.error(504, "generation_timeout")
    except LLMUnavailableError as exc:
        # 예외 메시지에는 공급자의 오류 문구나 모델의 답이 실려 올 수 있어 종류만 남긴다
        logger.warning("사진 책 찾기: LLM 장애 (%s)", type(exc).__name__)
        if isinstance(exc, LLMTimeoutError):
            return responses.error(504, "generation_timeout")
        return responses.error(503, "upstream_unavailable")
    except Exception:
        # 그대로 두면 FastAPI 기본 500 {"detail": ...} 이 나가 공통 응답 형식이 깨진다
        logger.exception("사진 책 찾기 실패")
        return responses.error(500, "internal_server_error")

    return responses.success("shelf_scan_success", to_data(result))
