"""BE→AI 서비스 토큰 인증.

명세: ⑧ /health 를 제외한 모든 엔드포인트는 Authorization: Bearer <토큰> 을
요구하고, 없거나 틀리면 401 이다. 토큰은 발급받는 게 아니라 두 서버가
환경변수(AI_SERVICE_TOKEN)로 똑같이 들고 있는 비밀 문자열 하나다.

임시로 AI 서버에 AI_SERVICE_TOKEN 을 비워 두면 검사하지 않는다(#261). AI 서버는 내부망에서
BE 만 부를 수 있어, V1 에서 토큰을 켤지는 설정 값으로 정한다. BE 는 헤더를 그대로 붙여도 된다.
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
    # 토큰을 설정하지 않았으면 검사하지 않는다. 헤더가 없거나 무엇이 와도 통과다.
    if not expected:
        return
    provided = credentials.credentials if credentials else ""
    # 길이·내용에 따라 걸리는 시간이 달라지지 않게 비교한다(타이밍 공격).
    if not hmac.compare_digest(provided.encode(), expected.encode()):
        raise Unauthorized()
