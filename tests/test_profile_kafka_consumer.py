"""카프카로 오는 프로필 계산 요청의 소비자(#669). 브로커 없이 가짜 소비자·발행자로 돈다.

지키려는 것(BE #551 README 「AI 추천 프로필 Kafka 직접 전달」)
- 저장(계산)이 끝난 뒤에만 offset 을 넘긴다
- 틀린 메시지는 다시 하지 않고 DLT 로 보낸다. 계산이 일시 실패하면 간격을 늘려 다시 하고, 다 실패하면 DLT 로 보낸다
- DLT 발행이 실패하면 원본을 넘기지 않는다(성공할 때까지 다시 한다)
- 기본은 꺼져 있다. 켰는데 브로커·그룹이 비어 있으면 띄우지 않고, 브로커에 못 붙어도 서버는 뜬다
"""

import asyncio
import logging
import re
from dataclasses import dataclass, field

import pytest
from aiokafka import ConsumerRebalanceListener
from aiokafka.structs import TopicPartition

from app.core.config import Settings
from app.profile import kafka_consumer
from app.profile.kafka_consumer import ProfileConsumer, handle
from tests.test_profile_kafka_message import _v2, _v2_delete, _value

MAX_BYTES = 8 * 1024 * 1024
TOPIC = "book.ai-profile.v1"


@dataclass
class Record:
    offset: int
    key: bytes | None = b"42"
    value: bytes | None = field(default_factory=_value)
    topic: str = TOPIC
    partition: int = 0
    # BE 가 보낸 시각(밀리초). 카프카가 메시지에 붙여 준다
    timestamp: int | None = None


class Calls:
    """가짜 계산·DLT 발행·잠자기가 불린 차례를 적는다. fail_* 만큼 먼저 실패한다."""

    def __init__(
        self,
        *,
        fail_rebuild: int = 0,
        fail_dlt: int = 0,
        fail_delete: int = 0,
        had_row: bool = True,
    ) -> None:
        self.fail_rebuild = fail_rebuild
        self.fail_dlt = fail_dlt
        self.fail_delete = fail_delete
        self.had_row = had_row
        self.rebuilt: list[int] = []
        self.deleted: list[int] = []
        # 계산·삭제가 불린 차례(같은 사용자의 갱신 다음 삭제가 차례대로인지 본다)
        self.order: list[tuple[str, int]] = []
        self.dlt: list[tuple[int, str]] = []
        self.slept: list[float] = []

    async def rebuild(self, request) -> None:
        self.rebuilt.append(request.user_id)
        self.order.append(("UPSERT", request.user_id))
        if self.fail_rebuild > 0:
            self.fail_rebuild -= 1
            raise RuntimeError("DB 연결이 잠깐 끊겼다")

    async def delete(self, user_id: int) -> bool:
        self.deleted.append(user_id)
        self.order.append(("DELETE", user_id))
        if self.fail_delete > 0:
            self.fail_delete -= 1
            raise RuntimeError("DB 연결이 잠깐 끊겼다")
        return self.had_row

    async def send_dlt(self, record, reason: str) -> None:
        if self.fail_dlt > 0:
            self.fail_dlt -= 1
            raise RuntimeError("DLT 브로커가 잠깐 안 된다")
        self.dlt.append((record.offset, reason))

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)


def _handle(record: Record, calls: Calls) -> None:
    asyncio.run(
        handle(
            record,
            max_bytes=MAX_BYTES,
            rebuild=calls.rebuild,
            delete=calls.delete,
            send_dlt=calls.send_dlt,
            sleep=calls.sleep,
        )
    )


# --- 메시지 한 건 ---


def test_맞는_메시지는_계산하고_DLT로_보내지_않는다() -> None:
    calls = Calls()

    _handle(Record(offset=5), calls)

    assert calls.rebuilt == [42]
    assert calls.dlt == []
    assert calls.slept == []


