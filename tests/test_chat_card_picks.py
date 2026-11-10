"""③ 카드 JSON 을 picks → reply → cards 순서로 받는다.

말풍선(reply)을 카드 이유보다 먼저 쓰게 해 SSE 가 말풍선을 먼저 보낸다. 책은 말풍선보다 먼저 고르게 해(picks) 말한
책과 카드가 어긋나지 않게 한다(#540). 화면 차례는 picks 를 따르고, picks 가 틀려도 카드를 버리지 않는다.
"""

import asyncio
import json

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.chat.schemas import Spec
from app.routers import chat

SPEC = Spec.model_validate(
    {
        "intent": "semantic",
        "exact": {"title": None, "author": None, "publisher": None},
        "filters": {},
        "semantic": None,
        "anchor_book": None,
        "exclude": [],
    }
)


def _book(book_id: int) -> dict:
    return {
        "book_id": book_id,
        "isbn": str(9780000000000 + book_id),
        "title": f"책{book_id}",
        "author": "작가",
        "description": f"책{book_id} 소개.",
        "price": 10000,
        "cover_url": None,
        "match_score": 50,
        "popularity": 0.0,
    }


def _card(book_id: int) -> dict:
    return {
        "book_id": book_id,
        "reason_short": f"책{book_id} 한 줄 이유예요.",
        "reason_long": f"찾으신 것과 책{book_id}가 맞아요.",
        "match_basis": [],
    }


def _generate(answer: dict, on_reply=None) -> chat.CardResult:
    model = GenericFakeChatModel(
        messages=iter([AIMessage(content=json.dumps(answer, ensure_ascii=False))])
    )
    original = chat.get_chat_model
    chat.get_chat_model = lambda: model
    try:
        return asyncio.run(
            chat.generate_cards(
                [_book(1), _book(2), _book(3)],
                SPEC,
                message="책 추천해줘",
                on_reply=on_reply,
            )
        )
    finally:
        chat.get_chat_model = original


def _isbns(result: chat.CardResult) -> list[str]:
    return [card["isbn"][-1] for card in result.cards]


def test_화면_차례는_picks를_따른다() -> None:
    result = _generate(
        {"picks": [3, 1], "reply": "골라 봤어요.", "cards": [_card(1), _card(3)]}
    )

    assert _isbns(result) == ["3", "1"]
    assert [card["rank"] for card in result.cards] == [1, 2]


@pytest.mark.parametrize(
    "picks",
    [
        pytest.param(None, id="picks_없음"),
        pytest.param("3,1", id="목록_아님"),
        pytest.param([], id="빈_목록"),
        pytest.param(["3", True], id="정수_아님"),
    ],
)
def test_picks가_틀리면_cards_차례를_쓴다(picks: object) -> None:
    result = _generate(
        {"picks": picks, "reply": "골라 봤어요.", "cards": [_card(2), _card(1)]}
    )

    assert _isbns(result) == ["2", "1"]


def test_picks에_없는_카드는_버리지_않고_뒤에_둔다() -> None:
    result = _generate(
        {"picks": [2, 9], "reply": "골라 봤어요.", "cards": [_card(1), _card(2)]}
    )

    assert _isbns(result) == ["2", "1"]


def test_SSE는_카드_이유를_받기_전에_말풍선을_보낸다() -> None:
    answer = {"picks": [1], "reply": "잔잔한 결로 골라 봤어요.", "cards": [_card(1)]}
    raw = json.dumps(answer, ensure_ascii=False)
    sent_at: list[int] = []
    fed = ""

    async def sink(text: str) -> None:
        sent_at.append(len(fed))

    original_stream = chat.stream_json

    async def watch_stream(chain, inputs, on_text):
        async def track(text: str) -> None:
            nonlocal fed
            fed = text
            await on_text(text)

        return await original_stream(chain, inputs, track)

    chat.stream_json = watch_stream
    try:
        result = _generate(answer, on_reply=sink)
    finally:
        chat.stream_json = original_stream

    assert len(sent_at) == 1
    assert sent_at[0] < raw.index('"cards"') + len('"cards"')
    assert _isbns(result) == ["1"]


def test_프롬프트는_picks_reply_cards_순서로_답하게_한다() -> None:
    prompt = chat.CARD_PROMPT.format(
        recent_turns="", message="", intro="", limit=3, listing="", turn_end=""
    )
    example = prompt[prompt.index("다음 JSON 형식으로만") :]

    assert (
        example.index('"picks"') < example.index('"reply"') < example.index('"cards"')
    )
