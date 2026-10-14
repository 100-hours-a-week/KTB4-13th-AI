"""③ 소개글이 없는 책의 카드 이유(#86) — 책 내용을 지어내지 않는다.

소개글 없는 책은 exact 요청(제목·저자로 콕 집어 찾음)에서만 후보에 남는다(semantic 은 후보 단계에서 뺀다).
카드 생성 프롬프트는 이 책을 소개글 칸을 비운 채 넘기고, 모델은 소개글이 없다는 것을 모르고 제목·저자만 보고
긴 이유를 지어냈다(평가 32번: 책소개 없는 카드 14장 전부에 긴 이유가 채워짐).

여기서는 DB 와 LLM 을 가짜로 바꿔 끼우고, 소개글이 없는 책에 대해 다음을 본다.
- 프롬프트가 "소개글 없음"이라고 알린다
- 모델이 무엇을 쓰든 긴 이유는 null, 근거(match_basis)는 빈 배열, 한 줄 이유는 제목·저자만 쓴 고정 문장이다
- 소개글이 있는 책의 카드는 그대로다
"""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.main import app
from app.routers import chat

client = TestClient(app)

INITIAL_SPEC = {
    "intent": "exact",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}

WITH_DESCRIPTION = {
    "book_id": 1,
    "title": "달러구트 꿈 백화점",
    "author": "이미예",
    "description": "잠든 사이 꿈을 사고파는 상점 이야기.",
    "match_score": 25,
    "popularity": 3.0,
}
NO_DESCRIPTION = {
    "book_id": 2,
    "title": "토지",
    "author": "박경리",
    "description": "",
    "match_score": 24,
    "popularity": 3.0,
}


def _request(**overrides) -> dict:
    body = {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": "토지 찾아줘",
        "recent_turns": [],
        "exclude_book_ids": [],
        "image_ref": None,
    }
    body.update(overrides)
    return body


def _spec_reply() -> str:
    return json.dumps({"exact": {"title": "토지"}}, ensure_ascii=False)


def _use_candidates(monkeypatch: pytest.MonkeyPatch, *books: dict) -> None:
    async def _fake(spec, exclude_book_ids, user_id, owned_book_ids=None) -> list[dict]:
        return [dict(b) for b in books]

    monkeypatch.setattr(chat, "get_candidates", _fake)


def _use_model(
    monkeypatch: pytest.MonkeyPatch, card_reply: str, sent: list[str] | None = None
) -> None:
    """1단계는 고정 답으로 통과시키고, 2번째 호출(카드 생성)의 프롬프트를 sent 에 담은 뒤 card_reply 를 준다."""
    calls = 0

    def _dispatch(prompt_value) -> AIMessage:
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(content=_spec_reply())
        if sent is not None:
            sent.append(prompt_value.to_messages()[0].content)
        return AIMessage(content=card_reply)

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))


def _post() -> dict:
    res = client.post("/recommendations/chat", json=_request())
    assert res.status_code == 200
    return res.json()["data"]


def test_소개글이_없는_책은_프롬프트에_소개글_없음으로_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    _use_candidates(monkeypatch, WITH_DESCRIPTION, NO_DESCRIPTION)
    _use_model(monkeypatch, '{"cards": []}', sent)

    _post()

    prompt = sent[0]
    assert "- book_id 2: 토지 - 박경리 - (소개글 없음)" in prompt
    # 소개글이 있는 책은 그대로 실린다.
    assert (
        "- book_id 1: 달러구트 꿈 백화점 - 이미예 - 잠든 사이 꿈을 사고파는 상점 이야기."
        in prompt
    )


