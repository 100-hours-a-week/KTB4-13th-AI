"""관심사별 홈 추천이 실제로 켜졌는지(app/feed/flag.py, #420). DB 없이 돈다.

설정이 켜져 있어도 새 표·칸(마이그레이션)이나 책 분류 데이터가 없으면 꺼진 것처럼 돈다.
"""

import asyncio
import logging

import pytest

from app.core.config import get_settings
from app.feed import flag


class _Conn:
    """flag.missing 이 보내는 조회에 정해 둔 답을 준다."""

    def __init__(
        self, absent_columns=(), classes_missing=False, classes_empty=False
    ) -> None:
        self.absent_columns = list(absent_columns)
        self.classes_missing = classes_missing
        self.classes_empty = classes_empty
        self.calls = 0

    async def fetch(self, sql: str, *args):
        self.calls += 1
        return [{"name": n} for n in self.absent_columns]

    async def fetchval(self, sql: str, *args):
        self.calls += 1
        if "to_regclass" in sql:
            return self.classes_missing
        return not self.classes_empty


class _Broken:
    async def fetch(self, sql: str, *args):
        raise RuntimeError("DB 없음")

    fetchval = fetch


@pytest.fixture
def settings_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "interest_feed", True)
    # check 가 바꾼 값을 테스트가 끝나면 되돌린다
    monkeypatch.setattr(flag, "_schema_ready", True)


def test_설정이_꺼져_있으면_확인하지_않고_끈_채로_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "interest_feed", False)
    monkeypatch.setattr(flag, "_schema_ready", True)
    conn = _Conn()

    asyncio.run(flag.check(conn))

    assert conn.calls == 0
    assert flag.on() is False


@pytest.mark.usefixtures("settings_on")
def test_새_표와_칸과_분류_데이터가_다_있으면_켠다() -> None:
    asyncio.run(flag.check(_Conn()))

    assert flag.on() is True


@pytest.mark.usefixtures("settings_on")
@pytest.mark.parametrize(
    ("conn", "named"),
    [
        (_Conn(absent_columns=["feed", "feed_version"]), "taste_profile.feed_version"),
        (_Conn(classes_missing=True), "book_classes"),
        (_Conn(classes_empty=True), "book_classes 데이터"),
    ],
)
def test_하나라도_없으면_끄고_없는_것을_경고로_남긴다(
    conn: _Conn, named: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.feed.flag"):
        asyncio.run(flag.check(conn))

    assert flag.on() is False
    assert named in caplog.text


@pytest.mark.usefixtures("settings_on")
def test_확인이_실패해도_끄고_서버는_그대로_뜬다() -> None:
    asyncio.run(flag.check(_Broken()))

    assert flag.on() is False
