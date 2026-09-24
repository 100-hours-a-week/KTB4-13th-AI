"""⑧ GET /health 의 구성요소 판정 테스트.

DB 와 모델을 모두 가짜로 바꿔 끼워 조합별 상태만 본다.
"""

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app as fastapi_app

client = TestClient(fastapi_app)


@pytest.fixture
def db_up(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok() -> bool:
        return True

    monkeypatch.setattr(main.db, "check_database", _ok)
    monkeypatch.setattr(main.db, "check_vector_index", _ok)


@pytest.fixture
def db_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail() -> bool:
        return False

    monkeypatch.setattr(main.db, "check_database", _fail)
    monkeypatch.setattr(main.db, "check_vector_index", _fail)


def _set_model_loaded(monkeypatch: pytest.MonkeyPatch, loaded: bool) -> None:
    monkeypatch.setattr(main.embedding, "is_loaded", lambda: loaded)


def _set_llm_status(monkeypatch: pytest.MonkeyPatch, status: str) -> None:
    # 실시간 점검이 아니라 ③이 최근에 관측한 값을 흉내 낸다(app/gateway/llm.py).
    monkeypatch.setattr(main.llm, "last_known_status", lambda: status)


def test_모델이_올라와_있으면_embedding이_ok다(
    db_up: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_model_loaded(monkeypatch, True)

    body = client.get("/health").json()

    assert body["components"]["embedding"] == "ok"


def test_모델이_없으면_embedding이_unavailable이다(
    db_up: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_model_loaded(monkeypatch, False)

    body = client.get("/health").json()

    assert body["components"]["embedding"] == "unavailable"


def test_모델이_없어도_200_degraded로_응답한다(
    db_up: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 임베딩이 죽어도 ① 은 키워드 전용으로 응답하므로 트래픽에서 빼지 않는다.
    _set_model_loaded(monkeypatch, False)

    res = client.get("/health")

    assert res.status_code == 200
    assert res.json()["status"] == "degraded"


def test_DB가_죽으면_모델이_있어도_down_503이다(
    db_down: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_model_loaded(monkeypatch, True)

    res = client.get("/health")

    assert res.status_code == 503
    assert res.json()["status"] == "down"


def test_llm이_최근_정상이면_llm이_ok다(
    db_up: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_model_loaded(monkeypatch, True)
    _set_llm_status(monkeypatch, "ok")

    body = client.get("/health").json()

    assert body["components"]["llm"] == "ok"


def test_llm이_최근_실패했으면_llm이_unavailable이다(
    db_up: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_model_loaded(monkeypatch, True)
    _set_llm_status(monkeypatch, "unavailable")

    body = client.get("/health").json()

    assert body["components"]["llm"] == "unavailable"


def test_모든_구성요소가_정상이면_status가_ok다(
    db_up: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # llm이 하드코딩된 unavailable이던 동안은 status가 ok로 나올 방법이
    # 없었다 — 그 회귀를 막는 테스트.
    _set_model_loaded(monkeypatch, True)
    _set_llm_status(monkeypatch, "ok")

    res = client.get("/health")

    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_llm이_죽어도_200_degraded로_응답한다(
    db_up: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # llm이 죽어도 ③이 degraded 200으로 흡수하므로 down이 아니라 degraded다.
    _set_model_loaded(monkeypatch, True)
    _set_llm_status(monkeypatch, "unavailable")

    res = client.get("/health")

    assert res.status_code == 200
    assert res.json()["status"] == "degraded"