@pytest.mark.parametrize(
    ("timestamp", "since"), [(1_000_000, "2.50초"), (None, "모름"), (-1, "모름")]
)
def test_계산하면_걸린_시간과_BE가_보낸_뒤_저장까지_걸린_시간을_남긴다(
    caplog: pytest.LogCaptureFixture, timestamp: int | None, since: str
) -> None:
    # 카프카(#666)로 바꾼 뒤 보낸 것이 프로필로 저장되기까지 걸린 시간을 견준다(#731). 시각이 없는 옛 형식(-1)은
    # 모른다고 남긴다
    caplog.set_level(logging.INFO, logger=kafka_consumer.__name__)
    calls = Calls()

    asyncio.run(
        handle(
            Record(offset=5, timestamp=timestamp),
            max_bytes=MAX_BYTES,
            rebuild=calls.rebuild,
            send_dlt=calls.send_dlt,
            sleep=calls.sleep,
            now=lambda: 1_002.5,
        )
    )

    [line] = [r.getMessage() for r in caplog.records if "계산했다" in r.getMessage()]
    assert re.fullmatch(
        rf"카프카로 온 프로필을 계산했다\(사용자 42\): 계산 \d+\.\d\d초, 보낸 뒤 {since}",
        line,
    )


@pytest.mark.parametrize(
    ("had_row", "said"), [(True, "지운 줄 있음"), (False, "지울 줄 없음")]
)
def test_DELETE는_계산하지_않고_지우고_지운_줄이_있었는지_남긴다(
    caplog: pytest.LogCaptureFixture, had_row: bool, said: str
) -> None:
    # 지운 줄이 있었다면 그 전에 프로필이 남아 있었다는 뜻이다. BE 가 다시 보내는 삭제가 무엇을 잡았는지 센다(#730)
    caplog.set_level(logging.INFO, logger=kafka_consumer.__name__)
    calls = Calls(had_row=had_row)

    _handle(Record(offset=5, value=_v2_delete()), calls)

    assert (calls.deleted, calls.rebuilt, calls.dlt) == ([42], [], [])
    [line] = [r.getMessage() for r in caplog.records if "처리했다" in r.getMessage()]
    assert (
        line == f"카프카로 온 프로필 삭제를 처리했다(사용자 42): {said}, 보낸 뒤 모름"
    )


def test_v2_UPSERT는_지우지_않고_계산한다() -> None:
    calls = Calls()

    _handle(Record(offset=5, value=_v2()), calls)

    assert (calls.rebuilt, calls.deleted, calls.dlt) == ([42], [], [])


def test_삭제가_잠깐_실패하면_다시_하고_끝까지_실패하면_DLT로_보낸다() -> None:
    once = Calls(fail_delete=1)
    _handle(Record(offset=5, value=_v2_delete()), once)
    assert (once.deleted, once.dlt) == ([42, 42], [])

    always = Calls(fail_delete=99)
    _handle(Record(offset=5, value=_v2_delete()), always)
    assert always.deleted == [42, 42, 42]
    assert always.dlt == [(5, "delete_failed")]


def test_같은_사용자의_갱신_다음_삭제를_차례로_처리하고_offset을_넘긴다() -> None:
    # 같은 키는 같은 파티션에 오고 하나씩 처리하므로, 탈퇴 전에 보낸 갱신이 삭제 뒤에 처리되지 않는다(#730)
    consumer = FakeConsumer(
        [Record(offset=7, value=_v2()), Record(offset=8, value=_v2_delete())]
    )
    calls = Calls()

    _run(consumer, FakeProducer(), calls)

    assert calls.order == [("UPSERT", 42), ("DELETE", 42)]
    tp = TopicPartition(TOPIC, 0)
    assert consumer.commits == [{tp: 8}, {tp: 9}]


def test_삭제를_넘기지_않으면_지금_삭제를_부른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = Calls()
    monkeypatch.setattr(kafka_consumer.service, "delete", calls.delete)
    consumer = FakeConsumer([Record(offset=7, value=_v2_delete())])
    worker = ProfileConsumer(
        _settings(),
        rebuild=calls.rebuild,
        clients=lambda: (consumer, FakeProducer()),
        sleep=calls.sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run())

    assert calls.deleted == [42]


