"""③ 대화로 좁혀 가기(#530) — 갈래 묻기, 대답 턴, 마지막으로 보여 준 카드 읽기.

DB(후보 검색·보여 준 카드 조회)와 LLM 은 가짜로 바꿔 끼운다. 여기서는 라우터가 어느 길로 가는지와 응답 모양만 본다.
"""

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat import branches, shown
from app.chat.schemas import Spec, Turn
from app.gate import rules as gate_rules
from app.main import app
from app.routers import chat

client = TestClient(app)

INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}

BOOKS = [
    {
        "book_id": 1,
        "title": "짧은 시 모음",
        "author": "가",
        "description": "하루 한 편 짧게 읽는 시.",
        "match_score": 30,
        "popularity": 1.0,
    },
    {
        "book_id": 2,
        "title": "담담한 하루",
        "author": "나",
        "description": "일상을 담담하게 적은 에세이.",
        "match_score": 28,
        "popularity": 1.0,
    },
    {
        "book_id": 3,
        "title": "골목 끝 서점",
        "author": "다",
        "description": "작은 서점에서 벌어지는 소설.",
        "match_score": 25,
        "popularity": 1.0,
    },
]

SHOWN = [
    shown.ShownBook(1, 1, "짧은 시 모음", "가", "하루 한 편 짧게 읽는 시."),
    shown.ShownBook(
        2, 2, "담담한 하루", "나", "일상을 담담하게 적은 에세이. 어려운 말 없이 읽힌다."
    ),
]


def _request(**overrides) -> dict:
    body = {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": "위로되는 책 추천해줘",
        "recent_turns": [],
        "exclude_book_ids": [],
        "image_ref": None,
    }
    body.update(overrides)
    return body


def _cards_json(
    branches: list[str] | None,
    reply: str = "위로하는 방식이 달라서 결이 다른 세 권을 골랐어요.",
) -> str:
    return json.dumps(
        {
            "cards": [
                {"book_id": book["book_id"], "reason_short": "이유"} for book in BOOKS
            ],
            "reply": reply,
            "branches": branches,
        },
        ensure_ascii=False,
    )


class _Recorder:
    """호출마다 정해진 답을 차례로 돌려주고, 받은 프롬프트를 남긴다.

    앞 요청에 주제가 있으면 새 요청 판정 호출이 1단계와 나란히 간다(#513). 그 호출은 차례를 쓰지 않고 늘 이어가는
    말(same)로 답한다.
    """

    def __init__(self, *replies: str) -> None:
        self._replies = iter(replies)
        self.prompts: list[str] = []

    def model(self) -> RunnableLambda:
        def _call(prompt_value) -> AIMessage:
            text = prompt_value.to_messages()[0].content
            if text.startswith("너는 책 추천 챗봇의 대화 분석기다"):
                return AIMessage(content='{"relation": "same"}')
            self.prompts.append(text)
            return AIMessage(content=next(self._replies))

        return RunnableLambda(_call)


@pytest.fixture(autouse=True)
def fake_db(monkeypatch: pytest.MonkeyPatch) -> list[list]:
    """후보 검색과 보여 준 카드 조회를 가짜로 바꾼다. 후보 검색이 불렸는지 남긴다."""
    searched: list[list] = []

    async def _candidates(spec, exclude_book_ids, user_id, owned_book_ids=None):
        searched.append([spec])
        return [dict(book) for book in BOOKS]

    async def _fetch(cards):
        ranks = {rank for rank, _ in cards}
        return [book for book in SHOWN if book.rank in ranks]

    monkeypatch.setattr(chat, "get_candidates", _candidates)
    monkeypatch.setattr(shown, "fetch", _fetch)
    return searched


BRANCHES = ["짧은 글", "에세이", "소설"]
# 질문 문장은 코드가 만든다. 모델은 갈래 이름만 낸다
QUESTION = (
    "짧은 글(1번), 에세이(2번), 소설(3번) 중 어느 쪽이 끌리세요? "
    "셋 다 아니면 원하는 쪽을 말씀해 주세요."
)


