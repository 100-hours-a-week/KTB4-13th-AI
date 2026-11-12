"""실제 브로커로 카프카 프로필 소비자를 처음부터 끝까지 본다(#670).

브로커가 있어야 해서 KAFKA_TEST_BOOTSTRAP_SERVERS 가 없으면 건너뛴다(CI 에는 브로커가 없다). 로컬에서는:

    docker compose -f compose.kafka.yml up -d
    KAFKA_TEST_BOOTSTRAP_SERVERS=localhost:19092 uv run pytest tests/test_profile_kafka_e2e.py

계산(service.rebuild_unhurried)은 DB 가 필요해서 가짜로 바꾼다. 여기서 보는 것은 카프카 쪽이다: BE 와 같은 모양으로 보낸 메시지를
받아 계산으로 넘기는지, 틀린 메시지가 원본 그대로 DLT 에 가는지, 처리한 만큼 offset 이 넘어가 다시 붙어도 같은 메시지를
다시 받지 않는지, 1MB 를 넘는 큰 본문을 받고 DLT 로도 다시 보내는지. 테스트마다 토픽·그룹 이름을 새로 만들어 서로 섞이지
않는다.

11/11 이 시험을 처음 돌리며 알게 된 것: 받기 한도(max_partition_fetch_bytes)는 기본(1MB)이어도 브로커가 큰 첫 메시지를
돌려줘 3.6MB 를 받는다. 발행 한도(max_request_size)는 기본이면 큰 원본을 DLT 로 못 보낸다(MessageSizeTooLargeError).
그래서 소비자는 발행 한도만 넓힌다(#673).
"""

import asyncio
import contextlib
import json
import os
import uuid

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.structs import TopicPartition

from app.core.config import Settings
from app.profile.kafka_consumer import ProfileConsumer
from tests.test_profile_kafka_message import _profile, _v2, _v2_delete, _value

BOOTSTRAP = os.environ.get("KAFKA_TEST_BOOTSTRAP_SERVERS", "")

pytestmark = pytest.mark.skipif(
    not BOOTSTRAP, reason="KAFKA_TEST_BOOTSTRAP_SERVERS 가 없다(로컬 브로커 시험)"
)

MAX_BYTES = 8 * 1024 * 1024
# 토픽의 메시지 한도. 브로커·토픽 기본(1MB)이면 큰 본문은 보내는 쪽에서부터 막힌다. 운영 토픽과 DLT 토픽도 이만큼이어야
# 한다(#666 「맞출 것」)
TOPIC_MAX_BYTES = MAX_BYTES + 1024 * 1024
DIM = 384


def _names() -> tuple[str, str, str]:
    run = uuid.uuid4().hex[:8]
    topic = f"test.ai-profile.{run}"
    return topic, f"{topic}.DLT", f"test-ai-profile-{run}"


def _settings(topic: str, dlt: str, group: str) -> Settings:
    return Settings(
        _env_file=None,
        profile_kafka_enabled=True,
        kafka_bootstrap_servers=BOOTSTRAP,
        profile_kafka_topic=topic,
        profile_kafka_dlt_topic=dlt,
        profile_kafka_group_id=group,
    )


async def _create_topics(*names: str) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=BOOTSTRAP)
    await admin.start()
    try:
        await admin.create_topics(
            [
                NewTopic(
                    name,
                    num_partitions=1,
                    replication_factor=1,
                    topic_configs={"max.message.bytes": str(TOPIC_MAX_BYTES)},
                )
                for name in names
            ]
        )
    finally:
        await admin.close()


async def _send(topic: str, messages: list[tuple[bytes, bytes]]) -> None:
    """BE 처럼 키는 사용자 번호 글자, 값은 JSON 바이트로 보낸다."""
    producer = AIOKafkaProducer(
        bootstrap_servers=BOOTSTRAP, max_request_size=TOPIC_MAX_BYTES
    )
    await producer.start()
    try:
        for key, value in messages:
            await producer.send_and_wait(topic, value=value, key=key)
    finally:
        await producer.stop()


async def _read(topic: str, count: int, timeout: float = 20.0) -> list:
    """그룹 없이 토픽을 처음부터 count 개 읽는다."""
    consumer = AIOKafkaConsumer(
        bootstrap_servers=BOOTSTRAP,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
        max_partition_fetch_bytes=TOPIC_MAX_BYTES,
    )
    await consumer.start()
    try:
        consumer.assign([TopicPartition(topic, 0)])
        records = []
        async with asyncio.timeout(timeout):
            while len(records) < count:
                records.append(await consumer.getone())
        return records
    finally:
        await consumer.stop()


async def _committed(topic: str, group: str) -> int | None:
    consumer = AIOKafkaConsumer(bootstrap_servers=BOOTSTRAP, group_id=group)
    await consumer.start()
    try:
        return await consumer.committed(TopicPartition(topic, 0))
    finally:
        await consumer.stop()


