"""카프카로 온 프로필 계산 요청을 읽는다(#668).

BE 가 온보딩 완료·취향 수정 때 PUT /preferences/profile 과 같은 본문을 카프카 토픽으로도 보낸다(BE #551,
README 「AI 추천 프로필 Kafka 직접 전달」). 메시지 모양:

    키: 사용자 번호의 UTF-8 10진 글자("42")
    값: {"schema_version": 1, "job_revision": "<BE 작업 UUID>", "profile": <PUT 본문>}

profile 은 HTTP 와 같은 계약(ProfileRequest)으로 검사한다. 키와 profile.user_id 가 다르면 받지 않는다.

틀린 메시지는 다시 읽어도 결과가 같다. 그래서 까닭만 단 BadMessage 를 던지고, 소비자(#669)는 다시 하지 않고 바로
DLT 로 보낸다. 메시지에는 개인정보와 벡터가 들어 있어 까닭에 본문을 싣지 않고, 검증 오류(입력값이 들어 있다)를
예외에 이어 붙이지도 않는다 — 로그의 스택에 본문이 남지 않게 한다.

job_revision 은 BE 작업 UUID 라 순서를 나타내지 않고, 재시도 때 같은 번호에 최신 내용이 다시 올 수 있다(BE 계약).
그래서 중복을 거르는 데 쓰지 않는다. 프로필은 받은 본문 전체로 처음부터 다시 계산하므로 같은 메시지가 두 번 와도
결과가 같다(app/profile/service.py).
"""

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError

from app.profile.schemas import ProfileRequest

# 메시지 계약 버전. BE 가 모양을 바꾸면 올린다
SCHEMA_VERSION = 1

# BE 는 Long.toString() 으로 키를 만든다. 앞자리 0·부호·공백이 붙은 키는 다른 쪽이 만든 것이다
_USER_KEY = re.compile(r"[1-9][0-9]*")


class BadMessage(Exception):
    """다시 읽어도 처리할 수 없는 메시지. reason 은 짧은 영문 낱말이고 본문은 담지 않는다."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ProfileMessage:
    request: ProfileRequest
    job_revision: str


def _bad(reason: str) -> BadMessage:
    error = BadMessage(reason)
    # 이 예외를 except 안에서 던져도 앞 예외(본문이 든 검증 오류)가 이어지지 않게 한다
    error.__suppress_context__ = True
    return error


def _user_from_key(key: bytes | None) -> int:
    if not key:
        raise _bad("no_key")
    try:
        text = key.decode("utf-8")
    except UnicodeDecodeError:
        raise _bad("bad_key") from None
    if not _USER_KEY.fullmatch(text):
        raise _bad("bad_key")
    return int(text)


def parse(key: bytes | None, value: bytes | None, *, max_bytes: int) -> ProfileMessage:
    """메시지 하나를 요청 객체로 읽는다. 틀리면 BadMessage."""
    if not value:
        raise _bad("empty")
    if len(value) > max_bytes:
        raise _bad("too_large")
    try:
        payload = json.loads(value.decode("utf-8"))
    except ValueError:
        # UnicodeDecodeError 와 JSONDecodeError 둘 다 ValueError 다
        raise _bad("not_json") from None
    if not isinstance(payload, dict):
        raise _bad("not_object")

    version = payload.get("schema_version")
    # True == 1 이라 bool 은 따로 막는다
    if type(version) is not int or version != SCHEMA_VERSION:
        raise _bad("schema_version")
    revision = payload.get("job_revision")
    if not isinstance(revision, str) or not revision.strip():
        raise _bad("job_revision")

    profile = payload.get("profile")
    if not isinstance(profile, dict):
        raise _bad("invalid_profile")
    try:
        request = ProfileRequest.model_validate(profile)
    except ValidationError:
        raise _bad("invalid_profile") from None

    if _user_from_key(key) != request.user_id:
        raise _bad("key_mismatch")
    return ProfileMessage(request=request, job_revision=revision)
