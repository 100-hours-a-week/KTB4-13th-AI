"""서명된 페이지 커서 테스트."""

import asyncio

import pytest

from app import main
from app.core import cursor


@pytest.fixture(autouse=True)
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")


def test_만든_커서를_그대로_돌려주면_내용이_나온다() -> None:
    token = cursor.encode({"o": 15, "f": "abc"})

    assert cursor.decode(token) == {"o": 15, "f": "abc"}


def test_커서는_URL에_그대로_실을_수_있는_글자만_쓴다() -> None:
    token = cursor.encode({"o": 15, "f": "한글 조건"})

    assert all(c.isalnum() or c in "-_." for c in token)


def test_30분이_지나면_만료다() -> None:
    token = cursor.encode({"o": 15}, now=1_000_000)

    assert cursor.decode(token, now=1_000_000 + 30 * 60) == {"o": 15}
    with pytest.raises(cursor.CursorError, match="만료"):
        cursor.decode(token, now=1_000_000 + 30 * 60 + 1)


def test_내용을_고치면_서명이_안_맞는다() -> None:
    token = cursor.encode({"o": 15})
    forged_body = cursor._b64(b'{"d":{"o":99999},"t":9999999999}')

    with pytest.raises(cursor.CursorError, match="서명"):
        cursor.decode(f"{forged_body}.{token.split('.')[1]}")


def test_다른_키로_만든_커서는_거부한다(monkeypatch: pytest.MonkeyPatch) -> None:
    token = cursor.encode({"o": 15})
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "other-key")
    cursor.get_settings.cache_clear()

    with pytest.raises(cursor.CursorError, match="서명"):
        cursor.decode(token)


@pytest.mark.parametrize("token", ["", "점없는글자", "a.b", "!!!.???", "."])
def test_모양이_깨진_커서는_거부한다(token: str) -> None:
    with pytest.raises(cursor.CursorError):
        cursor.decode(token)


def test_서명_키가_비어_있으면_크게_실패한다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "")
    cursor.get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="CURSOR_SIGNING_KEY"):
        cursor.encode({"o": 15})


def test_서명_키가_비어_있으면_서버가_DB에_붙기_전에_멈춘다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "")
    connected = False

    async def _connect() -> None:
        nonlocal connected
        connected = True

    monkeypatch.setattr(main.db, "connect", _connect)

    async def _start() -> None:
        async with main.lifespan(main.app):
            pass

    with pytest.raises(RuntimeError, match="CURSOR_SIGNING_KEY"):
        asyncio.run(_start())
    assert connected is False
