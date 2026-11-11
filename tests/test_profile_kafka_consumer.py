"""카프카로 오는 프로필 계산 요청의 소비자(#669). 브로커 없이 가짜 소비자·발행자로 돈다.

지키려는 것(BE #551 README 「AI 추천 프로필 Kafka 직접 전달」)
- 저장(계산)이 끝난 뒤에만 offset 을 넘긴다
- 틀린 메시지는 다시 하지 않고 DLT 로 보낸다. 계산이 일시 실패하면 간격을 늘려 다시 하고, 다 실패하면 DLT 로 보낸다
- DLT 발행이 실패하면 원본을 넘기지 않는다(성공할 때까지 다시 한다)
- 기본은 꺼져 있다. 켰는데 브로커·그룹이 비어 있으면 띄우지 않고, 브로커에 못 붙어도 서버는 뜬다
"""

import asyncio
from dataclasses import dataclass, field

import pytest
from aiokafka.structs import TopicPartition

from app.core.config import Settings
from app.profile import kafka_consumer
from app.profile.kafka_consumer import ProfileConsumer, handle
from tests.test_profile_kafka_message import _value

MAX_BYTES = 8 * 1024 * 1024
TOPIC = "book.ai-profile.v1"


@dataclass
class Record:
    offset: int
    key: bytes | None = b"42"
    value: bytes | None = field(default_factory=_value)
    topic: str = TOPIC
    partition: int = 0


class Calls:
    """가짜 계산·DLT 발행·잠자기가 불린 차례를 적는다. fail_* 만큼 먼저 실패한다."""

    def __init__(self, *, fail_rebuild: int = 0, fail_dlt: int = 0) -> None:
        self.fail_rebuild = fail_rebuild
        self.fail_dlt = fail_dlt
        self.rebuilt: list[int] = []
        self.dlt: list[tuple[int, str]] = []
        self.slept: list[float] = []

    async def rebuild(self, request) -> None:
        self.rebuilt.append(request.user_id)
        if self.fail_rebuild > 0:
            self.fail_rebuild -= 1
            raise RuntimeError("DB 연결이 잠깐 끊겼다")

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
