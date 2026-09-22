"""목록 API(① 검색, ④ 피드)의 페이지 커서.

서버는 커서를 저장하지 않는다. 내용에 서명을 붙여 돌려주고, 다음 요청에 돌아오면 서명으로
우리가 만든 것인지 확인한다. 클라이언트에게는 뜻을 알 필요 없는 글자일 뿐이다.

모양: base64url(JSON) + "." + base64url(HMAC-SHA256 서명)
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.core.config import get_settings

# 명세: 30분 지나면 만료
TTL_SECONDS = 30 * 60


class CursorError(Exception):
    """커서를 쓸 수 없다 — 모양이 깨졌거나, 서명이 안 맞거나, 만료됐다."""


def _key() -> bytes:
    key = get_settings().cursor_signing_key
    if not key:
        # 빈 키로 서명하면 누구나 커서를 위조할 수 있다. 조용히 넘어가지 않고 크게 실패시킨다.
        raise RuntimeError(
            "CURSOR_SIGNING_KEY 가 비어 있습니다. .env 에 값을 넣어야 합니다."
        )
    return key.encode()


def check_key() -> None:
    """서버가 뜰 때 부른다. 키가 비어 있으면 요청을 받기 전에 멈춘다.

    요청 중에야 알면 다음 페이지가 있는 검색마다 500 이 난다. ① 검색 함수를 부르는 ③ 챗봇도
    커서를 안 쓰는데 같이 깨진다. DATABASE_URL 처럼 없으면 아예 안 뜨는 쪽이 알아채기 쉽다.
    """
    _key()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())


def encode(data: dict[str, Any], now: float | None = None) -> str:
    """data 에 만든 시각을 붙여 서명한 커서를 만든다."""
    issued_at = int(time.time() if now is None else now)
    payload = json.dumps(
        {"d": data, "t": issued_at}, separators=(",", ":"), sort_keys=True
    )
    body = _b64(payload.encode())
    return f"{body}.{_sign(body)}"


def decode(token: str, now: float | None = None) -> dict[str, Any]:
    """서명과 만료를 확인하고 data 를 돌려준다. 못 쓰는 커서면 CursorError."""
    body, sep, signature = token.partition(".")
    if not sep:
        raise CursorError("모양이 다르다")
    # 일반 비교(==)는 앞 글자부터 맞춰 보다 틀린 자리에서 멈춰, 걸린 시간으로 서명을 한 글자씩 알아낼 수 있다.
    if not hmac.compare_digest(signature, _sign(body)):
        raise CursorError("서명이 맞지 않는다")
    try:
        payload = json.loads(_unb64(body))
        data, issued_at = payload["d"], payload["t"]
    except (ValueError, KeyError, TypeError) as exc:
        raise CursorError("내용을 읽을 수 없다") from exc
    current = time.time() if now is None else now
    if current - issued_at > TTL_SECONDS:
        raise CursorError("만료됐다")
    return data
