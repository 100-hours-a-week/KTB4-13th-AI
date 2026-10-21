"""③ 카드 턴 말풍선이 사용자에게 차례를 넘긴다(#545).

10/21 평가 원본에서 카드 턴 말풍선이 다음 말을 건넨 적이 0/39였다. 말풍선의 마지막 문장이 갈리는 점을 묻거나 한 권을
짚어 의견을 말하게 하고, 연달아 묻지 않게 직전 챗봇 말이 질문이었으면 의견만 말하게 한다. 첫 턴에 코드가 질문을 붙이는
요청(선물·나이·예산·최근)도 말풍선은 묻지 않는다. 한 번에 두 개를 묻지 않게.
"""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.agent import prompts as agent_prompts
from app.chat.schemas import Turn
from app.core import voice
from app.main import app
from app.routers import chat

client = TestClient(app)

INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": "비 오는 날 읽을 책",
    "anchor_book": None,
    "exclude": [],
}


@pytest.fixture(autouse=True)
def fake_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake(spec, exclude_book_ids, user_id, owned_book_ids=None) -> list[dict]:
        return [
            {
                "book_id": 1088,
                "title": "달러구트 꿈 백화점",
                "author": "이미예",
                "description": "잠든 사이 꿈을 사고파는 상점 이야기.",
                "match_score": 25,
                "popularity": 3.0,
            }
        ]

    monkeypatch.setattr(chat, "get_candidates", _fake)


def _card_prompt(
    monkeypatch: pytest.MonkeyPatch, message: str, recent_turns: list[dict]
) -> str:
    """요청을 보내고 카드 생성 호출에 간 프롬프트를 돌려준다. 다른 호출에는 빈 답을 준다."""
    sent: list[str] = []

    def _dispatch(prompt_value) -> AIMessage:
        content = prompt_value.to_messages()[0].content
        if "reply는 말풍선에 보일" in content:
            sent.append(content)
            return AIMessage(content='{"cards": []}')
        return AIMessage(content=json.dumps({"semantic": "비 오는 날 읽을 책"}))

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))
    res = client.post(
        "/recommendations/chat",
        json={
            "user_id": 123,
            "consented": True,
            "spec": INITIAL_SPEC,
            "message": message,
            "recent_turns": recent_turns,
            "exclude_book_ids": [],
            "image_ref": None,
        },
    )
    assert res.status_code == 200
    return sent[0]


def test_첫_턴_말풍선은_차례를_넘긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    prompt = _card_prompt(monkeypatch, "비 오는 날 읽을 책 추천해줘", [])

    assert chat._HAND_OVER in prompt
    assert chat._POINT_ONE not in prompt


def test_직전에_물었으면_묻지_않고_한_권을_짚는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = [
        {"role": "user", "text": "비 오는 날 읽을 책 추천해줘"},
        {
            "role": "assistant",
            "text": "꿈 이야기로 골랐어요. 이야기 쪽과 에세이 쪽 중 어느 쪽이 맞으세요?",
        },
    ]

    prompt = _card_prompt(monkeypatch, "이야기 쪽이요", turns)

    assert chat._POINT_ONE in prompt
    assert chat._HAND_OVER not in prompt


def test_직전_챗봇_말이_질문이_아니면_다시_차례를_넘긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = [
        {"role": "user", "text": "비 오는 날 읽을 책 추천해줘"},
        {"role": "assistant", "text": "1번이 제일 잘 맞아 보여요."},
    ]

    prompt = _card_prompt(monkeypatch, "다른 것도 보여줘", turns)

    assert chat._HAND_OVER in prompt


def test_첫_턴에_코드가_질문을_붙이면_말풍선은_묻지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "선물"은 받는 분을 묻는 질문을 코드가 말풍선 끝에 붙인다(_SLOT_RULES)
    prompt = _card_prompt(monkeypatch, "선물할 책 추천해줘", [])

    assert chat._POINT_ONE in prompt