def test_공백뿐인_소개글도_없는_것으로_본다(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    _use_candidates(monkeypatch, {**NO_DESCRIPTION, "description": "  \n "})
    _use_model(monkeypatch, '{"cards": []}', sent)

    _post()

    assert "- book_id 2: 토지 - 박경리 - (소개글 없음)" in sent[0]


def test_카드_프롬프트가_소개글_없는_책의_내용을_말하지_말라고_알린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    _use_candidates(monkeypatch, NO_DESCRIPTION)
    _use_model(monkeypatch, '{"cards": []}', sent)

    _post()

    assert "(소개글 없음)인 책" in sent[0]
    assert "지어내지 마라" in sent[0]


def test_소개글_없는_책은_모델이_긴_이유를_써도_null이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_reply = json.dumps(
        {
            "reply": "토지를 찾았어요.",
            "cards": [
                {
                    "book_id": 2,
                    "reason_short": "한국의 농촌과 삶을 깊이 있게 탐구한 대하소설이에요.",
                    "reason_long": "이야기의 중반부를 담당하는 인물들의 갈등을 그립니다.",
                    "match_basis": [{"label": "주제", "detail": "농촌과 삶"}],
                }
            ],
        },
        ensure_ascii=False,
    )
    _use_candidates(monkeypatch, NO_DESCRIPTION)
    _use_model(monkeypatch, card_reply)

    card = _post()["cards"][0]

    assert card["reason_long"] is None


def test_소개글_없는_책의_한_줄_이유는_제목_저자만_쓴_문장이고_근거는_비운다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_reply = json.dumps(
        {
            "cards": [
                {
                    "book_id": 2,
                    "reason_short": "한국의 농촌과 삶을 깊이 있게 탐구한 대하소설이에요.",
                    "reason_long": "지어낸 긴 이유",
                    "match_basis": [{"label": "주제", "detail": "농촌과 삶"}],
                }
            ],
        },
        ensure_ascii=False,
    )
    _use_candidates(monkeypatch, NO_DESCRIPTION)
    _use_model(monkeypatch, card_reply)

    card = _post()["cards"][0]

    assert card["reason_short"] == "찾으신 박경리의 『토지』이에요."
    assert card["match_basis"] == []


def test_저자가_비어_있으면_제목만_쓴다(monkeypatch: pytest.MonkeyPatch) -> None:
    card_reply = json.dumps(
        {"cards": [{"book_id": 2, "reason_short": "지어낸 이유"}]}, ensure_ascii=False
    )
    _use_candidates(monkeypatch, {**NO_DESCRIPTION, "author": ""})
    _use_model(monkeypatch, card_reply)

    card = _post()["cards"][0]

    assert card["reason_short"] == "찾으신 『토지』이에요."


def test_모델이_소개글_없는_책의_한_줄_이유를_빠뜨려도_카드는_남는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 소개글 없는 책의 이유는 모델이 쓴 것이 아니라 규칙이 쓴 것이라, 모델이 비워도 카드를 뺄 까닭이 없다.
    card_reply = json.dumps({"cards": [{"book_id": 2}]}, ensure_ascii=False)
    _use_candidates(monkeypatch, NO_DESCRIPTION)
    _use_model(monkeypatch, card_reply)

    cards = _post()["cards"]

    assert [c["book_id"] for c in cards] == [2]
    assert cards[0]["reason_short"] == "찾으신 박경리의 『토지』이에요."


def test_소개글이_있는_책의_카드는_모델이_쓴_그대로다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_reply = json.dumps(
        {
            "cards": [
                {
                    "book_id": 1,
                    "reason_short": "꿈을 파는 가게 이야기예요.",
                    "reason_long": "잠든 사이 꿈을 사고파는 상점을 그린 소설입니다.",
                    "match_basis": [{"label": "분위기", "detail": "잔잔함"}],
                },
                {"book_id": 2, "reason_short": "지어낸 이유", "reason_long": "지어낸"},
            ],
        },
        ensure_ascii=False,
    )
    _use_candidates(monkeypatch, WITH_DESCRIPTION, NO_DESCRIPTION)
    _use_model(monkeypatch, card_reply)

    cards = _post()["cards"]

    with_description, without_description = cards
    assert with_description["reason_short"] == "꿈을 파는 가게 이야기예요."
    assert (
        with_description["reason_long"]
        == "잠든 사이 꿈을 사고파는 상점을 그린 소설입니다."
    )
    assert with_description["match_basis"] == [{"label": "분위기", "detail": "잔잔함"}]
    assert without_description["reason_long"] is None
