"""테스트 공통 설정.

`Settings` 는 DATABASE_URL 을 필수로 요구한다(값이 없으면 기동 거부).
개발자 로컬에는 `.env` 가 있어 통과하지만 CI 체크아웃에는 없어서,
`app.main` 을 import 하는 순간 ValidationError 로 테스트가 무더기로 깨진다.
DB 를 실제로 쓰지 않는 테스트까지 환경 파일에 의존하지 않도록 여기서 채운다.
"""

import os

import pytest

# import 시점에 넣는다 — 테스트 모듈이 app.main 을 import 하면서
# 곧바로 Settings() 를 만들기 때문에 fixture 로는 늦다.
os.environ.setdefault("DATABASE_URL", "postgresql://test@127.0.0.1:5432/test")

# 이 import 는 위 환경변수 설정 뒤여야 한다 — 순서를 바꾸면 Settings 가 실패한다.
from app.core.config import get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_settings 는 lru_cache 라 테스트가 환경변수를 바꿔도 값이 굳는다."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
