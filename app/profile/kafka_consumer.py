"""카프카로 오는 프로필 계산 요청을 받아 계산한다(#669). 기본은 꺼짐(PROFILE_KAFKA_ENABLED).

BE 가 온보딩 완료·취향 수정 때 PUT /preferences/profile 과 같은 본문을 토픽 book.ai-profile.v1 로 보낸다(BE #551).
계약(BE README 「AI 추천 프로필 Kafka 직접 전달」)에서 AI 가 지킬 것:

- 자동 offset commit 을 끄고, 저장이 끝난 뒤에만 commit 한다
- 일시 실패는 간격을 늘려 다시 하고, 다 실패하면 DLT 에 보낸 것을 확인한 다음 원본 offset 을 넘긴다
- DLT 발행이 실패하면 원본을 넘기지 않는다
- 본문은 로그에 남기지 않는다(개인정보·벡터)

계산은 HTTP PUT 과 같은 길(service.rebuild)이다. 같은 사용자의 계산은 사용자별 잠금으로 차례로 돌고, 받은 본문 전체로
처음부터 다시 계산하므로 같은 메시지가 두 번 와도 결과가 같다. 그래서 내려가는 중이나 다시 붙은 뒤 같은 메시지가 한 번 더
와도 해가 없다.

메시지는 하나씩 차례로 처리한다. 같은 사용자의 메시지는 같은 파티션에 오므로(키가 사용자 번호) 순서가 지켜진다.
브로커에 못 붙어도 서버는 그대로 뜨고, 이 소비자만 RECONNECT_SECONDS 마다 다시 붙는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
from aiokafka.structs import TopicPartition

from app.core.config import Settings
from app.profile import kafka_message, service
from app.profile.kafka_message import BadMessage
from app.profile.schemas import ProfileRequest

logger = logging.getLogger(__name__)

# 계산을 해 보는 횟수. 다 실패하면 DLT 로 보낸다
REBUILD_ATTEMPTS = 3
# 다시 하는 간격은 1초부터 두 배씩, 이 상한까지
RETRY_BASE_SECONDS = 1.0
RETRY_MAX_SECONDS = 30.0
# 브로커에 못 붙거나 받다가 끊겼을 때 다시 붙기까지 기다리는 시간
RECONNECT_SECONDS = 5.0
# DLT 로 보낼 때 머리글이 붙는 몫. 원본이 크기 한도에 꽉 차도 다시 보낼 수 있게 발행 한도를 이만큼 넉넉히 둔다
_HEADER_ROOM_BYTES = 64 * 1024


class Record(Protocol):
    topic: str
    partition: int
    offset: int
    key: bytes | None
    value: bytes | None


Rebuild = Callable[[ProfileRequest], Awaitable[Any]]
SendDlt = Callable[[Record, str], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


def _delay(attempt: int) -> float:
    return min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** (attempt - 1))


def _where(record: Record) -> str:
    return f"{record.topic}/{record.partition}/{record.offset}"


async def _to_dlt(record: Record, reason: str, send_dlt: SendDlt, sleep: Sleep) -> None:
    """DLT 에 보낸다. 실패하면 성공할 때까지 다시 한다 — 그동안 원본 offset 을 넘기지 않는다(BE 계약)."""
    attempt = 0
    while True:
        attempt += 1
        try:
            await send_dlt(record, reason)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "카프카 프로필 DLT 발행 실패(%s, %d번째) — 원본을 넘기지 않고 다시 한다",
                _where(record),
                attempt,
                exc_info=True,
            )
            await sleep(_delay(attempt))


async def handle(
    record: Record,
    *,
    max_bytes: int,
    rebuild: Rebuild,
    send_dlt: SendDlt,
    sleep: Sleep,
) -> None:
    """메시지 하나를 끝까지 처리한다. 돌아오면 offset 을 넘겨도 된다(계산해 저장했거나 DLT 로 보냈다).

    서버가 내려가며 취소되면 CancelledError 가 그대로 올라간다. 그 메시지는 넘기지 않아서 다음에 다시 온다.
    """
    try:
        message = kafka_message.parse(record.key, record.value, max_bytes=max_bytes)
    except BadMessage as bad:
        # 다시 읽어도 결과가 같다. 다시 하지 않고 DLT 로 보낸다
        logger.warning(
            "카프카 프로필 메시지를 받지 않는다(%s, %s)", bad.reason, _where(record)
        )
        await _to_dlt(record, bad.reason, send_dlt, sleep)
        return

    user_id = message.request.user_id
    for attempt in range(1, REBUILD_ATTEMPTS + 1):
        try:
            await rebuild(message.request)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "카프카로 온 프로필 계산 실패(사용자 %d, %d/%d번째)",
                user_id,
                attempt,
                REBUILD_ATTEMPTS,
                exc_info=True,
            )
            if attempt < REBUILD_ATTEMPTS:
                await sleep(_delay(attempt))
            continue
        logger.info("카프카로 온 프로필을 계산했다(사용자 %d)", user_id)
        return

    logger.error(
        "카프카로 온 프로필 계산을 %d번 실패해 DLT 로 보낸다(사용자 %d, %s)",
        REBUILD_ATTEMPTS,
        user_id,
        _where(record),
    )
    await _to_dlt(record, "rebuild_failed", send_dlt, sleep)


def problem(settings: Settings) -> str | None:
    """켰는데 띄울 수 없는 까닭. 띄울 수 있으면 None."""
    if not settings.kafka_bootstrap_servers.strip():
        return "KAFKA_BOOTSTRAP_SERVERS 가 비어 있다"
    if not settings.profile_kafka_group_id.strip():
        return "PROFILE_KAFKA_GROUP_ID 가 비어 있다"
    return None


def _clients(settings: Settings) -> tuple[Any, Any]:
    """설정으로 소비자와 DLT 발행자를 만든다. 아직 붙지는 않는다."""
    servers = [
        s.strip() for s in settings.kafka_bootstrap_servers.split(",") if s.strip()
    ]
    limit = settings.profile_kafka_max_message_bytes
    consumer = AIOKafkaConsumer(
        settings.profile_kafka_topic,
        bootstrap_servers=servers,
        group_id=settings.profile_kafka_group_id.strip(),
        client_id="bookjeok-ai",
        enable_auto_commit=False,
        # 그룹이 처음 붙을 때 남아 있는 메시지부터 받는다. BE 는 발행 확인으로 작업을 끝내므로, 늦게 붙은 소비자가
        # 앞 메시지를 건너뛰면 그 프로필은 다시 오지 않는다
        auto_offset_reset="earliest",
        # 기본(1MB)이면 8MB 본문을 못 받는다
        max_partition_fetch_bytes=limit + _HEADER_ROOM_BYTES,
    )
    producer = AIOKafkaProducer(
        bootstrap_servers=servers,
        client_id="bookjeok-ai",
        acks="all",
        max_request_size=limit + _HEADER_ROOM_BYTES,
    )
    return consumer, producer


class ProfileConsumer:
    """토픽을 받는 순환. run() 은 취소될 때까지 돈다."""

    def __init__(
        self,
        settings: Settings,
        *,
        rebuild: Rebuild = service.rebuild,
        clients: Callable[[], tuple[Any, Any]] | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._settings = settings
        self._rebuild = rebuild
        self._clients = clients or (lambda: _clients(settings))
        self._sleep = sleep

    async def run(self) -> None:
        while True:
            consumer, producer = self._clients()
            try:
                await consumer.start()
                await producer.start()
            except asyncio.CancelledError:
                await _stop(consumer, producer)
                raise
            except Exception:
                logger.warning(
                    "카프카 브로커에 못 붙었다 — %.0f초 뒤 다시 붙는다",
                    RECONNECT_SECONDS,
                    exc_info=True,
                )
                await _stop(consumer, producer)
                await self._sleep(RECONNECT_SECONDS)
                continue

            logger.info(
                "카프카 프로필 소비자를 띄웠다(토픽 %s, 그룹 %s)",
                self._settings.profile_kafka_topic,
                self._settings.profile_kafka_group_id,
            )
            try:
                await self._consume(consumer, producer)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 커밋 실패(그룹이 다시 나뉨)나 끊김. 넘기지 못한 메시지는 다시 붙은 뒤 다시 온다
                logger.warning(
                    "카프카 프로필 소비자가 멈췄다 — %.0f초 뒤 다시 붙는다",
                    RECONNECT_SECONDS,
                    exc_info=True,
                )
            finally:
                await _stop(consumer, producer)
            await self._sleep(RECONNECT_SECONDS)

    async def _consume(self, consumer: Any, producer: Any) -> None:
        async def send_dlt(record: Record, reason: str) -> None:
            await producer.send_and_wait(
                self._settings.profile_kafka_dlt_topic,
                value=record.value,
                key=record.key,
                headers=[
                    ("reason", reason.encode()),
                    ("source_topic", record.topic.encode()),
                    ("source_partition", str(record.partition).encode()),
                    ("source_offset", str(record.offset).encode()),
                ],
            )

        while True:
            record = await consumer.getone()
            await handle(
                record,
                max_bytes=self._settings.profile_kafka_max_message_bytes,
                rebuild=self._rebuild,
                send_dlt=send_dlt,
                sleep=self._sleep,
            )
            await consumer.commit(
                {TopicPartition(record.topic, record.partition): record.offset + 1}
            )


async def _stop(consumer: Any, producer: Any) -> None:
    """붙다 만 연결도 닫는다. 닫다가 난 오류는 다시 붙는 것을 막지 않게 남기기만 한다."""
    for client in (consumer, producer):
        try:
            await client.stop()
        except Exception:
            logger.warning("카프카 연결 닫기 실패", exc_info=True)


_task: asyncio.Task[None] | None = None


def start(settings: Settings) -> bool:
    """켜져 있으면 소비자를 뒤에서 띄운다. 띄웠으면 True. 서버가 뜰 때 부른다(app/main.py)."""
    global _task
    if not settings.profile_kafka_enabled:
        return False
    if (why := problem(settings)) is not None:
        logger.warning("카프카 프로필 소비자를 띄우지 않는다: %s", why)
        return False
    _task = asyncio.create_task(
        ProfileConsumer(settings).run(), name="profile-kafka-consumer"
    )
    return True


async def stop() -> None:
    """서버가 내려갈 때 부른다. 처리 중이던 메시지는 넘기지 않아서 다음에 다시 온다."""
    global _task
    if _task is None:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _task
    _task = None
