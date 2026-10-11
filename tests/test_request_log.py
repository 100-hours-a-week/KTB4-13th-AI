"""app/core/request_log.py 단위 테스트 — request_id 전파와 JSON 로그 포맷(#202)."""

import json
import logging

from app.core import request_log


def test_new_request_id는_매번_다르다() -> None:
    assert request_log.new_request_id() != request_log.new_request_id()


def test_요청_컨텍스트가_없으면_None이다() -> None:
    assert request_log.current_request_id() is None


def test_set_하면_current가_그_값을_돌려주고_reset하면_원래대로다() -> None:
    assert request_log.current_request_id() is None
    token = request_log.set_request_id("req-abc")
    try:
        assert request_log.current_request_id() == "req-abc"
    finally:
        request_log.reset_request_id(token)
    assert request_log.current_request_id() is None


def test_JsonFormatter는_유효한_JSON_한_줄을_낸다() -> None:
    formatter = request_log._JsonFormatter()
    record = logging.LogRecord(
        name="app.routers.chat",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="카드 생성 실패",
        args=(),
        exc_info=None,
    )

    line = formatter.format(record)
    payload = json.loads(line)  # 유효한 JSON이 아니면 여기서 예외

    assert payload["level"] == "ERROR"
    assert payload["logger"] == "app.routers.chat"
    assert payload["message"] == "카드 생성 실패"
    assert "time" in payload


def test_JsonFormatter는_현재_request_id를_같이_싣는다() -> None:
    formatter = request_log._JsonFormatter()
    record = logging.LogRecord(
        name="app.routers.chat",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="처리 완료",
        args=(),
        exc_info=None,
    )

    token = request_log.set_request_id("req-xyz")
    try:
        payload = json.loads(formatter.format(record))
    finally:
        request_log.reset_request_id(token)

    assert payload["request_id"] == "req-xyz"


def test_configure는_루트_로거_레벨을_LOG_LEVEL로_맞춘다() -> None:
    root = logging.getLogger()
    original_level, original_handlers = root.level, root.handlers
    try:
        request_log.configure("WARNING")
        assert root.level == logging.WARNING
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, request_log._JsonFormatter)
    finally:
        root.setLevel(original_level)
        root.handlers = original_handlers


# ---------------------------------------------------------------------------
# 접속 로그에서 주소의 ? 뒤를 지운다(#333)
# ---------------------------------------------------------------------------


def _access_record(path: str) -> logging.LogRecord:
    """uvicorn 이 접속 로그를 남길 때 만드는 기록과 같은 모양."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("10.0.0.1:51234", "GET", path, "1.1", 200),
        exc_info=None,
    )


def _logged(path: str) -> str:
    record = _access_record(path)
    assert request_log.drop_query_string(record) is True  # 기록 자체는 남긴다
    return record.getMessage()


def test_접속_로그에_주소의_물음표_뒤를_남기지_않는다() -> None:
    # 홈 추천은 GET 이라 사용자 번호와 산 책의 ISBN 이 주소에 실린다.
    line = _logged(
        "/recommendations/feed?user_id=123&surface=home"
        "&purchased_isbns=9788936434120,9788954651135"
    )

    assert line == '10.0.0.1:51234 - "GET /recommendations/feed HTTP/1.1" 200'
    assert "123" not in line.split('"')[1]
    assert "9788936434120" not in line


def test_물음표가_없는_주소는_그대로_남긴다() -> None:
    assert _logged("/health") == '10.0.0.1:51234 - "GET /health HTTP/1.1" 200'


def test_모양이_다른_기록은_건드리지_않는다() -> None:
    # uvicorn 이 형식을 바꿔도 로그를 남기다 죽으면 안 된다. 모르는 모양이면 그대로 둔다.
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="다른 형식 %s",
        args=("값?뒤",),
        exc_info=None,
    )

    assert request_log.drop_query_string(record) is True
    assert record.getMessage() == "다른 형식 값?뒤"


def test_configure가_접속_로그에_이_처리를_건다() -> None:
    access = logging.getLogger("uvicorn.access")
    root = logging.getLogger()
    before = (list(access.filters), list(root.handlers), root.level)
    try:
        request_log.configure("INFO")
        request_log.configure("INFO")  # 두 번 불러도 한 번만 걸린다

        assert access.filters.count(request_log.drop_query_string) == 1
    finally:
        access.filters, root.handlers = before[0], before[1]
        root.setLevel(before[2])