def test_넓은_첫_요청은_갈래_질문을_말풍선_끝에_붙인다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder(json.dumps({"semantic": "위로가 되는 책"}), _cards_json(BRANCHES))
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert data["reply"].endswith(QUESTION)
    assert data["reply"].startswith("위로하는 방식이 달라서")
    assert len(data["reply"]) <= 200
    assert [card["book_id"] for card in data["cards"]] == [1, 2, 3]
    # 카드가 있는 턴이라 명세상 followup 은 null 이다. 질문은 말풍선에 있다.
    assert data["followup"] is None
    # 좁히기를 허락한 턴에만 갈래 지시문이 실린다.
    assert "갈래마다 가장 맞는 책을 한 권씩" in rec.prompts[1]


def test_사용자가_두_번_답한_뒤에는_모델이_질문을_내도_붙이지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = [
        {"role": "user", "text": "위로되는 책 추천해줘"},
        {"role": "assistant", "text": "결이 다른 세 권이에요."},
        {"role": "user", "text": "에세이 쪽"},
        {"role": "assistant", "text": "에세이로 좁혔어요."},
    ]
    spec = {**INITIAL_SPEC, "semantic": "위로가 되는 에세이"}
    rec = _Recorder("{}", _cards_json(BRANCHES))
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post(
        "/recommendations/chat",
        json=_request(message="담담한 쪽", recent_turns=turns, spec=spec),
    ).json()["data"]

    assert QUESTION not in data["reply"]
    assert "branches는 null로 둬라." in rec.prompts[-1]


def test_제목을_콕_집은_요청은_좁히지_않는다() -> None:
    spec = Spec.model_validate(
        {
            **INITIAL_SPEC,
            "intent": "exact",
            "exact": {"title": "데미안", "author": None, "publisher": None},
        }
    )
    assert chat._may_narrow(spec, [], None) is False


def test_나이처럼_빠진_정보를_묻는_턴은_좁히지_않는다() -> None:
    spec = Spec.model_validate({**INITIAL_SPEC, "semantic": "아이에게 읽어 줄 책"})
    assert chat._may_narrow(spec, [], "아이가 몇 살쯤인가요?") is False
    assert chat._may_narrow(spec, [], None) is True


def test_사용자_말만_실린_최근_대화도_두_번까지만_좁힌다() -> None:
    spec = Spec.model_validate({**INITIAL_SPEC, "semantic": "위로가 되는 책"})
    one = [Turn(role="user", text="위로되는 책")]
    two = [*one, Turn(role="user", text="에세이")]
    assert chat._may_narrow(spec, one, None) is True
    assert chat._may_narrow(spec, two, None) is False


def test_고른_카드가_빠져_번호가_밀리면_질문을_버린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_json = json.dumps(
        {
            "cards": [
                {"book_id": 1, "reason_short": "이유"},
                {"book_id": 999, "reason_short": "목록에 없는 책"},
                {"book_id": 3, "reason_short": "이유"},
            ],
            "reply": "결이 다른 책을 골랐어요.",
            "branches": BRANCHES,
        },
        ensure_ascii=False,
    )
    rec = _Recorder(json.dumps({"semantic": "위로가 되는 책"}), card_json)
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert [card["book_id"] for card in data["cards"]] == [1, 3]
    assert QUESTION not in data["reply"]


def test_카드_프롬프트에_이번_메시지와_최근_대화와_걸린_조건이_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = [
        {"role": "user", "text": "퇴근길에 읽을 소설"},
        {"role": "assistant", "text": "세 권이에요."},
    ]
    spec = {**INITIAL_SPEC, "semantic": "퇴근길에 읽을 소설"}
    rec = _Recorder(json.dumps({"filters": {"price_max": 20000}}), _cards_json(None))
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    client.post(
        "/recommendations/chat",
        json=_request(message="2만원 이하로만 보여줘", recent_turns=turns, spec=spec),
    )

    prompt = rec.prompts[1]
    assert '이번 메시지: "2만원 이하로만 보여줘"' in prompt
    assert "user: 퇴근길에 읽을 소설" in prompt
    assert "걸린 조건: 2만원 이하" in prompt


