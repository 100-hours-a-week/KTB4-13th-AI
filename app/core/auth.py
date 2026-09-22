"""BE→AI 서비스 토큰 인증.

명세: ⑧ /health 를 제외한 모든 엔드포인트는 Authorization: Bearer <토큰> 을
요구하고, 없거나 틀리면 401 이다. 토큰은 발급받는 게 아니라 두 서버가
환경변수(AI_SERVICE_TOKEN)로 똑같이 들고 있는 비밀 문자열 하나다.
"""

import hmac

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

_bearer_scheme = HTTPBearer(auto_error=False)


class Unauthorized(Exception):
    """서비스 토큰이 없거나 설정값과 다르다."""


def verify_service_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    expected = get_settings().ai_service_token
    provided = credentials.credentials if credentials else ""
    # expected 가 비어있으면(환경변수 설정 누락) 무조건 거부한다.
    # 그냥 == 비교로 두면 빈 문자열끼리 우연히 같아져 인증이 조용히
    # 무력화될 수 있고, 타이밍 공격에도 노출된다.
    if not expected or not hmac.compare_digest(provided, expected):
        raise Unauthorized()
