"""③ 후보 리랭커(외부 API) 시험(#638).

CHAT_RERANKER=true 면 분위기 요청의 후보를 OpenRouter `POST /api/v1/rerank` 로 다시 줄 세운다. 꺼져 있거나 못 쓰면
받은 목록을 그대로 돌려준다(app/gateway/reranker.py). 네트워크는 가짜 HTTP 로 끼운다.
"""

import asyncio
import json
import logging

import httpx
import pytest

from app.core.config import get_settings
from app.gateway import reranker
from app.gateway.llm import report_models

RERANK_URL = "https://openrouter.ai/api/v1/rerank"


@pytest.fixture
def reranker_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHAT_RERANKER", "true")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter-no-zdr")
    monkeypatch.setenv("EXTERNAL_AI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MOCK", "false")
    get_settings.cache_clear()


def _api(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """리랭커로 가는 HTTP 를 handler 로 받는다. 보낸 요청을 모아 돌려준다."""
    sent: list[httpx.Request] = []

    async def _record(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return await handler(request)

    monkeypatch.setattr(
        reranker,
        "_http",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(_record)),
    )
    return sent


def _scored(*pairs: tuple[int, float]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "cohere/rerank-5-fast",
            "results": [{"index": i, "relevance_score": s} for i, s in pairs],
            "usage": {"cost": 0.00017},
        },
    )


def _books(n: int) -> list[dict]:
    return [
        {"book_id": i, "title": f"책{i}", "author": "작가", "description": f"소개{i}"}
        for i in range(1, n + 1)
    ]


def _ids(books: list[dict]) -> list[int]:
    return [b["book_id"] for b in books]


def _rerank(query: str, books: list[dict]) -> list[dict]:
    return asyncio.run(reranker.rerank(query, books))


def _logged(caplog: pytest.LogCaptureFixture, level: int) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == level]


# --- 점수로 다시 세우기 ------------------------------------------------------------


def test_점수가_높은_순으로_다시_세운다(
    monkeypatch: pytest.MonkeyPatch, reranker_on: None
) -> None:
    async def _ok(request: httpx.Request) -> httpx.Response:
        return _scored((0, 0.1), (1, 0.9), (2, 0.5))

    _api(monkeypatch, _ok)

    assert _ids(_rerank("잔잔한 소설", _books(3))) == [2, 3, 1]


def test_점수가_같으면_검색_순서를_지킨다(
    monkeypatch: pytest.MonkeyPatch, reranker_on: None
) -> None:
    async def _tie(request: httpx.Request) -> httpx.Response:
        return _scored((0, 0.5), (1, 0.5), (2, 0.9))

    _api(monkeypatch, _tie)

    assert _ids(_rerank("잔잔한 소설", _books(3))) == [3, 1, 2]


def test_응답에_없는_책은_맨_뒤에_검색_순서로_남긴다(
    monkeypatch: pytest.MonkeyPatch, reranker_on: None
) -> None:
    async def _partial(request: httpx.Request) -> httpx.Response:
        return _scored((3, 0.2), (1, 0.8))

    _api(monkeypatch, _partial)

    assert _ids(_rerank("잔잔한 소설", _books(4))) == [2, 4, 1, 3]


def test_모델_이름과_질문_책_글을_보낸다(
    monkeypatch: pytest.MonkeyPatch, reranker_on: None
) -> None:
    async def _ok(request: httpx.Request) -> httpx.Response:
        return _scored((0, 0.1), (1, 0.9))

    sent = _api(monkeypatch, _ok)
    books = [
        {"book_id": 1, "title": " 책1 ", "author": "작가", "description": "소개1"},
        # 비어 있는 칸은 빼고 잇는다
        {"book_id": 2, "title": "책2", "author": None, "description": "소개2"},
    ]

    _rerank("비 오는 날 읽을 책", books)

    (request,) = sent
    assert str(request.url) == RERANK_URL
    assert request.headers["Authorization"] == "Bearer test-key"
    assert json.loads(request.content) == {
        "model": "cohere/rerank-5-fast",
        "query": "비 오는 날 읽을 책",
        "documents": ["책1\n작가\n소개1", "책2\n소개2"],
    }


def test_받은_목록은_바꾸지_않고_새_목록을_돌려준다(
    monkeypatch: pytest.MonkeyPatch, reranker_on: None
) -> None:
    async def _ok(request: httpx.Request) -> httpx.Response:
        return _scored((0, 0.1), (1, 0.9))

    _api(monkeypatch, _ok)
    books = _books(2)

    result = _rerank("잔잔한 소설", books)

    assert _ids(result) == [2, 1]
    assert _ids(books) == [1, 2]


# --- 못 쓰면 검색 순서 --------------------------------------------------------------


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, json={"error": {"message": "비 오는 날 읽을 책"}}),
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        # 모양이 틀린 응답들
        httpx.Response(200, text="not json"),
        httpx.Response(200, json={"data": []}),
        httpx.Response(200, json={"results": []}),
        httpx.Response(200, json={"results": [{"index": 5, "relevance_score": 0.9}]}),
        httpx.Response(
            200, json={"results": [{"index": True, "relevance_score": 0.9}]}
        ),
        httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ]
            },
        ),
        httpx.Response(200, json={"results": [{"index": 0, "relevance_score": "0.9"}]}),
        httpx.Response(200, json={"results": [{"index": 0}]}),
    ],
)
def test_실패하거나_응답_모양이_틀리면_검색_순서를_쓴다(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reranker_on: None,
    response: httpx.Response,
) -> None:
    async def _bad(request: httpx.Request) -> httpx.Response:
        return response

    _api(monkeypatch, _bad)

    with caplog.at_level(logging.WARNING, logger="app.gateway.reranker"):
        result = _rerank("비 오는 날 읽을 책", _books(3))

    assert _ids(result) == [1, 2, 3]
    (warning,) = _logged(caplog, logging.WARNING)
    assert "검색 순서" in warning
    # 응답 본문은 보낸 질문을 되풀이할 수 있어 로그에 남기지 않는다(#202)
    assert "비 오는 날" not in warning