@pytest.mark.parametrize(
    ("turns", "asked"),
    [
        ([], False),
        ([Turn(role="user", text="책 추천해줘?")], False),
        ([Turn(role="assistant", text="이런 쪽이 맞으세요?")], True),
        ([Turn(role="assistant", text="골라봤어요.")], False),
    ],
)
def test_직전_챗봇_말이_질문인지_본다(turns: list[Turn], asked: bool) -> None:
    assert chat._bot_asked_last(turns) is asked


def test_카드_대답_쇼핑_프롬프트가_같은_말투_규칙을_쓴다() -> None:
    # 한 대화 안에서 목소리가 여럿이었다(#545). 세 프롬프트에 같은 글이 들어간다
    card = chat.CARD_PROMPT.format(
        recent_turns="", message="", intro="", limit=3, listing="", turn_end=""
    )
    answer = chat.ANSWER_PROMPT.format(recent_turns="", shown="", message="")

    assert voice.RULES in card
    assert voice.RULES in answer
    assert voice.RULES in agent_prompts.SYSTEM_PROMPT
    assert "한국어 존댓말로" not in agent_prompts.SYSTEM_PROMPT


def test_말투_규칙에_베낄_값이_없다() -> None:
    # 예시에 낱말·값을 적으면 모델이 베낀다(#410). 분위기 낱말도, 가격 같은 숫자도 적지 않는다
    for word in ("잔잔", "따뜻", "가벼", "편안", "재미", "감동"):
        assert word not in voice.RULES
    assert not any(ch.isdigit() for ch in voice.RULES)


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            "2만원 이하의 추리 소설을 찾으셨군요. 그중 『지워진 일주일』이 제일 맞아 보여요.",
            "그중 『지워진 일주일』이 제일 맞아 보여요.",
        ),
        (
            "추리 소설을 원하셨군요! 입문 쪽이 좋으세요, 정통 쪽이 좋으세요?",
            "입문 쪽이 좋으세요, 정통 쪽이 좋으세요?",
        ),
        (
            "주식 입문서를 찾으셨네요. 처음이면 1권부터 보세요.",
            "처음이면 1권부터 보세요.",
        ),
        # 사정에 답하는 말은 되풀이가 아니다
        (
            "요즘 많이 지치셨군요. 쉬어 가며 읽을 책으로 골랐어요.",
            "요즘 많이 지치셨군요. 쉬어 가며 읽을 책으로 골랐어요.",
        ),
        # 그 문장뿐이면 남긴다. 말풍선이 비면 안 된다
        ("파이썬 입문서를 찾으셨군요.", "파이썬 입문서를 찾으셨군요."),
        # 가정하는 말은 되풀이가 아니다
        (
            "입문서를 원하신다면 이 책들이 좋아요.",
            "입문서를 원하신다면 이 책들이 좋아요.",
        ),
    ],
)
def test_요청을_되풀이하는_첫_문장은_뺀다(reply: str, expected: str) -> None:
    assert voice.without_echo(reply) == expected


def test_카드_말풍선에서_되풀이_문장을_뺀다(monkeypatch: pytest.MonkeyPatch) -> None:
    card_reply = json.dumps(
        {
            "cards": [{"book_id": 1088, "reason_short": "꿈을 사고파는 이야기예요."}],
            "reply": "비 오는 날 읽을 책을 찾으셨군요. 『달러구트 꿈 백화점』이 제일 맞아 보여요.",
        },
        ensure_ascii=False,
    )

    def _dispatch(prompt_value) -> AIMessage:
        content = prompt_value.to_messages()[0].content
        if "reply는 말풍선에 보일" in content:
            return AIMessage(content=card_reply)
        return AIMessage(content=json.dumps({"semantic": "비 오는 날 읽을 책"}))

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))
    res = client.post(
        "/recommendations/chat",
        json={
            "user_id": 123,
            "consented": True,
            "spec": INITIAL_SPEC,
            "message": "비 오는 날 읽을 책 추천해줘",
            "recent_turns": [],
            "exclude_book_ids": [],
            "image_ref": None,
        },
    )

    assert res.json()["data"]["reply"] == "『달러구트 꿈 백화점』이 제일 맞아 보여요."
