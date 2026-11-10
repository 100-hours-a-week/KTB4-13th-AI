"""③ 후보 리랭커 어댑터(app/gateway/reranker.py) 테스트.

실제 모델(fp32 약 2.3GB)은 읽지 않는다. predict 하나만 있는 가짜 모델을 끼운다.
"""

import asyncio
import io
import json
import threading
import time

import pytest

from app.core.config import get_settings
from app.gateway import reranker


class _FakeModel:
    """책 글에 든 낱말로 점수를 매기는 가짜. 받은 묶음 크기를 기록한다."""

    def __init__(
        self, scores: dict[str, float], gate: threading.Event | None = None
    ) -> None:
        self.scores = scores
        self.gate = gate
        self.batch_sizes: list[int] = []
        self.pair_counts: list[int] = []

    def predict(self, pairs, batch_size: int = 32, show_progress_bar: bool = False):
        self.batch_sizes.append(batch_size)
        self.pair_counts.append(len(pairs))
        if self.gate is not None:
            self.gate.wait(timeout=5)
        return [self.scores.get(text.split("\n")[0], 0.0) for _, text in pairs]


@pytest.fixture(autouse=True)
def fresh_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트마다 빈 추론 슬롯에서 시작한다."""
    monkeypatch.setattr(reranker, "_slot", threading.Lock())


def _books(*titles: str) -> list[dict]:
    return [
        {"book_id": i, "title": t, "author": "작가", "description": "소개"}
        for i, t in enumerate(titles)
    ]


def _wait_until_free(timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not reranker._slot.locked():
            return True
        time.sleep(0.01)
    return False


def test_책_글은_제목_저자_소개를_줄바꿈으로_잇고_빈_칸은_뺀다() -> None:
    assert (
        reranker._book_text(
            {"title": " 데미안 ", "author": None, "description": "성장 소설"}
        )
        == "데미안\n성장 소설"
    )
    assert reranker._book_text({"title": "제목만"}) == "제목만"


def test_점수가_높은_순으로_줄_세우고_같으면_검색_순서를_지킨다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _FakeModel({"가": 0.1, "나": 0.9, "다": 0.5, "라": 0.5})
    monkeypatch.setattr(reranker, "_model", model)
    books = _books("가", "나", "다", "라")

    result = asyncio.run(reranker.rerank("비 오는 날", books))

    assert [b["title"] for b in result] == ["나", "다", "라", "가"]
    # 받은 목록은 건드리지 않고 새 목록을 돌려준다
    assert [b["title"] for b in books] == ["가", "나", "다", "라"]
    assert model.batch_sizes == [get_settings().reranker_batch_size]
    assert _wait_until_free()


def test_모델이_없으면_받은_목록을_그대로_돌려준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reranker, "_model", None)
    books = _books("가", "나")

    assert asyncio.run(reranker.rerank("비 오는 날", books)) is books


def test_다른_요청이_추론_중이면_기다리지_않고_검색_순서를_쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _FakeModel({"나": 1.0})
    monkeypatch.setattr(reranker, "_model", model)
    reranker._slot.acquire()
    books = _books("가", "나")
    try:
        result = asyncio.run(reranker.rerank("비 오는 날", books))
    finally:
        reranker._slot.release()

    assert result is books
    assert model.batch_sizes == []


def test_시간을_넘기면_검색_순서를_쓰고_추론이_끝나면_슬롯을_푼다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RERANKER_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()
    gate = threading.Event()
    monkeypatch.setattr(reranker, "_model", _FakeModel({"나": 1.0}, gate=gate))
    books = _books("가", "나")

    result = asyncio.run(reranker.rerank("비 오는 날", books))

    assert result is books
    # 스레드의 추론은 아직 돌고 있어 슬롯이 잡혀 있다 — 그사이 온 요청은 바로 검색 순서를 쓴다
    assert reranker._slot.locked()
    gate.set()
    assert _wait_until_free()


def test_추론이_실패하면_검색_순서를_쓰고_슬롯을_푼다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Broken:
        def predict(self, *args, **kwargs):
            raise RuntimeError("터짐")

    monkeypatch.setattr(reranker, "_model", _Broken())
    books = _books("가", "나")

    assert asyncio.run(reranker.rerank("비 오는 날", books)) is books
    assert _wait_until_free()


def test_후보가_한_권이거나_질문이_비면_모델을_부르지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _FakeModel({})
    monkeypatch.setattr(reranker, "_model", model)

    asyncio.run(reranker.rerank("비 오는 날", _books("가")))
    asyncio.run(reranker.rerank("", _books("가", "나")))

    assert model.batch_sizes == []


def test_메모리_한도가_모자라면_모델을_읽지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 지금 운영 AI 태스크 한도(3GiB)에서 띄우면 컨테이너가 메모리 부족으로 죽는다
    monkeypatch.setattr(reranker, "_model", None)
    monkeypatch.setattr(reranker, "_memory_limit_mib", lambda: 3072)

    def _must_not_load(*args, **kwargs):
        raise AssertionError("모델을 읽으면 안 된다")

    monkeypatch.setattr(reranker, "CrossEncoder", _must_not_load)

    reranker.load_model()

    assert reranker.is_loaded() is False


@pytest.mark.parametrize("limit", [None, 5120])
def test_메모리_한도가_넉넉하거나_없으면_모델을_읽고_한_번_돌려_둔다(
    monkeypatch: pytest.MonkeyPatch, limit: int | None
) -> None:
    monkeypatch.setattr(reranker, "_model", None)
    monkeypatch.setattr(reranker, "_memory_limit_mib", lambda: limit)
    monkeypatch.setattr(reranker, "_server_available_mib", lambda: None)
    built: dict = {}

    def _fake_cross_encoder(name: str, device: str, max_length: int) -> _FakeModel:
        built.update(name=name, device=device, max_length=max_length)
        built["model"] = _FakeModel({})
        return built["model"]

    monkeypatch.setattr(reranker, "CrossEncoder", _fake_cross_encoder)

    reranker.load_model()

    settings = get_settings()
    assert built["name"] == settings.reranker_model
    assert built["device"] == "cpu"
    assert built["max_length"] == settings.reranker_max_length
    # 예열 한 쌍(기본 묶음 크기), 그다음 실제 요청과 같은 묶음 크기로 30권 속도 시험
    assert built["model"].pair_counts == [1, 30]
    assert built["model"].batch_sizes == [32, settings.reranker_batch_size]
    assert reranker.is_loaded() is True


def test_꺼져_있으면_읽기를_시작하지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_RERANKER", "false")
    get_settings.cache_clear()

    def _must_not_start(*args, **kwargs):
        raise AssertionError("읽기 스레드를 띄우면 안 된다")

    monkeypatch.setattr(reranker.threading, "Thread", _must_not_start)

    reranker.start_loading()


def test_cgroup_v2_한도를_MiB로_읽는다(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    v2 = tmp_path / "memory.max"
    v2.write_text(f"{3 * 1024**3}\n")
    monkeypatch.setattr(reranker, "_CGROUP_LIMIT_FILES", (v2, tmp_path / "없음"))
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)

    assert reranker._memory_limit_mib() == 3072


def test_cgroup에_한도가_없으면_ECS_태스크_메타데이터를_본다(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    v2 = tmp_path / "memory.max"
    v2.write_text("max\n")
    v1 = tmp_path / "memory.limit_in_bytes"
    v1.write_text("9223372036854771712\n")  # cgroup v1 의 "한도 없음"
    monkeypatch.setattr(reranker, "_CGROUP_LIMIT_FILES", (v2, v1))
    monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
    asked: list[str] = []

    def _urlopen(url: str, timeout: float):
        asked.append(url)
        return io.BytesIO(json.dumps({"Limits": {"CPU": 1, "Memory": 3072}}).encode())

    monkeypatch.setattr(reranker.urllib.request, "urlopen", _urlopen)

    assert reranker._memory_limit_mib() == 3072
    assert asked == ["http://169.254.170.2/v4/abc/task"]


def test_한도를_어디서도_못_읽으면_None(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setattr(reranker, "_CGROUP_LIMIT_FILES", (tmp_path / "없음",))
    monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)

    assert reranker._memory_limit_mib() is None


def test_태스크_한도가_넉넉해도_서버에_남은_메모리가_모자라면_읽지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 4GiB 서버(t3.medium)에 4GiB 태스크: 한도 검사는 지나도 서버의 운영체제·로그 수집기 몫이 없다
    monkeypatch.setattr(reranker, "_model", None)
    monkeypatch.setattr(reranker, "_memory_limit_mib", lambda: 4096)
    monkeypatch.setattr(reranker, "_server_available_mib", lambda: 2400)

    def _must_not_load(*args, **kwargs):
        raise AssertionError("모델을 읽으면 안 된다")

    monkeypatch.setattr(reranker, "CrossEncoder", _must_not_load)

    reranker.load_model()

    assert reranker.is_loaded() is False


def test_서버에_남은_메모리는_meminfo_의_MemAvailable_을_MiB로_읽는다(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(
        "MemTotal:        3936000 kB\nMemFree:          200000 kB\nMemAvailable:    2457600 kB\n"
    )
    monkeypatch.setattr(reranker, "_MEMINFO", meminfo)
    assert reranker._server_available_mib() == 2400

    monkeypatch.setattr(reranker, "_MEMINFO", tmp_path / "없음")
    assert reranker._server_available_mib() is None


def test_30권을_제한_시간_안에_못_매기는_CPU면_모델을_끈다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 운영 t3 서버는 맥보다 처리량이 16–28배 낮았다(#563). 켜 두면 분위기 요청마다 효과 없이 5초만 늦어진다
    monkeypatch.setattr(reranker, "_model", None)
    monkeypatch.setattr(reranker, "_memory_limit_mib", lambda: None)
    monkeypatch.setattr(reranker, "_server_available_mib", lambda: None)
    monkeypatch.setattr(reranker, "CrossEncoder", lambda *a, **k: _FakeModel({}))
    monkeypatch.setattr(
        reranker,
        "_speed_check_seconds",
        lambda model: get_settings().reranker_timeout_seconds + 0.1,
    )

    reranker.load_model()

    assert reranker.is_loaded() is False


def test_속도_시험_권수는_챗봇이_검색에서_받는_권수와_같다() -> None:
    from app.routers import chat

    assert reranker._SPEED_CHECK_PAIRS == chat.SEARCH_SIZE
