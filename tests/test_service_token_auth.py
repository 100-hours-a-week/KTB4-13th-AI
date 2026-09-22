"""BE→AI 서비스 토큰 인증 테스트 (#27).

conftest.py 의 _bypass_service_token 이 기본적으로 검사를 통과시켜 두므로,
여기서는 그 오버라이드를 직접 지우고 실제 검사 경로를 확인한다.
"""

import pytest
from fastapi.testclient import TestClient

from app.core.auth import verify_service_token
from app.core.config import get_settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _real_auth_check(monkeypatch: pytest.MonkeyPatch) -> None:
    app.dependency_overrides.pop(verify_service_token, None)
    monkeypatch.setenv("AI_SERVICE_TOKEN", "secret-token")
    get_settings.cache_clear()


def test_헤더가_없으면_401이다() -> None:
    res = client.post("/search", json={"query": "아무 검색어"})

    assert res.status_code == 401
    assert res.json() == {"message": "unauthorized", "data": None}


def test_토큰이_틀리면_401이다() -> None:
    res = client.post(
        "/search",
        json={"query": "아무 검색어"},
        headers={"Authorization": "Bearer wrong-token"},
    )

    assert res.status_code == 401
    assert res.json() == {"message": "unauthorized", "data": None}


def test_토큰이_맞으면_통과한다() -> None:
    res = client.post(
        "/search",
        json={"query": "아무 검색어"},
        headers={"Authorization": "Bearer secret-token"},
    )

    assert res.status_code != 401


def test_설정에_토큰이_비어있으면_뭘_보내도_401이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AI_SERVICE_TOKEN", "")
    get_settings.cache_clear()

    res = client.post(
        "/search",
        json={"query": "아무 검색어"},
        headers={"Authorization": "Bearer "},
    )

    assert res.status_code == 401


def test_토큰에_비ASCII_문자가_있어도_500이_아니라_401이다() -> None:
    # dict 헤더는 httpx가 ascii로 인코딩을 강제해 버그를 재현하지 못하므로,
    # 실제 요청처럼 raw bytes 헤더로 보낸다.
    res = client.post(
        "/search",
        json={"query": "아무 검색어"},
        headers=[(b"authorization", "Bearer 한글토큰".encode())],
    )

    assert res.status_code == 401
    assert res.json() == {"message": "unauthorized", "data": None}


def test_health는_인증_없이도_통과한다() -> None:
    res = client.get("/health")

    assert res.status_code in (200, 503)