def test_말풍선이_200자를_넘으면_뒷_문장부터_덜어_낸다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_reply = "가" * 150 + ". " + "나" * 120 + "."
    rec = _Recorder("{}", _cards_json(None, reply=long_reply))
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())
    spec = {**INITIAL_SPEC, "semantic": "위로가 되는 책"}

    data = client.post(
        "/recommendations/chat",
        json=_request(
            spec=spec,
            recent_turns=[{"role": "user", "text": "a"}, {"role": "user", "text": "b"}],
        ),
    ).json()["data"]

    assert data["reply"] == "가" * 150 + "."


# --- 대답 턴 -----------------------------------------------------------------


def test_질문이면_검색하지_않고_보여_준_책_소개로_답한다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    rec = _Recorder(
        json.dumps({"turn": "answer"}),
        json.dumps(
            {
                "reply": "2번은 어려운 말 없이 읽혀요.",
                "followup": "이 결로 더 찾아볼까요?",
            },
            ensure_ascii=False,
        ),
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())
    spec = {**INITIAL_SPEC, "semantic": "위로가 되는 에세이"}
    body = _request(
        message="2번 초보도 읽을 수 있어?",
        spec=spec,
        recent_turns=[
            {"role": "user", "text": "위로되는 에세이"},
            {"role": "assistant", "text": "세 권이에요."},
        ],
        context_cards=[
            {"isbn": "9780000000001", "rank": 1, "match_score": 30},
            {"isbn": "9780000000002", "rank": 2, "match_score": 28},
        ],
    )

    data = client.post("/recommendations/chat", json=body).json()["data"]

    assert fake_db == []  # 후보 검색을 부르지 않았다
    assert data["cards"] == []
    assert data["reply"] == "2번은 어려운 말 없이 읽혀요."
    assert data["followup"] == "이 결로 더 찾아볼까요?"
    assert (
        data["spec"] == Spec.model_validate(spec).model_dump()
    )  # 질문은 조건을 바꾸지 않는다
    assert data["degraded"] is False
    # 1단계와 대답 프롬프트 모두 보여 준 책을 본다.
    assert "2번: 『담담한 하루』 - 나 - 일상을 담담하게" in rec.prompts[0]
    assert "어려운 말 없이 읽힌다" in rec.prompts[1]
    assert len(rec.prompts) == 2  # 호출은 두 번


def test_대답_턴의_followup이_비면_기본_질문을_쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder(
        json.dumps({"turn": "answer"}),
        json.dumps({"reply": "안녕하세요!"}, ensure_ascii=False),
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request(message="안녕")).json()[
        "data"
    ]

    assert data["reply"] == "안녕하세요!"
    assert data["followup"] == chat._ANSWER_DEFAULT_FOLLOWUP
    assert data["cards"] == []


def test_대답_LLM이_실패하면_고정_안내와_degraded를_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def _call(_prompt) -> AIMessage:
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(content=json.dumps({"turn": "answer"}))
        return AIMessage(content="JSON 아님")

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_call))

    data = client.post(
        "/recommendations/chat", json=_request(message="너는 뭐 할 수 있어?")
    ).json()["data"]

    assert data["degraded"] is True
    assert data["cards"] == []
    assert data["reply"] == chat._ANSWER_FALLBACK_REPLY
    assert data["followup"] == chat._ANSWER_FALLBACK_FOLLOWUP


def test_보여_준_카드가_없으면_1단계_프롬프트에_없음이라고_적는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _Recorder(json.dumps({"semantic": "위로가 되는 책"}), _cards_json(None))
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    client.post("/recommendations/chat", json=_request())

    assert "마지막으로 보여 준 책(번호는 화면의 카드 순서):\n(없음)" in rec.prompts[0]


# --- context_cards 읽기 --------------------------------------------------------


def test_context_cards는_틀린_항목을_건너뛰고_순번_차례로_읽는다() -> None:
    payload = {
        "context_cards": [
            {"isbn": "9780000000003", "rank": 3},
            {"isbn": "978-000", "rank": 4},  # ISBN 형식이 틀림
            {"isbn": "9780000000001", "rank": 1},
            {"isbn": "9780000000001", "rank": 2},  # 같은 ISBN
            {"isbn": "9780000000005", "rank": True},  # 순번이 정수가 아님
            "문자열",
            {"isbn": "9780000000006", "rank": 0},
        ]
    }
    assert shown.parse(payload) == [(1, "9780000000001"), (3, "9780000000003")]


