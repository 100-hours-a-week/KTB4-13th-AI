"""추천 채팅 부하 테스트 mock의 범위와 정상 후보 처리를 확인한다."""

import asyncio

import pytest

from app.chat.schemas import Spec
from app.core.config import get_settings
from app.gateway import llm
from app.routers import chat


@pytest.fixture
def enabled(monkeypatch):
    settings = get_settings().model_copy(update={"chat_load_test_mock": True})
    monkeypatch.setattr(chat, "get_settings", lambda: settings)
    monkeypatch.setattr(llm, "get_settings", lambda: settings)
    return settings


def test_external_llm_is_blocked(enabled):
    with pytest.raises(llm.LLMUnavailableError):
        llm.get_chat_model()


def test_mock_continues_existing_request_without_restarting(enabled, monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("continuation must not reset and re-extract conditions")

    monkeypatch.setattr(chat, "_restart_patch", forbidden)
    spec = Spec.model_validate(
        {
            "intent": "semantic",
            "exact": {},
            "filters": {"price_max": 20000},
            "semantic": "잔잔한 소설",
            "anchor_book": None,
            "exclude": [],
        }
    )
    assert (
        asyncio.run(chat._asks_new_request("여행 중 읽을 소설", "잔잔한 소설")) is False
    )
    result = asyncio.run(chat.update_spec("여행 중 읽을 소설", spec, []))
    assert not result.degraded
    assert result.spec.filters.price_max == 20000
    assert result.spec.semantic == "여행 중 읽을 소설"


@pytest.mark.parametrize("preview", [False, True])
def test_mock_uses_real_candidates_and_does_not_call_llm(enabled, monkeypatch, preview):
    def forbidden(*args, **kwargs):
        raise AssertionError("external LLM must not be called")

    monkeypatch.setattr(chat, "get_chat_model", forbidden)
    spec = Spec.model_validate(
        {
            "intent": "semantic",
            "exact": {},
            "filters": {},
            "semantic": None,
            "anchor_book": None,
            "exclude": [],
        }
    )
    patch = asyncio.run(chat._ask_spec_patch("잔잔한 소설 추천해줘", spec, []))
    assert patch["semantic"] == "잔잔한 소설 추천해줘"
    candidates = [
        {
            "book_id": 102,
            "title": "테스트 책",
            "author": "저자",
            "description": "책 소개",
            "match_score": 80,
        }
    ]
    previews = []

    async def on_reply(text):
        previews.append(text)

    result = asyncio.run(
        chat.generate_cards(candidates, spec, on_reply=on_reply if preview else None)
    )
    assert not result.degraded
    assert [card["book_id"] for card in result.cards] == [102]
    assert bool(previews) == preview