def test_틀린_메시지는_계산하지_않고_까닭을_달아_DLT로_보낸다() -> None:
    calls = Calls()

    _handle(Record(offset=5, key=b"43"), calls)

    assert calls.rebuilt == []
    assert calls.dlt == [(5, "key_mismatch")]


def test_계산이_잠깐_실패하면_간격을_늘려_다시_한다() -> None:
    calls = Calls(fail_rebuild=2)

    _handle(Record(offset=5), calls)

    assert calls.rebuilt == [42, 42, 42]
    assert calls.slept == [1.0, 2.0]
    assert calls.dlt == []


def test_계산이_끝까지_실패하면_DLT로_보낸다() -> None:
    calls = Calls(fail_rebuild=99)

    _handle(Record(offset=5), calls)

    assert len(calls.rebuilt) == kafka_consumer.REBUILD_ATTEMPTS
    assert calls.dlt == [(5, "rebuild_failed")]


def test_DLT_발행이_실패하면_성공할_때까지_다시_한다() -> None:
    calls = Calls(fail_dlt=3)

    _handle(Record(offset=5, key=None), calls)

    assert calls.dlt == [(5, "no_key")]
    assert calls.slept == [1.0, 2.0, 4.0]


def test_다시_하는_간격은_상한을_넘지_않는다() -> None:
    calls = Calls(fail_dlt=12)

    _handle(Record(offset=5, key=None), calls)

    assert max(calls.slept) == kafka_consumer.RETRY_MAX_SECONDS


def test_서버가_내려가며_취소되면_DLT로_보내지_않고_그대로_멈춘다() -> None:
    calls = Calls()

    async def cancelled(request) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            handle(
                Record(offset=5),
                max_bytes=MAX_BYTES,
                rebuild=cancelled,
                send_dlt=calls.send_dlt,
                sleep=calls.sleep,
            )
        )
    assert calls.dlt == []


# --- 받는 순환 ---


class FakeConsumer:
    """records 를 차례로 주고, 다 주면 서버가 내려가는 것처럼 취소된다."""

    def __init__(self, records: list[Record], *, fail_start: int = 0) -> None:
        self.records = list(records)
        self.fail_start = fail_start
        self.commits: list[dict] = []
        self.started = 0
        self.stopped = 0
        self.topics: list[str] = []
        self.listener: ConsumerRebalanceListener | None = None

    def subscribe(self, topics, listener=None) -> None:
        self.topics = list(topics)
        self.listener = listener

    async def start(self) -> None:
        self.started += 1
        if self.fail_start > 0:
            self.fail_start -= 1
            raise ConnectionError("브로커에 못 붙었다")

    async def stop(self) -> None:
        self.stopped += 1

    async def getone(self) -> Record:
        if not self.records:
            raise asyncio.CancelledError
        return self.records.pop(0)

    async def commit(self, offsets: dict) -> None:
        self.commits.append(offsets)


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.stopped = 0

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        self.stopped += 1

    async def send_and_wait(self, topic, value=None, key=None, headers=None) -> None:
        self.sent.append(
            {"topic": topic, "value": value, "key": key, "headers": dict(headers)}
        )