def test_context_cards가_없거나_목록이_아니면_빈_목록이다() -> None:
    assert shown.parse({}) == []
    assert shown.parse({"context_cards": "x"}) == []


def test_보여_준_책_목록은_번호_제목_저자_소개_앞부분이다() -> None:
    books = [
        shown.ShownBook(1, 1, "제목", "저자", "  가나다\n라마바사  "),
        shown.ShownBook(2, 2, "소개 없는 책", None, None),
    ]
    assert (
        shown.listing(books, 5)
        == "1번: 『제목』 - 저자 - 가나다 라\n2번: 『소개 없는 책』 - (소개 없음)"
    )
    assert shown.listing([], 5) == "(없음)"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "짧은 글, 에세이, 소설",  # 목록이 아니다
        ["짧은 글", "에세이"],  # 카드 수와 다르다
        ["짧은 글", "짧은 글", "소설"],  # 갈래가 겹친다
        ["짧은 글", "", "소설"],
        ["짧은 글", "에세이", "가" * (branches.NAME_MAX + 1)],
        ["짧은 글", 2, "소설"],
    ],
)
def test_갈래_이름이_카드마다_하나씩_다르지_않으면_묻지_않는다(raw) -> None:
    assert branches.parse_names(raw, 3) is None


def test_카드가_한_장이면_갈래를_묻지_않는다() -> None:
    assert branches.parse_names(["에세이"], 1) is None


def test_갈래_질문은_코드가_어느_쪽_꼴로_만들고_다른_쪽_길을_연다() -> None:
    two = branches.question(["시", "에세이"])

    assert (
        two
        == "시(1번), 에세이(2번) 중 어느 쪽이 끌리세요? 둘 다 아니면 원하는 쪽을 말씀해 주세요."
    )
    # 문지기가 이 꼴을 갈래 질문으로 알아본다
    assert gate_rules.asked_branch(
        [Turn(role="assistant", text="두 권이에요.\n\n" + two)]
    )


ASKED = [
    Turn(role="user", text="위로되는 에세이"),
    Turn(
        role="assistant",
        text="결이 다른 세 권이에요.\n\n"
        + branches.question(["전문가의 위로", "심리학 에세이", "감성 에세이"]),
    ),
]


@pytest.mark.parametrize(
    ("message", "picked"),
    [
        ("3번", "감성 에세이"),
        ("3번이 좋을 것 같아!", "감성 에세이"),
        ("두 번째 쪽이요", "심리학 에세이"),
        ("감성 쪽으로", "감성 에세이"),
        ("3번은 별로야", None),
        ("1번은 초보도 볼 수 있어?", None),
        ("1번 책, 진짜 처음 하는 사람용인지 물어볼게.", None),
        ("1번이랑 3번", None),
        ("에세이 쪽", None),  # 두 갈래에 다 있는 낱말
    ],
)
def test_직전_갈래_질문에서_고른_갈래_하나를_찾는다(message: str, picked) -> None:
    names = branches.asked(ASKED)

    assert names == ["전문가의 위로", "심리학 에세이", "감성 에세이"]
    assert branches.chosen(message, names) == picked


def test_고른_갈래를_검색어에_붙이고_이미_있으면_두_번_붙이지_않는다() -> None:
    spec = Spec.model_validate({**INITIAL_SPEC, "semantic": "위로가 되는 에세이"})

    applied = chat._apply_branch_choice(spec, "3번", ASKED)
    assert applied.semantic == "위로가 되는 에세이, 감성 에세이"
    assert chat._apply_branch_choice(applied, "3번", ASKED).semantic == applied.semantic
    # 직전 말이 갈래 질문이 아니면 그대로다
    assert chat._apply_branch_choice(spec, "3번", ASKED[:1]) == spec


def test_갈래_질문은_한_대화에서_한_번만_한다() -> None:
    spec = Spec.model_validate({**INITIAL_SPEC, "semantic": "위로가 되는 에세이"})

    assert chat._may_narrow(spec, ASKED, None) is False
