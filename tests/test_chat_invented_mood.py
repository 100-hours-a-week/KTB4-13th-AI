"""③ 검색 문장에서 사용자가 말하지 않은 분위기 낱말을 뺀다(#543, app/chat/invented_mood.py).

1단계가 "에세이 추천해줘"를 "가볍고 재밌는 에세이"로 바꾸면, 말풍선이 "가벼운 에세이를 찾으신다면"이라고 사용자가 안 한
말을 되읊고 검색도 가벼운 책으로 쏠린다. 아래 검색 문장은 대부분 10/21 평가 원본(gpt-4o-mini)에서 실제로 나온 것이다.
"""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat import invented_mood
from app.chat.schemas import Spec, Turn
from app.chat.shown import ShownBook
from app.main import app
from app.routers import chat

client = TestClient(app)


def _strip(semantic: str | None, message: str, *, before: list[Turn] | None = None, previous: str | None = None, shown=()) -> str | None:  # fmt: skip
    grounds = invented_mood.evidence(message, before or [], previous, list(shown))
    return invented_mood.strip(semantic, grounds)[0]


@pytest.mark.parametrize(
    ("message", "semantic", "expected"),
    [
        ("에세이 추천해줘", "가볍고 재밌는 에세이", "에세이"),
        ("역사책 읽고 싶어", "역사에 대한 흥미로운 책", "역사에 대한 책"),
        ("소설 추천해줘", "무겁지 않은 재밌는 소설 추천해줘", "소설 추천해줘"),
        # 딸린 말("분위기의")과 정도를 정하는 말("조금 더")도 같이 뺀다
        (
            "요즘 회사 일이 너무 힘들어",
            "요즘 회사 일이 힘들어도 가벼운 분위기의 책",
            "요즘 회사 일이 힘들어도 책",
        ),
        (
            "요즘 회사 일이 너무 힘들어",
            "요즘 회사 일이 너무 힘들어, 조금 더 가벼운 책 추천해줘",
            "요즘 회사 일이 너무 힘들어, 책 추천해줘",
        ),
        (
            "자기계발서 추천해줘",
            "자기계발서를 추천해 줘, 너무 무겁지 않은 걸로",
            "자기계발서를 추천해 줘",
        ),
        # "아무거나"의 "무거"는 무거운 쪽을 싫다는 말이 아니다
        ("아무거나", "무겁지 않은 소설", "소설"),
    ],
)
def test_사용자가_말하지_않은_분위기_낱말을_뺀다(
    message: str, semantic: str, expected: str
) -> None:
    assert _strip(semantic, message) == expected


@pytest.mark.parametrize(
    ("message", "semantic"),
    [
        ("따뜻한 시집 찾고 있어요", "따뜻한 분위기의 시집"),
        # 같은 쪽 뜻을 말했으면 남긴다
        ("좀 덜 무거운 걸로", "덜 무겁고 가벼운 소설"),
        ("위로되는 책 추천해줘", "위로가 되는 따뜻한 책"),
        ("2번은 좀 비극적인 내용 같아", "범인을 쫓는 미스터리 소설을 무겁지 않게"),
        ("재밌는 소설 없어?", "흥미로운 소설"),
        # 분위기 낱말이 없으면 건드리지 않는다
        ("돈 공부 하고 싶어", "돈에 대한 지식을 쌓고 싶어하는 책"),
    ],
)
def test_사용자가_말한_분위기는_남긴다(message: str, semantic: str) -> None:
    assert _strip(semantic, message) == semantic


def test_앞선_턴의_사용자_말도_근거로_본다() -> None:
    before = [
        Turn(role="user", text="가벼운 에세이 추천해줘"),
        Turn(role="assistant", text="퇴근길에 읽기 좋은 에세이를 골랐어요."),
    ]

    assert _strip("가벼운 에세이", "2만원 이하로", before=before) == "가벼운 에세이"


def test_앞_검색_문장에_있던_분위기는_남긴다() -> None:
    # 들어온 턴에 이미 검사했다. 최근 대화는 20턴까지만 와서, 그 앞의 사용자 말은 없을 수 있다
    semantic = "퇴근길에 가볍게 읽을 소설"

    assert _strip(semantic, "2만원 이하 아니어도 돼", previous=semantic) == semantic


def test_챗봇_말은_근거로_보지_않는다() -> None:
    # 지어낸 낱말을 말풍선이 되읊는다. 챗봇 말을 근거로 보면 한 번 지어낸 낱말이 계속 남는다.
    before = [
        Turn(role="user", text="에세이 추천해줘"),
        Turn(role="assistant", text="가벼운 에세이를 골라봤어요."),
    ]

    assert _strip("가벼운 에세이", "2만원 이하로", before=before) == "에세이"


def _book(rank: int, description: str) -> ShownBook:
    return ShownBook(
        rank=rank, book_id=rank, title=f"책{rank}", author=None, description=description
    )


def test_가리킨_카드의_책_소개도_근거로_본다() -> None:
    shown = [_book(1, "역사를 다룬 교양서"), _book(2, "가볍게 읽는 직장 에세이")]
    semantic = "가볍게 읽는 직장인 에세이"

    assert _strip(semantic, "2번 같은 거 더 보여줘", shown=shown) == semantic
    assert _strip(semantic, "이거 같은 거 더 보여줘", shown=shown) == semantic
    # 가리키지 않은 책의 소개는 근거가 아니다
    assert (
        _strip(semantic, "1번 같은 거 더 보여줘", shown=shown) == "읽는 직장인 에세이"
    )


def test_빼면_주제가_안_남으면_건드리지_않는다() -> None:
    # 비우면 대화 한가운데서 분야부터 다시 묻게 된다
    semantic = "더 재밌는 걸로 추천해줘"

    assert _strip(semantic, "다른 거 보여줘") == semantic
    assert (
        _strip("더 가볍고 재밌는 추천이 필요해", "다른 거")
        == "더 가볍고 재밌는 추천이 필요해"
    )


def test_검색_문장이_없으면_그대로다() -> None:
    assert invented_mood.strip(None, "에세이 추천해줘") == (None, 0)


INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}


def test_뺀_검색_문장으로_후보를_찾고_응답_spec에_싣는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Spec] = []

    async def _fake_candidates(spec, exclude_book_ids, user_id, owned_book_ids=None):
        seen.append(spec)
        return []

    monkeypatch.setattr(chat, "get_candidates", _fake_candidates)
    patch = {"intent": "semantic", "semantic": "가볍고 재밌는 에세이"}
    model = RunnableLambda(
        lambda _p: AIMessage(content=json.dumps(patch, ensure_ascii=False))
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post(
        "/recommendations/chat",
        json={
            "user_id": 123,
            "consented": True,
            "spec": INITIAL_SPEC,
            "message": "에세이 추천해줘",
            "recent_turns": [],
            "exclude_book_ids": [],
            "image_ref": None,
        },
    )

    assert res.status_code == 200
    assert seen[0].semantic == "에세이"
    assert res.json()["data"]["spec"]["semantic"] == "에세이"  # 다음 턴으로 이어진다
