"""공통 로그 설정 — JSON 한 줄, X-Request-Id로 BE 로그와 잇는다(#202).

인프라 설계(docs/wiki/ai/7-infra-monitoring/design.md): "로그는 JSON 한 줄로
남긴다... X-Request-Id는 BE가 붙여 보내고, 없으면 AI가 만들어 응답 헤더로
돌려준다. BE 로그와 AI 로그를 이 키로 잇는다."

대화 원문·검색어·이미지를 로그 메시지에 안 담는 건 각 호출부(app/gateway/llm.py 등)의
책임이다 — 이 파일은 형식(JSON, request_id)만 맞춘다.
"""

import contextvars
import json
import logging
import uuid
from datetime import UTC, datetime

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

# 받은 X-Request-Id를 길이 제한 없이 그대로 로그·응답 헤더에 쓰면, 아주 긴 값을
# 보내는 호출자 하나가 로그를 부풀릴 수 있다(리뷰 지적, #202). 다른 필드들의
# 상한(MAX_QUERY_CHARS 등)과 맞춰 200자로 둔다 — 이 값을 넘기면 BE가 보낸 값이
# 아니라고 보고 새로 만든다.
MAX_REQUEST_ID_LEN = 200


def new_request_id() -> str:
    return uuid.uuid4().hex


def incoming_request_id(header_value: str | None) -> str:
    """요청 헤더의 X-Request-Id를 쓸지, 새로 만들지 정한다."""
    if header_value and len(header_value) <= MAX_REQUEST_ID_LEN:
        return header_value
    return new_request_id()


def set_request_id(request_id: str) -> contextvars.Token:
    """미들웨어가 요청 시작 때 부른다. 반환값은 reset_request_id에 그대로 넘긴다."""
    return _request_id.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    """미들웨어가 요청이 끝난 뒤(성공이든 예외든) 부른다 — 다음 요청에 값이 안 새게 한다."""
    _request_id.reset(token)


def current_request_id() -> str | None:
    """요청 처리 중이 아니면(예: 기동 로그, 배치 작업) None."""
    return _request_id.get()


class _JsonFormatter(logging.Formatter):
    """로그 한 줄을 JSON 객체로 낸다."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": current_request_id(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure(level: str) -> None:
    """서버가 뜰 때 한 번 부른다.

    전에는 아무것도 안 불러서 LOG_LEVEL 설정값이 그냥 무시되고(파이썬 로깅 기본
    레벨은 WARNING이라) INFO 로그가 하나도 안 나갔다.
    """
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
