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


def test_처리_못한_예외도_X_Request_Id를_응답과_로그에_남긴다(
    monkeypatch, caplog
) -> None:
    """리뷰 지적 — main.py의 @app.exception_handler(Exception)(#201)은 이 미들웨어

    "바깥"(Starlette ServerErrorMiddleware)에서 돌아서, 거기로 넘기면 응답에
    X-Request-Id가 안 붙고 그 오류 로그의 request_id도 이미 reset된 뒤라 null로
    남는다. 미들웨어가 예외를 직접 잡아야 둘 다 제대로 남는다.
    """

    async def _boom() -> bool:
        raise RuntimeError("DB 장애")

    monkeypatch.setattr("app.main.db.check_database", _boom)

    with caplog.at_level("ERROR"):
        res = client.get("/health", headers={"X-Request-Id": "req_error_probe"})

    assert res.status_code == 500
    assert res.headers["X-Request-Id"] == "req_error_probe"
    assert res.json() == {"message": "internal_server_error", "data": None}
    assert "처리하지 못한 예외" in caplog.text


def test_X_Request_Id가_너무_길면_새로_만든다() -> None:
    """리뷰 지적 — 길이 제한 없이 그대로 쓰면 긴 값을 보내는 호출자가 로그를 부풀릴 수 있다."""
    too_long = "x" * (request_log.MAX_REQUEST_ID_LEN + 1)

    res = client.get("/health", headers={"X-Request-Id": too_long})

    assert res.headers["X-Request-Id"] != too_long
    assert len(res.headers["X-Request-Id"]) <= request_log.MAX_REQUEST_ID_LEN


def test_X_Request_Id가_상한_이내면_그대로_쓴다() -> None:
    exactly_max = "x" * request_log.MAX_REQUEST_ID_LEN

    res = client.get("/health", headers={"X-Request-Id": exactly_max})

    assert res.headers["X-Request-Id"] == exactly_max
