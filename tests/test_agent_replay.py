"""⑦ 멱등 기록(app/agent/replay.py) 테스트(#376). DB 대신 메모리 기록소를 쓴다(conftest 의 idempotency_rows).

같은 키·같은 본문은 저장한 응답을, 같은 키·다른 본문은 충돌을 돌려준다. 기록소가 고장 나도 요청을 막지 않는다.
"""

import asyncio

import pytest

from app.agent import replay
from app.core import db
from app.core.idempotency import IdempotencyConflict

KEY = replay.key_of("idem_1")
RESPONSE = {"message": "agent_success", "data": {"reply": "담았어요."}}


def _run(coro):
    return asyncio.run(coro)


def test_키_앞에_API_이름을_붙여_다른_API와_겹치지_않는다() -> None:
    assert replay.key_of("k1") == "agent:k1"
    assert replay.key_of("k1") != "profile:k1"


def test_처음_온_키는_저장한_응답이_없다(idempotency_rows) -> None:
    assert _run(replay.find(KEY, "h1")) is None


def test_저장한_뒤_같은_본문으로_찾으면_처음_응답이_나온다(idempotency_rows) -> None:
    assert _run(replay.save(KEY, "h1", RESPONSE)) is None

    assert _run(replay.find(KEY, "h1")) == RESPONSE


def test_같은_키에_다른_본문이면_충돌이다(idempotency_rows) -> None:
    _run(replay.save(KEY, "h1", RESPONSE))

    with pytest.raises(IdempotencyConflict):
        _run(replay.find(KEY, "h2"))


def test_다른_키는_서로_영향을_주지_않는다(idempotency_rows) -> None:
    _run(replay.save(KEY, "h1", RESPONSE))

    assert _run(replay.find(replay.key_of("idem_2"), "h1")) is None


def test_저장이_겹치면_먼저_저장된_응답을_돌려준다(idempotency_rows) -> None:
    # 같은 요청이 동시에 처리되어 다른 쪽이 먼저 저장했다
    first = {"message": "agent_success", "data": {"reply": "처음 응답"}}
    idempotency_rows[KEY] = ("h1", first)

    assert _run(replay.save(KEY, "h1", RESPONSE)) == first
    assert idempotency_rows[KEY] == ("h1", first)  # 덮어쓰지 않는다


def test_저장이_겹쳤는데_본문이_다르면_충돌이다(idempotency_rows) -> None:
    idempotency_rows[KEY] = ("h1", RESPONSE)

    with pytest.raises(IdempotencyConflict):
        _run(replay.save(KEY, "h2", RESPONSE))


def _broken_pool(monkeypatch) -> None:
    def boom():
        raise RuntimeError("DB 풀이 없습니다")

    monkeypatch.setattr(db, "get_pool", boom)


def test_기록소를_못_읽어도_요청을_막지_않는다(monkeypatch, caplog) -> None:
    _broken_pool(monkeypatch)

    with caplog.at_level("WARNING"):
        assert _run(replay.find(KEY, "h1")) is None

    assert "RuntimeError" in caplog.text


def test_기록소에_못_써도_응답은_그대로_보낸다(monkeypatch, caplog) -> None:
    _broken_pool(monkeypatch)

    with caplog.at_level("WARNING"):
        assert _run(replay.save(KEY, "h1", RESPONSE)) is None

    assert "RuntimeError" in caplog.text


def test_기록소_장애_로그에_응답_내용이_남지_않는다(monkeypatch, caplog) -> None:
    _broken_pool(monkeypatch)

    with caplog.at_level("WARNING"):
        _run(replay.save(KEY, "h1", RESPONSE))

    assert "담았어요" not in caplog.text
