"""카프카로 프로필 계산 요청 받기(#666)의 설정(#667).

지키려는 것
- 기본은 꺼져 있다. 켜지 않으면 서버는 브로커를 찾지 않는다
- 토픽·DLT 이름은 BE #551 계약과 같다
- 메시지 크기 한도는 HTTP PUT 한도와 같다. 같은 본문이 어느 길로 와도 받을 수 있어야 한다
"""

import pytest

from app.core.config import Settings
from app.routers import profile as profile_router


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    for name in (
        "PROFILE_KAFKA_ENABLED",
        "KAFKA_BOOTSTRAP_SERVERS",
        "PROFILE_KAFKA_TOPIC",
        "PROFILE_KAFKA_DLT_TOPIC",
        "PROFILE_KAFKA_GROUP_ID",
        "PROFILE_KAFKA_MAX_MESSAGE_BYTES",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_기본은_꺼져_있고_브로커_주소가_없다(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)

    assert settings.profile_kafka_enabled is False
    assert settings.kafka_bootstrap_servers == ""
    assert settings.profile_kafka_group_id == ""


def test_토픽_이름은_BE_계약과_같다(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(monkeypatch)

    assert settings.profile_kafka_topic == "book.ai-profile.v1"
    assert settings.profile_kafka_dlt_topic == "book.ai-profile.v1.DLT"


def test_메시지_크기_한도는_HTTP_PUT_한도와_같다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch)

    assert settings.profile_kafka_max_message_bytes == profile_router.MAX_BODY_BYTES


def test_환경변수로_켜고_브로커와_그룹을_정한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        PROFILE_KAFKA_ENABLED="true",
        KAFKA_BOOTSTRAP_SERVERS="b-1:9092,b-2:9092",
        PROFILE_KAFKA_GROUP_ID="book-ai-profile-v1-dev",
    )

    assert settings.profile_kafka_enabled is True
    assert settings.kafka_bootstrap_servers == "b-1:9092,b-2:9092"
    assert settings.profile_kafka_group_id == "book-ai-profile-v1-dev"


def test_카프카_클라이언트를_불러올_수_있다() -> None:
    import aiokafka

    assert hasattr(aiokafka, "AIOKafkaConsumer")
    assert hasattr(aiokafka, "AIOKafkaProducer")