def _settings(**overrides) -> Settings:
    values = {
        "profile_kafka_enabled": True,
        "kafka_bootstrap_servers": "localhost:9092",
        "profile_kafka_group_id": "book-ai-profile-v1-test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _run(consumer: FakeConsumer, producer: FakeProducer, calls: Calls) -> None:
    worker = ProfileConsumer(
        _settings(),
        rebuild=calls.rebuild,
        delete=calls.delete,
        clients=lambda: (consumer, producer),
        sleep=calls.sleep,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run())


def test_처리한_메시지마다_다음_offset을_차례로_넘긴다() -> None:
    consumer = FakeConsumer([Record(offset=7), Record(offset=8, key=b"43")])
    producer = FakeProducer()

    _run(consumer, producer, Calls())

    tp = TopicPartition(TOPIC, 0)
    assert consumer.commits == [{tp: 8}, {tp: 9}]


def test_계산은_HTTP_길이_아니라_기다리는_쪽이_없는_길로_한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # HTTP 와 같은 길(service.rebuild)이면 비동기를 켰을 때 목록이 서버 안 줄로 가서, offset 을 넘긴 뒤
    # 만들기 전에 서버가 내려가면 그 목록은 다시 오지 않는다(#726)
    calls = Calls()
    monkeypatch.setattr(kafka_consumer.service, "rebuild_unhurried", calls.rebuild)
    consumer = FakeConsumer([Record(offset=7)])
    worker = ProfileConsumer(
        _settings(), clients=lambda: (consumer, FakeProducer()), sleep=calls.sleep
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run())

    assert calls.rebuilt == [42]
    assert consumer.commits == [{TopicPartition(TOPIC, 0): 8}]


def test_틀린_메시지는_원본_그대로_DLT_토픽에_까닭과_출처를_달아_보낸다() -> None:
    record = Record(offset=8, key=b"43")
    producer = FakeProducer()

    _run(FakeConsumer([record]), producer, Calls())

    assert producer.sent == [
        {
            "topic": "book.ai-profile.v1.DLT",
            "value": record.value,
            "key": b"43",
            "headers": {
                "reason": b"key_mismatch",
                "source_topic": TOPIC.encode(),
                "source_partition": b"0",
                "source_offset": b"8",
            },
        }
    ]


def test_계산_중에_내려가면_그_메시지의_offset을_넘기지_않는다() -> None:
    consumer = FakeConsumer([Record(offset=7)])

    async def stuck(request) -> None:
        raise asyncio.CancelledError

    worker = ProfileConsumer(
        _settings(),
        rebuild=stuck,
        clients=lambda: (consumer, FakeProducer()),
        sleep=Calls().sleep,
    )
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker.run())

    assert consumer.commits == []
    assert consumer.stopped == 1


def test_브로커에_못_붙으면_기다렸다가_다시_붙는다() -> None:
    consumer = FakeConsumer([Record(offset=7)], fail_start=2)
    calls = Calls()

    _run(consumer, FakeProducer(), calls)

    assert consumer.started == 3
    assert calls.slept == [kafka_consumer.RECONNECT_SECONDS] * 2
    assert calls.rebuilt == [42]


# --- 켜고 끄기 ---


def test_꺼져_있으면_소비자를_띄우지_않는다() -> None:
    async def scenario() -> bool:
        started = kafka_consumer.start(_settings(profile_kafka_enabled=False))
        await kafka_consumer.stop()
        return started

    assert asyncio.run(scenario()) is False


