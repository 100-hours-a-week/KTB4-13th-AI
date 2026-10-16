"""⑥ 온보딩 라벨 벡터와 관심사 이름 벡터 테스트. 모델 대신 가짜 embed 를 쓴다."""

import asyncio
import contextlib
import logging

import pytest

from app import main
from app.core import categories
from app.profile import labels


@pytest.fixture(autouse=True)
def _empty_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labels, "_vectors", {})
    monkeypatch.setattr(labels, "_name_vectors", {})


def _fake_embed(calls: list[tuple[list[str], str]]):
    async def embed(texts: list[str], purpose: str):
        calls.append((texts, purpose))
        return [[float(i), 1.0] for i in range(len(texts))], 2, "fake"

    return embed


def test_라벨마다_query_용도로_벡터를_만들어_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(labels.embedding, "embed", _fake_embed(calls))

    asyncio.run(labels.load())

    assert calls[0] == (list(labels.LABELS), "query")
    assert labels.vector("소설") == [float(labels.LABELS.index("소설")), 1.0]
    # 세부 태그도 라벨이다(#165).
    assert labels.vector("추리/스릴러") == [
        float(labels.LABELS.index("추리/스릴러")),
        1.0,
    ]


def test_관심사_이름은_카테고리와_세부_카테고리_전부를_정해_둔_글로_만든다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(labels.embedding, "embed", _fake_embed(calls))

    asyncio.run(labels.load())

    texts = list(categories.NAME_TEXTS.values())
    assert calls[1] == (texts, "query")
    # 이름이 아니라 정해 둔 글로 만든다. 카테고리는 이름 그대로다.
    assert labels.name_vector("시") == [float(texts.index("시집")), 1.0]
    assert labels.name_vector("어린이") == [float(texts.index("어린이")), 1.0]
    assert labels.name_vector("힐링") is None


def test_관심사_이름_벡터를_못_만들어도_라벨_벡터는_그대로다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    fake = _fake_embed([])

    async def names_broken(texts: list[str], purpose: str):
        if texts != list(labels.LABELS):
            raise RuntimeError("모델 없음")
        return await fake(texts, purpose)

    monkeypatch.setattr(labels.embedding, "embed", names_broken)

    with caplog.at_level(logging.ERROR):
        asyncio.run(labels.load())

    assert labels.vector("소설") == [float(labels.LABELS.index("소설")), 1.0]
    assert labels.name_vector("소설") is None
    assert "관심사 이름 벡터를 만들지 못했습니다" in caplog.text


def test_목록에_없는_라벨은_None이다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labels.embedding, "embed", _fake_embed([]))
    asyncio.run(labels.load())

    assert labels.vector("힐링") is None


def test_만들지_못하면_ERROR만_남기고_빈_채로_둔다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(texts: list[str], purpose: str):
        raise RuntimeError("모델 없음")

    monkeypatch.setattr(labels.embedding, "embed", broken)

    with caplog.at_level(logging.ERROR):
        asyncio.run(labels.load())

    assert labels.vector("소설") is None
    assert "라벨 벡터를 만들지 못했습니다" in caplog.text


def test_벡터_개수가_라벨과_다르면_실패로_보고_빈_채로_둔다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def short(texts: list[str], purpose: str):
        return [[1.0, 0.0]], 2, "fake"

    monkeypatch.setattr(labels.embedding, "embed", short)

    with caplog.at_level(logging.ERROR):
        asyncio.run(labels.load())

    assert labels.vector("소설") is None
    assert "라벨 벡터를 만들지 못했습니다" in caplog.text


@pytest.mark.parametrize(
    ("model_loaded", "already", "expected"),
    [
        (True, False, 2),  # 기동 때 못 만들었고 지금은 모델이 떠 있다(라벨, 이름)
        (True, True, 0),  # 이미 만들어 뒀다
        (False, False, 0),  # 모델이 아직 없다. 요청 안에서 모델을 새로 읽지 않는다
    ],
)
def test_비어_있고_모델이_떠_있을_때만_나중에_만든다(
    monkeypatch: pytest.MonkeyPatch, model_loaded: bool, already: bool, expected: int
) -> None:
    calls: list[tuple[list[str], str]] = []
    monkeypatch.setattr(labels.embedding, "embed", _fake_embed(calls))
    monkeypatch.setattr(labels.embedding, "is_loaded", lambda: model_loaded)
    if already:
        monkeypatch.setattr(labels, "_vectors", {"소설": [1.0, 0.0]})

    asyncio.run(labels.ensure_loaded())

    assert len(calls) == expected


class _NoDb:
    """lifespan 이 DB 에 하는 일을 모두 건너뛰는 가짜."""

    def __getattr__(self, name: str):
        if name == "get_pool":
            return lambda: self
        if name == "acquire":
            return lambda: contextlib.nullcontext()
        return _noop


async def _noop(*args, **kwargs) -> None:
    pass


def _start_app(monkeypatch: pytest.MonkeyPatch, load_model) -> list[str]:
    """DB 없이 lifespan 을 돌리고 라벨을 만들려 했는지 기록한다."""
    called: list[str] = []

    async def load() -> None:
        called.append("labels")

    monkeypatch.setattr(main.cursor, "check_key", lambda: None)
    monkeypatch.setattr(main, "db", _NoDb())
    monkeypatch.setattr(main.embedding, "load_model", load_model)
    monkeypatch.setattr(main.labels, "load", load)

    async def run() -> None:
        async with main.lifespan(main.app):
            pass

    asyncio.run(run())
    return called


def test_모델이_뜨면_기동할_때_라벨_벡터를_만든다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _start_app(monkeypatch, lambda: None) == ["labels"]


def test_모델_예열이_실패하면_라벨은_건너뛴다(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError("모델 없음")

    # 같은 실패를 라벨에서 한 번 더 로그로 남기지 않는다.
    assert _start_app(monkeypatch, broken) == []
