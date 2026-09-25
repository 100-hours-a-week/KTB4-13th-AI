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
