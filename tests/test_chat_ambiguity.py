"""③ 모호한 요청 되묻기의 규칙(#283) — 지시어·무의미 입력·빠진 정보를 가려 내는 사전.

LLM·DB 없이 문장만으로 판단한다. 라우터에 끼워 보는 테스트는 tests/test_chat_router.py에 있다.
맞는 경우만큼 "묻지 않아야 하는 경우"가 중요하다 — 괜한 질문은 대화를 막는다.
"""

import pytest

from app.chat.schemas import Spec, SpecExact, Turn
from app.routers import chat
from app.search.schemas import SearchFilters


def _spec(**overrides) -> Spec:
    base = {
        "intent": "semantic",
        "exact": SpecExact(title=None, author=None, publisher=None),
        "filters": SearchFilters(),
        "semantic": None,
        "anchor_book": None,
        "exclude": [],
    }
    base.update(overrides)
    return Spec(**base)


_HISTORY = [
    Turn(role="user", text="잔잔한 소설"),
    Turn(role="assistant", text="골라봤어요."),
]


@pytest.mark.parametrize(
    "message",
    [
        "이거랑 비슷한 책 추천해줘",
        "그 책 말고 다른 거 보여줘",
        "아까 그 책 말고 다른 거 보여줘",
        "첫 번째 책이랑 비슷한 걸로 추천해줘",
        "2번째 책 같은 거",
    ],
)
def test_첫_턴에_가리킬_책이_없는_지시어는_책_제목을_묻는다(message: str) -> None:
    result = chat._unanswerable_request(message, _spec(), [])

    assert result is not None
    assert result[0] == "어떤 책을 말씀하시는 건가요?"
    assert "책 제목" in result[1]


def test_이어지는_턴이면_지시어가_있어도_묻지_않는다() -> None:
    assert chat._unanswerable_request("이거랑 비슷한 책", _spec(), _HISTORY) is None


def test_spec에_anchor_book이_있으면_지시어가_가리키는_책이_있다() -> None:
    assert (
        chat._unanswerable_request("이거랑 비슷한 책", _spec(anchor_book=1088), [])
        is None
    )


@pytest.mark.parametrize(
    "message",
    [
        "이것저것 재밌는 책 추천해줘",
        "요리책 추천해줘",
        "해리포터",
        "잔잔한 소설 추천해줘",
    ],
)
def test_지시어가_아닌_말은_지시어로_보지_않는다(message: str) -> None:
    assert chat._unanswerable_request(message, _spec(), []) is None


@pytest.mark.parametrize("message", ["ㅁㄴㅇㄹ", "ㅋㅋㅋ", "ㅎㅎ?", "..."])
def test_자모와_기호뿐인_입력은_다시_말해_달라고_한다(message: str) -> None:
    result = chat._unanswerable_request(message, _spec(), _HISTORY)

    assert result is not None
    assert result[0] == "무슨 말씀인지 잘 모르겠어요."


def test_자모가_섞여도_뜻이_있는_말이면_넘어간다() -> None:
    assert chat._unanswerable_request("ㅋㅋ 소설 추천해줘", _spec(), []) is None


@pytest.mark.parametrize(
    ("message", "expected_start"),
    [
        ("아이한테 읽어줄 책 추천해줘", "아이가 몇 살쯤인가요?"),
        ("선물할 책 추천해줘", "받는 분이 어떤 분인가요?"),
        ("저렴한 책 추천해줘", "예산은 어느 정도로"),
        ("최근에 나온 책 추천해줘", '"최근"은 어느 정도까지'),
        ("요즘 신간 알려줘", '"최근"은 어느 정도까지'),
    ],
)
def test_빠진_정보가_있는_요청은_그_정보를_묻는다(
    message: str, expected_start: str
) -> None:
    question = chat._slot_question(message)

    assert question is not None
    assert question.startswith(expected_start)


@pytest.mark.parametrize(
    "message",
    [
        "5살 아이한테 읽어줄 책 추천해줘",
        "초등 저학년 아이 책 추천해줘",
        "엄마한테 선물할 책 추천해줘",
        "친구 선물로 좋은 책",
        "5천원 이하 저렴한 책 추천해줘",
        "2024년 이후 최근 책 추천해줘",
        "올해 나온 신간 추천해줘",
        "아이돌 에세이 추천해줘",
        "요리책 추천해줘",
        "판타지 소설 추천해줘",
    ],
)
def test_이미_구체적이거나_해당_없는_말은_묻지_않는다(message: str) -> None:
    assert chat._slot_question(message) is None


def test_여러_개가_걸려도_하나만_묻는다() -> None:
    # 선물 + 저렴한 + 최근: 우선순위가 앞선 선물 질문 하나만 나간다.
    question = chat._slot_question("선물할 저렴하고 최근에 나온 책")

    assert question.startswith("받는 분이 어떤 분인가요?")


def test_질문은_reply_뒤에_빈_줄을_두고_붙인다() -> None:
    combined = chat._append_question("선물하기 좋은 책이에요.", "받는 분이 누구세요?")

    assert combined == "선물하기 좋은 책이에요.\n\n받는 분이 누구세요?"


def test_200자를_넘으면_넘치는_뒷_문장을_덜어_낸다() -> None:
    first = "첫 문장이에요."
    long_second = "둘째 문장은 " + "아주 " * 60 + "길어요."  # 한 문장이 200자를 넘는다
    question = "예산은 어느 정도로 생각하세요?"

    combined = chat._append_question(f"{first} {long_second}", question)

    assert len(combined) <= 200
    assert combined == f"{first}\n\n{question}"


def test_들어가는_만큼의_앞_문장은_남긴다() -> None:
    sentences = ["하나예요.", "둘이에요.", "셋이에요."]
    question = "예산은 어느 정도로 생각하세요?"

    combined = chat._append_question(" ".join(sentences), question)

    assert combined == "하나예요. 둘이에요. 셋이에요.\n\n" + question


def test_질문을_붙일_자리가_없으면_reply를_그대로_둔다() -> None:
    reply = "아주 긴 한 문장" + "가" * 220

    assert chat._append_question(reply, "예산은 어느 정도로 생각하세요?") == reply