def test_연결이_실패하면_검색_순서를_쓴다(
    monkeypatch: pytest.MonkeyPatch, reranker_on: None
) -> None:
    async def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결 실패", request=request)

    _api(monkeypatch, _down)

    assert _ids(_rerank("잔잔한 소설", _books(3))) == [1, 2, 3]


def test_제한_시간을_넘기면_검색_순서를_쓴다(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reranker_on: None,
) -> None:
    monkeypatch.setenv("RERANKER_TIMEOUT_SECONDS", "0.05")
    get_settings.cache_clear()

    async def _slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return _scored((0, 0.1), (1, 0.9))

    _api(monkeypatch, _slow)

    with caplog.at_level(logging.WARNING, logger="app.gateway.reranker"):
        result = _rerank("잔잔한 소설", _books(2))

    assert _ids(result) == [1, 2]
    (warning,) = _logged(caplog, logging.WARNING)
    assert "안에 답하지 않아" in warning


# --- 부르지 않는 때 -----------------------------------------------------------------


def _forbid_http(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _never(request: httpx.Request) -> httpx.Response:
        raise AssertionError("리랭커를 부르면 안 된다")

    _api(monkeypatch, _never)


def test_기본값은_켜져_있어_다시_줄_세운다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTERNAL_AI_API_KEY", "test-key")
    monkeypatch.setenv("LLM_PROVIDER", "openrouter-no-zdr")
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.delenv("CHAT_RERANKER", raising=False)
    get_settings.cache_clear()

    async def _ok(request: httpx.Request) -> httpx.Response:
        return _scored((0, 0.1), (1, 0.9), (2, 0.5))

    sent = _api(monkeypatch, _ok)

    assert reranker.enabled() is True
    assert _ids(_rerank("잔잔한 소설", _books(3))) == [2, 3, 1]
    assert len(sent) == 1


def test_끄면_부르지_않는다(monkeypatch: pytest.MonkeyPatch, reranker_on: None) -> None:
    monkeypatch.setenv("CHAT_RERANKER", "false")
    get_settings.cache_clear()
    _forbid_http(monkeypatch)

    assert _ids(_rerank("잔잔한 소설", _books(3))) == [1, 2, 3]
    assert reranker.enabled() is False


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"LLM_PROVIDER": "openrouter"}, "ZDR"),
        ({"EXTERNAL_AI_API_KEY": ""}, "EXTERNAL_AI_API_KEY"),
        ({"LLM_MOCK": "true"}, None),
    ],
)
def test_ZDR_설정이거나_키가_없거나_가짜_LLM이면_켜도_부르지_않는다(
    monkeypatch: pytest.MonkeyPatch,
    reranker_on: None,
    env: dict[str, str],
    reason: str | None,
) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    _forbid_http(monkeypatch)

    assert _ids(_rerank("잔잔한 소설", _books(3))) == [1, 2, 3]
    assert reranker.enabled() is False
    skip = reranker.skip_reason(get_settings())
    assert (skip is None) if reason is None else (reason in skip)


@pytest.mark.parametrize(
    ("query", "count"), [("", 3), ("잔잔한 소설", 1), ("잔잔한 소설", 0)]
)
def test_질문이_없거나_후보가_한_권_이하면_부르지_않는다(
    monkeypatch: pytest.MonkeyPatch, reranker_on: None, query: str, count: int
) -> None:
    _forbid_http(monkeypatch)
    books = _books(count)

    assert _rerank(query, books) is books


# --- 서버가 뜰 때 남기는 줄 ---------------------------------------------------------


def _models_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL_ID", "shared-model")
    monkeypatch.setenv("EXTRACTION_LLM_MODEL_ID", "extraction-model")
    monkeypatch.setenv("AGENT_LLM_MODEL_ID", "agent-model")
    monkeypatch.setenv("RELATION_LLM_MODEL_ID", "relation-model")
    monkeypatch.setenv("EXTRACTION_SELF_HOSTED_MODE", "off")


def test_켜면_시작_로그에_리랭커_모델을_남긴다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reranker_on: None
) -> None:
    _models_env(monkeypatch)

    with caplog.at_level(logging.INFO, logger="app.gateway.llm"):
        report_models()

    assert _logged(caplog, logging.WARNING) == []
    (info,) = _logged(caplog, logging.INFO)
    assert "③ 후보 리랭커 cohere/rerank-5-fast" in info


def test_켰는데_ZDR_설정이면_검색_순서로_자른다고_경고한다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reranker_on: None
) -> None:
    _models_env(monkeypatch)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")

    with caplog.at_level(logging.INFO, logger="app.gateway.llm"):
        report_models()

    (warning,) = _logged(caplog, logging.WARNING)
    assert "CHAT_RERANKER" in warning
    assert "ZDR" in warning
    (info,) = _logged(caplog, logging.INFO)
    assert "리랭커" not in info


def test_꺼져_있으면_시작_로그에_리랭커가_없다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _models_env(monkeypatch)
    monkeypatch.setenv("LLM_MOCK", "false")
    monkeypatch.setenv("CHAT_RERANKER", "false")

    with caplog.at_level(logging.INFO, logger="app.gateway.llm"):
        report_models()

    assert all("리랭커" not in m for m in _logged(caplog, logging.INFO))
    assert all("CHAT_RERANKER" not in m for m in _logged(caplog, logging.WARNING))
