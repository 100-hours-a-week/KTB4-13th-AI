"""테스트 공통 설정.

`Settings` 는 DATABASE_URL 을 필수로 요구한다(값이 없으면 기동 거부).
개발자 로컬에는 `.env` 가 있어 통과하지만 CI 체크아웃에는 없어서,
`app.main` 을 import 하는 순간 ValidationError 로 테스트가 무더기로 깨진다.
DB 를 실제로 쓰지 않는 테스트까지 환경 파일에 의존하지 않도록 여기서 채운다.
"""

import json
import os

import asyncpg
import pytest

# import 시점에 넣는다 — 테스트 모듈이 app.main 을 import 하면서
# 곧바로 Settings() 를 만들기 때문에 fixture 로는 늦다.
os.environ.setdefault("DATABASE_URL", "postgresql://test@127.0.0.1:5432/test")

# 이 import 는 위 환경변수 설정 뒤여야 한다 — 순서를 바꾸면 Settings 가 실패한다.
from app.core import db
from app.core.auth import verify_service_token
from app.core.config import get_settings
from app.main import app as _app


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """get_settings 는 lru_cache 라 테스트가 환경변수를 바꿔도 값이 굳는다."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _bypass_service_token():
    """라우터 테스트마다 Authorization 헤더를 챙기지 않아도 되게 기본은 통과시킨다.

    인증 자체를 검증하는 tests/test_service_token_auth.py 는 이 오버라이드를
    직접 지우고 실제 검사 경로를 확인한다.
    """
    _app.dependency_overrides[verify_service_token] = lambda: None
    yield
    _app.dependency_overrides.pop(verify_service_token, None)


class _IdempotencyConn:
    """idempotency_records 표를 메모리 딕셔너리로 흉내 내는 연결. app.core.idempotency 의 질의 두 개만 받는다."""

    def __init__(self, rows: dict) -> None:
        self.rows = rows

    async def fetchrow(self, query: str, key: str):
        row = self.rows.get(key)
        if row is None:
            return None
        body_hash, response = row
        return {
            "body_hash": body_hash,
            "stored_response": json.dumps(response, ensure_ascii=False),
        }

    async def execute(self, query: str, key: str, body_hash: str, response: str):
        if key in self.rows:
            raise asyncpg.UniqueViolationError("duplicate key")
        self.rows[key] = (body_hash, json.loads(response))


class _IdempotencyAcquire:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    async def __aenter__(self) -> _IdempotencyConn:
        return _IdempotencyConn(self.rows)

    async def __aexit__(self, *exc) -> bool:
        return False


class _IdempotencyPool:
    def __init__(self, rows: dict) -> None:
        self.rows = rows

    def acquire(self) -> _IdempotencyAcquire:
        return _IdempotencyAcquire(self.rows)


@pytest.fixture
def idempotency_rows(monkeypatch) -> dict:
    """DB 없이 멱등 기록을 메모리에 둔다. {키: (본문 해시, 저장한 응답)}. 테스트가 직접 들여다볼 수 있다."""
    rows: dict = {}
    monkeypatch.setattr(db, "get_pool", lambda: _IdempotencyPool(rows))
    return rows
