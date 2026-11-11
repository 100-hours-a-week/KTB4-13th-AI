"""카프카로 온 프로필 계산 요청 읽기(#668). 계약은 BE #551 README 「AI 추천 프로필 Kafka 직접 전달」이다.

지키려는 것
- 맞는 메시지는 PUT /preferences/profile 과 같은 요청 객체가 된다
- 틀린 메시지는 다시 읽어도 결과가 같아서 까닭(reason)을 달아 BadMessage 로 알린다. 소비자는 이것을 DLT 로 보낸다
- 까닭에는 본문을 싣지 않는다. 메시지에는 개인정보·벡터가 들어 있어 로그에 남기지 않는다(BE 계약)
- 키(사용자 번호)와 profile.user_id 가 다르면 받지 않는다(BE 계약)
"""

import json

import pytest

from app.profile.kafka_message import BadMessage, ProfileMessage, parse

MAX_BYTES = 8 * 1024 * 1024
REVISION = "2d32c6a2-322d-46c3-b3d3-6c8680b6a723"


def _profile(**overrides) -> dict:
    profile = {
        "user_id": 42,
        "idempotency_key": "existing-http-request-key",
        "onboarding": {
            "reading_times": ["잠들기 전"],
            "criteria": [],
            "categories": ["소설"],
            "tags": ["SF"],
            "liked_isbns": [],
        },
        "memories": [],
        "history": {"purchased_isbns": [], "library_isbns": [], "reviews": []},
    }
    profile.update(overrides)
    return profile


def _value(**overrides) -> bytes:
    message = {"schema_version": 1, "job_revision": REVISION, "profile": _profile()}
    message.update(overrides)
    return json.dumps(message, ensure_ascii=False).encode()


def _reason(key: bytes | None, value: bytes | None) -> str:
    with pytest.raises(BadMessage) as caught:
        parse(key, value, max_bytes=MAX_BYTES)
    return caught.value.reason


def test_계약의_예시_메시지를_PUT과_같은_요청으로_읽는다() -> None:
    message = parse(b"42", _value(), max_bytes=MAX_BYTES)

    assert isinstance(message, ProfileMessage)
    assert message.job_revision == REVISION
    assert message.request.user_id == 42
    assert message.request.idempotency_key == "existing-http-request-key"
    assert message.request.onboarding.categories == ["소설"]
    assert message.request.history is not None


def test_키와_profile의_사용자가_다르면_받지_않는다() -> None:
    assert _reason(b"43", _value()) == "key_mismatch"


@pytest.mark.parametrize("key", [None, b""])
def test_키가_없으면_받지_않는다(key: bytes | None) -> None:
    assert _reason(key, _value()) == "no_key"


@pytest.mark.parametrize("key", [b"abc", b"042", b"+42", b" 42", b"4.2", b"\xff"])
def test_키가_사용자_번호의_10진_글자가_아니면_받지_않는다(key: bytes) -> None:
    assert _reason(key, _value()) == "bad_key"


@pytest.mark.parametrize("version", [2, 0, "1", True, None])
def test_계약_버전이_1이_아니면_받지_않는다(version: object) -> None:
    assert _reason(b"42", _value(schema_version=version)) == "schema_version"


def test_계약_버전_칸이_없으면_받지_않는다() -> None:
    message = {"job_revision": REVISION, "profile": _profile()}

    assert _reason(b"42", json.dumps(message).encode()) == "schema_version"


@pytest.mark.parametrize("revision", [None, "", "   ", 7])
def test_작업_번호가_없으면_받지_않는다(revision: object) -> None:
    assert _reason(b"42", _value(job_revision=revision)) == "job_revision"


@pytest.mark.parametrize(
    "profile",
    [
        None,
        "{}",
        {"user_id": 42},  # idempotency_key·onboarding 이 없다
        _profile(idempotency_key=""),  # 칸의 계약은 HTTP 와 같다(#303)
        _profile(user_id="42"),  # HTTP 처럼 글자를 숫자로 바꿔 받지 않는다
    ],
)
def test_profile이_PUT_본문_계약에_맞지_않으면_받지_않는다(profile: object) -> None:
    assert _reason(b"42", _value(profile=profile)) == "invalid_profile"


def test_모르는_칸은_HTTP처럼_무시하고_받는다() -> None:
    message = parse(b"42", _value(profile=_profile(new_field=1)), max_bytes=MAX_BYTES)

    assert message.request.user_id == 42


@pytest.mark.parametrize("value", [None, b""])
def test_본문이_없으면_받지_않는다(value: bytes | None) -> None:
    assert _reason(b"42", value) == "empty"


@pytest.mark.parametrize("value", [b"{", b"\xff\xfe", "책".encode("cp949")])
def test_JSON이_아니면_받지_않는다(value: bytes) -> None:
    assert _reason(b"42", value) == "not_json"


@pytest.mark.parametrize("value", [b"[]", b'"profile"', b"42"])
def test_JSON_객체가_아니면_받지_않는다(value: bytes) -> None:
    assert _reason(b"42", value) == "not_object"


def test_크기_한도를_넘으면_읽지_않는다() -> None:
    value = _value()

    with pytest.raises(BadMessage) as caught:
        parse(b"42", value, max_bytes=len(value) - 1)

    assert caught.value.reason == "too_large"


def test_틀린_까닭에_본문을_싣지_않는다() -> None:
    secret = _profile(idempotency_key="")
    secret["memories"] = [{"keyword": "아무도_몰라야_하는_취향"}]

    with pytest.raises(BadMessage) as caught:
        parse(b"42", _value(profile=secret), max_bytes=MAX_BYTES)

    assert "아무도_몰라야_하는_취향" not in str(caught.value)
    assert "아무도_몰라야_하는_취향" not in repr(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