async def _consume_until(settings: Settings, rebuilt: list, done, timeout=30.0) -> None:
    """소비자를 띄워 done() 이 참이 될 때까지 돌리고 내린다. 계산은 받은 사용자 번호만 적는다."""

    async def rebuild(request) -> dict:
        rebuilt.append(request.user_id)
        return {"message": "profile_success", "data": {}}

    task = asyncio.create_task(ProfileConsumer(settings, rebuild=rebuild).run())
    try:
        async with asyncio.timeout(timeout):
            while not done():
                if task.done():
                    task.result()  # 소비자가 먼저 죽었으면 그 예외를 보인다
                await asyncio.sleep(0.1)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def test_맞는_메시지는_계산하고_틀린_메시지는_원본_그대로_DLT로_보내고_둘_다_넘긴다() -> (
    None
):
    topic, dlt, group = _names()
    bad = _value()

    async def scenario() -> tuple[list, list, int | None]:
        await _create_topics(topic, dlt)
        await _send(topic, [(b"42", _value()), (b"43", bad)])
        rebuilt: list = []
        await _consume_until(
            _settings(topic, dlt, group), rebuilt, lambda: len(rebuilt) >= 1
        )
        dead = await _read(dlt, 1)
        return rebuilt, dead, await _committed(topic, group)

    rebuilt, dead, committed = asyncio.run(scenario())

    assert rebuilt == [42]
    assert dead[0].key == b"43"
    assert dead[0].value == bad
    assert dict(dead[0].headers) == {
        "reason": b"key_mismatch",
        "source_topic": topic.encode(),
        "source_partition": b"0",
        "source_offset": b"1",
    }
    assert committed == 2


def test_다시_붙은_소비자는_넘긴_메시지를_다시_받지_않는다() -> None:
    topic, dlt, group = _names()

    async def scenario() -> tuple[list, list]:
        await _create_topics(topic, dlt)
        await _send(topic, [(b"42", _value())])
        first: list = []
        await _consume_until(_settings(topic, dlt, group), first, lambda: first)

        await _send(topic, [(b"7", _value(profile=_profile(user_id=7)))])
        second: list = []
        await _consume_until(_settings(topic, dlt, group), second, lambda: second)
        return first, second

    first, second = asyncio.run(scenario())

    assert first == [42]
    assert second == [7]


def _big_profile(**overrides) -> bytes:
    """취향 기억 벡터로 키운 약 3.6MB 본문. 카프카 기본 메시지 한도(1MB)를 넘고 HTTP 한도(8MB) 안이다."""
    memory = {
        "type": "mood",
        "value": "잔잔한 소설을 좋아함",
        "strength": 2,
        "applies_to": [],
        "keyword": "잔잔한",
        "created_at": "2026-09-04T00:12:30.123456",
        "vector": [0.012345678] * DIM,
        "dim": DIM,
    }
    value = _value(profile=_profile(memories=[memory] * 700, **overrides))
    assert 3 * 1024 * 1024 < len(value) < MAX_BYTES, len(value)
    return value


def test_1MB를_넘는_큰_본문도_받아_계산한다() -> None:
    topic, dlt, group = _names()
    big = _big_profile()

    async def scenario() -> list:
        await _create_topics(topic, dlt)
        await _send(topic, [(b"42", big)])
        rebuilt: list = []
        await _consume_until(_settings(topic, dlt, group), rebuilt, lambda: rebuilt)
        return rebuilt

    assert asyncio.run(scenario()) == [42]


def test_1MB를_넘는_큰_틀린_메시지도_DLT로_다시_보내고_넘긴다() -> None:
    """발행 한도가 기본(1MB)이면 DLT 로 못 보내고, 그러면 원본을 넘기지 못해 파티션이 멈춘다(#673)."""
    topic, dlt, group = _names()
    big = _big_profile()

    async def scenario() -> tuple[list, list, int | None]:
        await _create_topics(topic, dlt)
        await _send(topic, [(b"43", big), (b"42", _value())])
        rebuilt: list = []
        # 틀린 큰 메시지 뒤의 맞는 메시지까지 계산하면 앞 메시지를 넘긴 것이다
        await _consume_until(_settings(topic, dlt, group), rebuilt, lambda: rebuilt)
        return rebuilt, await _read(dlt, 1), await _committed(topic, group)

    rebuilt, dead, committed = asyncio.run(scenario())

    assert rebuilt == [42]
    assert dead[0].value == big
    assert dict(dead[0].headers)["reason"] == b"key_mismatch"
    assert committed == 2


def test_보낸_메시지는_BE_계약의_모양이다() -> None:
    """이 파일이 보내는 값이 BE #551 README 예시와 같은 칸을 갖는지 확인한다(시험 자체의 전제)."""
    message = json.loads(_value())

    assert set(message) == {"schema_version", "job_revision", "profile"}
    assert message["schema_version"] == 1


def test_v1_v2_갱신_다음_v2_삭제를_보낸_차례대로_처리하고_모두_넘긴다() -> None:
    # 삭제도 같은 토픽·같은 키로 오면 탈퇴 전에 보낸 갱신이 삭제 뒤에 처리되지 않는다(#733, #730)
    topic, dlt, group = _names()

    async def scenario() -> tuple[list, int | None]:
        await _create_topics(topic, dlt)
        await _send(topic, [(b"42", _value()), (b"42", _v2()), (b"42", _v2_delete())])
        order: list = []

        async def rebuild(request) -> dict:
            order.append(("UPSERT", request.user_id))
            return {"message": "profile_success", "data": {}}

        async def delete(user_id: int) -> bool:
            order.append(("DELETE", user_id))
            return True

        settings = _settings(topic, dlt, group)
        task = asyncio.create_task(
            ProfileConsumer(settings, rebuild=rebuild, delete=delete).run()
        )
        try:
            async with asyncio.timeout(30.0):
                while len(order) < 3 or await _committed(topic, group) != 3:
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.1)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return order, await _committed(topic, group)

    order, committed = asyncio.run(scenario())

    assert order == [("UPSERT", 42), ("UPSERT", 42), ("DELETE", 42)]
    assert committed == 3