@pytest.mark.parametrize(
    "overrides",
    [{"kafka_bootstrap_servers": ""}, {"profile_kafka_group_id": "  "}],
)
def test_켰는데_브로커나_그룹이_비어_있으면_띄우지_않고_까닭을_남긴다(
    overrides: dict, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> bool:
        started = kafka_consumer.start(_settings(**overrides))
        await kafka_consumer.stop()
        return started

    assert asyncio.run(scenario()) is False
    assert "카프카 프로필 소비자를 띄우지 않는다" in caplog.text


def test_켜져_있으면_뒤에서_돌고_서버가_내려갈_때_멈춘다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    async def fake_run(self) -> None:
        ran.append("run")
        await asyncio.Event().wait()

    monkeypatch.setattr(ProfileConsumer, "run", fake_run)

    async def scenario() -> bool:
        started = kafka_consumer.start(_settings())
        await asyncio.sleep(0)
        await kafka_consumer.stop()
        return started

    assert asyncio.run(scenario()) is True
    assert ran == ["run"]


# --- 서버와 함께 뜨고 내려가기 ---


def test_서버가_뜰_때_띄우고_홈_목록_일꾼과_DB보다_먼저_멈춘다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """계산(service.rebuild_unhurried)이 DB 를 쓴다. DB 와 홈 목록 일꾼이 준비된 뒤 띄우고, 닫히기 전에 멈춘다."""
    import contextlib

    from app import main
    from app.feed import build_queue

    events: list[str] = []

    class _FakeDb:
        async def connect(self) -> None:
            events.append("db.connect")

        async def disconnect(self) -> None:
            events.append("db.disconnect")

        def get_pool(self):
            return self

        def acquire(self):
            return contextlib.nullcontext()

        async def report_missing_indexes(self, conn) -> None:
            pass

    async def no_labels() -> None:
        pass

    async def queue_stop() -> None:
        events.append("queue.stop")

    async def kafka_stop() -> None:
        events.append("kafka.stop")

    def kafka_start(settings) -> bool:
        events.append("kafka.start")
        return True

    monkeypatch.setattr(main.cursor, "check_key", lambda: None)
    monkeypatch.setattr(main, "db", _FakeDb())
    monkeypatch.setattr(main.embedding, "load_model", lambda: None)
    monkeypatch.setattr(main.labels, "load", no_labels)
    monkeypatch.setattr(build_queue, "start", lambda: events.append("queue.start"))
    monkeypatch.setattr(build_queue, "stop", queue_stop)
    monkeypatch.setattr(kafka_consumer, "start", kafka_start)
    monkeypatch.setattr(kafka_consumer, "stop", kafka_stop)

    async def run() -> None:
        async with main.lifespan(main.app):
            events.append("serving")

    asyncio.run(run())

    assert events == [
        "db.connect",
        "queue.start",
        "kafka.start",
        "serving",
        "kafka.stop",
        "queue.stop",
        "db.disconnect",
    ]


# --- 파티션을 내줄 때(#734). 옛 서버가 처리 중이던 갱신이 새 서버의 삭제보다 늦게 저장되지 않게 한다(#730) ---


def test_토픽을_구독하며_파티션을_내줄_때의_콜백을_건다() -> None:
    consumer = FakeConsumer([])

    _run(consumer, FakeProducer(), Calls())

    assert consumer.topics == [TOPIC]
    assert isinstance(consumer.listener, ConsumerRebalanceListener)


def test_처리_중인_메시지를_마치고_offset까지_넘긴_뒤에야_파티션을_내준다() -> None:
    consumer = FakeConsumer([Record(offset=7)])
    seen: dict = {}

    async def revoke_and_note() -> None:
        await consumer.listener.on_partitions_revoked({TopicPartition(TOPIC, 0)})
        seen["commits_when_revoked"] = list(consumer.commits)

    async def slow_rebuild(request) -> None:
        # 계산하는 사이 재할당이 시작된다
        seen["revoke"] = asyncio.create_task(revoke_and_note())
        for _ in range(10):
            await asyncio.sleep(0)
        seen["revoked_while_processing"] = seen["revoke"].done()

    worker = ProfileConsumer(
        _settings(),
        rebuild=slow_rebuild,
        clients=lambda: (consumer, FakeProducer()),
        sleep=Calls().sleep,
    )

    async def go() -> None:
        with pytest.raises(asyncio.CancelledError):
            await worker.run()
        await seen["revoke"]

    asyncio.run(go())

    assert seen["revoked_while_processing"] is False
    assert seen["commits_when_revoked"] == [{TopicPartition(TOPIC, 0): 8}]


def test_처리_중인_메시지가_없으면_바로_내준다() -> None:
    consumer = FakeConsumer([])
    _run(consumer, FakeProducer(), Calls())

    async def revoke() -> None:
        async with asyncio.timeout(1):
            await consumer.listener.on_partitions_revoked(set())

    asyncio.run(revoke())


def test_재할당을_기다려_주는_시간은_계산_세_번보다_길다() -> None:
    # 기본(session_timeout 10초)이면 처리 중인 메시지를 기다리는 사이 그룹에서 빠져 다른 서버가 파티션을 받는다
    async def made() -> int:
        # aiokafka 는 이벤트 루프 안에서 만들어야 한다. 붙지는 않는다
        consumer, _ = kafka_consumer._clients(_settings())
        return consumer._rebalance_timeout_ms

    assert asyncio.run(made()) == kafka_consumer.REBALANCE_TIMEOUT_MS
    assert kafka_consumer.REBALANCE_TIMEOUT_MS >= 60_000
