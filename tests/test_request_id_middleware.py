"""X-Request-Id 미들웨어 테스트(#202) — /health로 확인한다(인증이 필요 없어 단순하다)."""

from fastapi.testclient import TestClient

from app.core import request_log
from app.main import app

client = TestClient(app)


def test_요청에_없으면_서버가_만들어_응답_헤더로_돌려준다() -> None:
    res = client.get("/health")

    request_id = res.headers.get("X-Request-Id")
    assert request_id
    assert len(request_id) > 0


def test_요청에_있으면_그대로_돌려준다() -> None:
    res = client.get("/health", headers={"X-Request-Id": "req_20260907_0001"})

    assert res.headers["X-Request-Id"] == "req_20260907_0001"


def test_요청마다_안_주면_매번_다른_값이다() -> None:
    first = client.get("/health").headers["X-Request-Id"]
    second = client.get("/health").headers["X-Request-Id"]

    assert first != second


def test_요청_처리_중에는_현재_request_id로_조회된다(monkeypatch) -> None:
    """다른 모듈이 request_log.current_request_id()를 불러도 이 요청의 값이 나오는지 —

    로그 포맷터가 이 값을 읽는 방식과 같다.
    """
    seen: list[str | None] = []

    async def _spy_check_database() -> bool:
        seen.append(request_log.current_request_id())
        return True

    monkeypatch.setattr("app.main.db.check_database", _spy_check_database)

    res = client.get("/health", headers={"X-Request-Id": "req_probe"})

    assert seen == ["req_probe"]
    assert res.headers["X-Request-Id"] == "req_probe"


def test_요청이_끝나면_request_id_컨텍스트가_안_남는다() -> None:
    client.get("/health", headers={"X-Request-Id": "req_should_not_leak"})

    assert request_log.current_request_id() is None
