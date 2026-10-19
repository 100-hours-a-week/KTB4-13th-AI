"""③ 긴 이유가 책 소개를 옮기지 않게 한다(#503).

긴 이유(reason_long)는 도서 상세 페이지에 책 소개와 같이 보인다. 운영에서 『물감을 사야 해서, 퇴사는 잠시
미뤘습니다』의 긴 이유가 책 소개와 한 낱말만 달라 같은 글이 두 번 보이게 됐다.

여기서는 DB 와 LLM 을 가짜로 바꿔 끼우고 다음을 본다.
- 옮긴 정도(copied_share)가 운영에서 본 글은 옮긴 것으로, 요청과 이은 글은 아닌 것으로 잰다
- 모델이 책 소개를 옮긴 긴 이유를 쓰면 그 카드의 긴 이유만 null 이고, 한 줄 이유·근거·카드는 그대로다
- 프롬프트가 긴 이유를 요청과 이으라고 하되, 책 소개가 없는 책을 따로 짚지 않는다(#86)
"""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat.copying import COPIED_SHARE_MIN, copied_share
from app.main import app
from app.routers import chat

client = TestClient(app)

# 운영 실측(2026-10-19 dev, "퇴근길에 가볍게 읽을 에세이 추천해줘")의 책 소개와 긴 이유.
DESCRIPTION = (
    "한 소심하고 평범한 직장인이 퇴근 후에 그림을 배우면서 발견한 인생의 아름답고 빛나는 순간들을 "
    "때로는 열정적으로 때로는 소박하게 기록한 에세이. 현재 살고 있는 삶을 포기하지 않더라도 충분히 "
    "나 자신만의 일상을 온전히 회복하고 더욱 충만한 느낌으로 살아내는 법을 알려준다."
)
COPIED = (
    "한 소심하고 평범한 직장인이 퇴근 후에 그림을 배우면서 발견한 인생의 아름답고 빛나는 순간들을 "
    "때로는 열정적으로 때로는 소박하게 기록한 에세이. 현재 살고 있는 삶을 포기하지 않더라도 "
    "나 자신만의 일상을 온전히 회복하고 더욱 충만한 느낌으로 살아내는 법을 알려준다."
)
LINKED = (
    "퇴근길에 가볍게 읽을 에세이를 찾으셨죠. 평범한 직장인이 퇴근 후에 그림을 배우며 겪은 순간들을 "
    "기록한 책이라, 하루를 마치고 돌아가는 길에 공감하며 읽기 좋아요."
)

BOOK = {
    "book_id": 616964,
    "title": "물감을 사야 해서, 퇴사는 잠시 미뤘습니다",
    "author": "박지은",
    "description": DESCRIPTION,
    "match_score": 70,
    "popularity": 3.0,
}
OTHER = {
    "book_id": 2,
    "title": "달러구트 꿈 백화점",
    "author": "이미예",
    "description": "잠든 사이 꿈을 사고파는 상점 이야기.",
    "match_score": 60,
    "popularity": 3.0,
}


def test_운영에서_본_긴_이유는_책_소개를_옮긴_것으로_잰다() -> None:
    assert copied_share(COPIED, DESCRIPTION) >= COPIED_SHARE_MIN


def test_요청과_이은_긴_이유는_책_소개의_낱말을_써도_옮긴_것이_아니다() -> None:
    assert copied_share(LINKED, DESCRIPTION) < COPIED_SHARE_MIN


def test_띄어쓰기와_기호만_다르면_옮긴_것이다() -> None:
    assert (
        copied_share("잠든사이, 꿈을 사고 파는 상점 이야기", OTHER["description"])
        == 1.0
    )


def test_짧거나_책_소개가_없으면_0이다() -> None:
    assert copied_share("짧은 글", DESCRIPTION) == 0.0
    assert copied_share(COPIED, "") == 0.0
    assert copied_share(None, DESCRIPTION) == 0.0


# --- 카드 생성: 옮긴 긴 이유는 버린다 ---

INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}


def _use(monkeypatch: pytest.MonkeyPatch, cards: list[dict]) -> list[str]:
    """후보는 BOOK·OTHER, 1단계는 고정 답, 카드 생성은 cards 를 돌려준다. 카드 생성 프롬프트를 담아 돌려준다."""

    async def _candidates(spec, exclude_book_ids, user_id, owned_book_ids=None):
        return [dict(BOOK), dict(OTHER)]

    sent: list[str] = []
    calls = 0

    def _dispatch(prompt_value) -> AIMessage:
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(
                content=json.dumps(
                    {"intent": "semantic", "semantic": "퇴근길에 가볍게 읽을 에세이"},
                    ensure_ascii=False,
                )
            )
        sent.append(prompt_value.to_messages()[0].content)
        return AIMessage(
            content=json.dumps(
                {"reply": "골라봤어요.", "cards": cards}, ensure_ascii=False
            )
        )

    monkeypatch.setattr(chat, "get_candidates", _candidates)
    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))
    return sent


def _post() -> list[dict]:
    res = client.post(
        "/recommendations/chat",
        json={
            "user_id": 123,
            "consented": True,
            "spec": INITIAL_SPEC,
            "message": "퇴근길에 가볍게 읽을 에세이 추천해줘",
            "recent_turns": [],
            "exclude_book_ids": [],
            "image_ref": None,
        },
    )
    assert res.status_code == 200
    return res.json()["data"]["cards"]


def _card(book_id: int, reason_long: str) -> dict:
    return {
        "book_id": book_id,
        "reason_short": "퇴근 후의 일상을 다룬 에세이예요.",
        "reason_long": reason_long,
        "match_basis": [{"label": "주제", "detail": "퇴근 후 그림"}],
    }


def test_책_소개를_옮긴_긴_이유는_null이고_카드와_한_줄_이유는_남는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other_reason = "퇴근길에 머리를 식힐 이야기를 찾으셨다면, 꿈을 파는 상점이라는 설정이 부담 없이 읽혀요."
    _use(monkeypatch, [_card(616964, COPIED), _card(2, other_reason)])

    copied, other = _post()

    assert copied["book_id"] == 616964
    assert copied["reason_long"] is None
    assert copied["reason_short"] == "퇴근 후의 일상을 다룬 에세이예요."
    assert copied["match_basis"] == [{"label": "주제", "detail": "퇴근 후 그림"}]
    # 다른 카드의 긴 이유는 그대로다.
    assert other["reason_long"] == other_reason


def test_요청과_이은_긴_이유는_그대로_둔다(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, [_card(616964, LINKED)])

    (card,) = _post()

    assert card["reason_long"] == LINKED


def test_카드_프롬프트는_긴_이유를_요청과_이으라고_하고_책_소개가_없는_책은_짚지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _use(monkeypatch, [_card(616964, LINKED)])

    _post()

    prompt = sent[0]
    assert "책 소개 문장을 옮기거나 줄여 쓰지 말고" in prompt
    assert "사용자가 찾은 것(위 조건)과 이 책 소개의 어떤 점이 맞는지" in prompt
    # 짚으면 모델이 책 소개가 없는 책을 아예 안 골라 카드가 0장이 된다(#86, 평가 33번).
    assert "소개글 없음" not in prompt
    assert "지어내지 마라" not in prompt
