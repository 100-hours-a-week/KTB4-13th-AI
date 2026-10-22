"""③ 대화로 좁혀 가기(#530) — 넓은 첫 요청에 한 번 묻기, 대답 턴, 마지막으로 보여 준 카드 읽기.

DB(후보 검색·보여 준 카드 조회)와 LLM 은 가짜로 바꿔 끼운다. 여기서는 라우터가 어느 길로 가는지와 응답 모양만 본다.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat import asking, shown
from app.chat.schemas import Spec, Turn
from app.core.config import get_settings
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
    reply: str = "위로하는 방식이 달라서 결이 다른 세 권을 골랐어요.",
) -> str:
    return json.dumps(
        {
            "cards": [
                {"book_id": book["book_id"], "reason_short": "이유"} for book in BOOKS
            ],
            "reply": reply,
        },
        ensure_ascii=False,
    )


# 넓은 첫 요청 판정 프롬프트의 첫머리. 가짜 모델이 이것으로 판정 호출을 알아본다
SCOPE_PREFIX = "너는 책 추천 챗봇이다. 사용자의 첫 요청을 보고"


class _Recorder:
    """호출마다 정해진 답을 차례로 돌려주고, 받은 프롬프트를 남긴다.

    앞 요청에 주제가 있으면 새 요청 판정 호출이 1단계와 나란히 간다(#513). 그 호출은 차례를 쓰지 않고 늘 이어가는
    말(same)로 답한다. 대화의 첫 요청이면 넓은지 판정하는 호출도 나란히 간다(#530). 그 호출은 차례를 쓰지 않고
    scope 로 답한다(기본은 분명하다).
    """

    def __init__(self, *replies: str, scope: str = '{"scope": "clear"}') -> None:
        self._replies = iter(replies)
        self._scope = scope
        self.prompts: list[str] = []
        self.relation_calls = 0
        self.scope_calls = 0

    def model(self) -> RunnableLambda:
        def _call(prompt_value) -> AIMessage:
            text = prompt_value.to_messages()[0].content
            if text.startswith("너는 책 추천 챗봇의 대화 분석기다"):
                self.relation_calls += 1
                return AIMessage(content='{"relation": "same"}')
            if text.startswith(SCOPE_PREFIX):
                self.scope_calls += 1
                return AIMessage(content=self._scope)
            self.prompts.append(text)
            return AIMessage(content=next(self._replies))

        return RunnableLambda(_call)


@pytest.fixture(autouse=True)
def narrowing_on(monkeypatch: pytest.MonkeyPatch):
    """묻기는 설정으로 켠다(기본은 끔). 이 파일은 켠 상태를 본다."""
    monkeypatch.setenv("CHAT_NARROWING", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


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


BROAD = {
    "scope": "broad",
    "reply": "위로가 필요하시군요.",
    "question": "요즘 어떤 일로 지치셨어요? 읽고 싶은 글의 결도 알려 주세요.",
    "options": ["회사 일로 지쳤어요", "관계 때문에 힘들어요", "이유 없이 무기력해요"],
}
ASKED_TURNS = [
    {"role": "user", "text": "위로되는 책 추천해줘"},
    {
        "role": "assistant",
        "text": "위로가 필요하시군요.\n"
        + asking.followup("요즘 어떤 일로 지치셨어요?"),
    },
]


def _broad(**changes) -> str:
    answer = {**BROAD, **changes}
    return json.dumps(
        {k: v for k, v in answer.items() if v is not None}, ensure_ascii=False
    )


def test_넓은_첫_요청은_찾기_전에_카드_없이_한_번_묻는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    rec = _Recorder(json.dumps({"semantic": "위로가 되는 책"}), scope=_broad())
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert fake_db == []  # 후보 검색을 부르지 않았다
    assert data["cards"] == []
    assert data["reply"] == "위로가 필요하시군요."
    # 명세: 카드가 없으면 followup 에 되묻는 문장. 질문 아래에 답 보기를 한 줄에 하나씩, 끝에 코드가 고정 문장을 붙인다(#505)
    assert data["followup"] == (
        "요즘 어떤 일로 지치셨어요? 읽고 싶은 글의 결도 알려 주세요.\n\n"
        "• 회사 일로 지쳤어요\n"
        "• 관계 때문에 힘들어요\n"
        "• 이유 없이 무기력해요\n\n"
        "이 중에 고르시거나 직접 말씀해 주세요.\n" + asking.TAIL
    )
    # BE·FE 를 바꾸지 않는다. 보기는 버튼이나 새 칸이 아니라 문장에만 있다
    assert data["buttons"] == []
    assert "followup_options" not in data
    # 메시지에서 알게 된 조건은 spec 에 남겨 다음 턴에 넘긴다
    assert data["spec"]["semantic"] == "위로가 되는 책"
    assert data["degraded"] is False
    # 호출은 1단계와 판정 두 번이다. 판정은 1단계 프롬프트를 바꾸지 않는다
    assert len(rec.prompts) == 1
    assert rec.scope_calls == 1
    assert "이번 메시지는 그 답이다" not in rec.prompts[0]


def test_분명한_첫_요청은_묻지_않고_찾는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    rec = _Recorder(json.dumps({"semantic": "위로가 되는 책"}), _cards_json())
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert rec.scope_calls == 1
    assert len(fake_db) == 1
    assert [card["book_id"] for card in data["cards"]] == [1, 2, 3]


def test_묻기의_reply가_비면_기본_문장을_쓴다(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(
        json.dumps({"semantic": "위로가 되는 책"}), scope=_broad(reply=None)
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert data["reply"] == asking.REPLY_FALLBACK


@pytest.mark.parametrize(
    "scope",
    [
        pytest.param(_broad(question="  "), id="질문이_빔"),
        pytest.param("JSON 아님", id="판정_실패"),
    ],
)
def test_판정이_넓다고_했어도_물을_말이_없으면_묻지_않고_찾는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list], scope: str
) -> None:
    rec = _Recorder(
        json.dumps({"semantic": "위로가 되는 책"}), _cards_json(), scope=scope
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert len(fake_db) == 1
    assert [card["book_id"] for card in data["cards"]] == [1, 2, 3]


def test_판정이_1단계보다_한참_늦으면_묻지_않고_찾는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    async def _slow(_message: str):
        await asyncio.sleep(1)
        return asking.Ask("늦은 답", "늦은 질문")

    monkeypatch.setattr(chat, "_judge_scope", _slow)
    monkeypatch.setattr(chat, "SCOPE_GRACE_SECONDS", 0.01)
    rec = _Recorder(json.dumps({"semantic": "위로가 되는 책"}), _cards_json())
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert len(fake_db) == 1
    assert data["cards"]


def test_제목을_콕_집은_요청은_판정이_넓다고_해도_찾는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    rec = _Recorder(
        json.dumps({"intent": "exact", "exact": {"title": "데미안"}}),
        _cards_json(),
        scope=_broad(),
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post(
        "/recommendations/chat", json=_request(message="데미안 찾아줘")
    ).json()["data"]

    assert len(fake_db) == 1
    assert data["cards"]


def test_첫_요청이_아니면_판정하지_않는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    turns = [
        {"role": "user", "text": "퇴근길에 읽을 소설"},
        {"role": "assistant", "text": "세 권이에요."},
    ]
    rec = _Recorder("{}", _cards_json(), scope=_broad())
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post(
        "/recommendations/chat", json=_request(message="다른 거", recent_turns=turns)
    ).json()["data"]

    assert rec.scope_calls == 0
    assert len(fake_db) == 1
    assert data["cards"]


def test_직전_질문의_답이면_답_규칙을_싣고_새_요청_판정은_하지_않는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    rec = _Recorder(
        json.dumps({"semantic": "회사 일로 지친 직장인을 위한 담담한 에세이"}),
        _cards_json(),
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())
    spec = {**INITIAL_SPEC, "semantic": "위로가 되는 책"}

    data = client.post(
        "/recommendations/chat",
        json=_request(
            message="회사 일 때문에. 담담한 에세이가 좋아",
            recent_turns=ASKED_TURNS,
            spec=spec,
        ),
    ).json()["data"]

    assert "이번 메시지는 그 답이다" in rec.prompts[0]
    assert rec.relation_calls == 0  # 답은 새 요청이 아니다
    assert rec.scope_calls == 0  # 한 대화에서 한 번만 묻는다
    assert fake_db[0][0].semantic == "회사 일로 지친 직장인을 위한 담담한 에세이"
    assert data["cards"]


def test_답이_아무거나면_조건을_그대로_두고_찾는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    rec = _Recorder("{}", _cards_json())
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())
    spec = {**INITIAL_SPEC, "semantic": "위로가 되는 책"}

    data = client.post(
        "/recommendations/chat",
        json=_request(message="아무거나", recent_turns=ASKED_TURNS, spec=spec),
    ).json()["data"]

    assert fake_db[0][0].semantic == "위로가 되는 책"
    assert data["cards"]


def test_묻기_뒤의_답은_문지기가_쇼핑에_들르지_않고_추천으로_보낸다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*_a, **_k):
        raise AssertionError("⑦ 이 불리면 안 된다")

    monkeypatch.setattr(chat.agent, "shop_turn", _boom)
    rec = _Recorder(
        json.dumps({"semantic": "지친 마음을 달래는 에세이"}), _cards_json()
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post(
        "/recommendations/chat",
        json=_request(
            message="요즘 너무 지쳐서",
            recent_turns=ASKED_TURNS,
            spec={**INITIAL_SPEC, "semantic": "위로가 되는 책"},
            idempotency_key="idem_ask",
            user_context={"logged_in": True},
            context_cards=[],
        ),
    ).json()["data"]

    assert data["route"] == "recommend"
    assert data["cards"]


def test_설정을_끄면_판정하지_않고_찾는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    monkeypatch.setenv("CHAT_NARROWING", "false")
    get_settings.cache_clear()
    rec = _Recorder(
        json.dumps({"semantic": "위로가 되는 책"}), _cards_json(), scope=_broad()
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert rec.scope_calls == 0
    assert len(fake_db) == 1
    assert data["cards"]


def test_답_규칙이_없으면_1단계_프롬프트는_이_기능_전과_같다() -> None:
    def _prompt(rule: str) -> str:
        return chat.SPEC_PROMPT.format_messages(
            recent_turns="(최근 대화 없음)",
            shown="(없음)",
            spec_json="{}",
            message="책",
            ask_rule=rule,
        )[0].content

    without = _prompt("")
    assert "{ask_rule}" not in without
    assert '{"turn": "answer"}로만 답하라.\n\n이번 메시지로 바뀌는 게 없으면' in without
    assert asking.rule("none") == "" and asking.rule("may_ask") == ""
    assert asking.ANSWERED_RULE in _prompt(asking.rule("answered"))


def test_판정_프롬프트는_중괄호가_깨지지_않고_채워진다() -> None:
    text = asking.SCOPE_PROMPT.format_messages(message="여행 {x}")[0].content

    assert '첫 요청: "여행 {x}"' in text
    assert '{"scope": "clear"}' in text
    assert '"options": ["…", "…"]}' in text


@pytest.mark.parametrize(
    ("turns", "asked"),
    [
        (ASKED_TURNS, True),
        # BE 는 줄마다 뒤쪽 200자만 싣는다. 고정 문장은 끝에 있어 남는다
        (
            [
                {"role": "user", "text": "책"},
                {"role": "assistant", "text": ("가" * 300 + asking.TAIL)[-200:]},
            ],
            True,
        ),
        ([*ASKED_TURNS, {"role": "user", "text": "에세이"}], False),
        (
            [
                *ASKED_TURNS,
                {"role": "user", "text": "에세이"},
                {"role": "assistant", "text": "세 권이에요."},
            ],
            False,
        ),
        ([{"role": "assistant", "text": "어느 쪽이 끌리세요?"}], False),
        ([], False),
    ],
)
def test_직전_챗봇_말이_묻기였는지_고정_문장으로_본다(turns, asked) -> None:
    assert asking.asked([Turn.model_validate(turn) for turn in turns]) is asked


def test_긴_질문은_뒷_문장부터_덜어_낸다() -> None:
    long = "가" * 70 + "? " + "나" * 50 + "."
    ask = asking.parse({"scope": "broad", "reply": "다" * 80, "question": long})

    assert ask.question == "가" * 70 + "?"
    assert len(ask.reply) == asking.REPLY_MAX_CHARS
    assert ask.options == ()  # 보기를 안 줬으면 질문만 한다
    assert asking.parse({"scope": "clear", "question": "q"}) is None
    assert asking.parse({"semantic": "x"}) is None


@pytest.mark.parametrize(
    ("raw", "options"),
    [
        pytest.param(
            ["국내 여행 코스", "해외 배낭여행"],
            ("국내 여행 코스", "해외 배낭여행"),
            id="그대로_둔다",
        ),
        pytest.param(
            ["  국내   여행 ", "국내 여행", "", 3, None, "가" * 21, "해외 배낭여행"],
            ("국내 여행", "해외 배낭여행"),
            id="공백_정리_겹침_빈칸_글자아님_너무김을_버린다",
        ),
        pytest.param(
            ["국내 여행", "상관없어요", "아무거나 좋아요", "해외 여행"],
            ("국내 여행", "해외 여행"),
            id="건너뛰기_뜻은_고정_문장과_겹치니_버린다",
        ),
        pytest.param(
            ["가", "나", "다", "라", "마"],
            ("가", "나", "다", "라"),
            id="넷까지만",
        ),
        pytest.param(
            ['"국내 여행"', "“해외 여행”"],
            ("국내 여행", "해외 여행"),
            id="따옴표로_감싸_넣으니_양끝_따옴표를_뗀다",
        ),
        pytest.param(["국내 여행"], (), id="하나뿐이면_고를_게_없다"),
        pytest.param("국내 여행, 해외 여행", (), id="목록이_아님"),
        pytest.param(None, (), id="없음"),
    ],
)
def test_답_보기는_문장에_넣을_수_있는_것만_남긴다(raw, options) -> None:
    answer = {"scope": "broad", "reply": "좋아요.", "question": "어떤 여행이세요?"}
    if raw is not None:
        answer["options"] = raw

    assert asking.parse(answer).options == options


def test_보기를_못_만들어도_질문은_한다(monkeypatch: pytest.MonkeyPatch) -> None:
    rec = _Recorder(
        json.dumps({"semantic": "위로가 되는 책"}), scope=_broad(options=["하나뿐"])
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())

    data = client.post("/recommendations/chat", json=_request()).json()["data"]

    assert data["cards"] == []
    assert data["followup"] == BROAD["question"] + "\n" + asking.TAIL


def test_보기가_길어_넘치면_뒤_보기부터_빼고_둘도_안_되면_질문만_한다() -> None:
    question = "가" * 80
    options = ("나" * 20, "다" * 20, "라" * 20)

    fitted = asking.followup(question, options)
    assert len(fitted) <= asking.FOLLOWUP_MAX_CHARS
    assert f"• {'나' * 20}\n• {'다' * 20}\n\n" in fitted
    assert "라" * 20 not in fitted
    assert fitted.endswith(asking.TAIL)
    assert asking.followup(question + "가" * 50, options) == (
        question + "가" * 50 + "\n" + asking.TAIL
    )
    assert asking.followup("어떤 여행이세요?") == "어떤 여행이세요?\n" + asking.TAIL


def test_보기를_따라_말한_답은_직전_질문의_답으로_받는다(
    monkeypatch: pytest.MonkeyPatch, fake_db: list[list]
) -> None:
    """보기 중 하나를 그대로 말해도 직접 쓴 답과 같은 길(답 규칙)로 간다."""
    rec = _Recorder(
        json.dumps({"semantic": "회사 일로 지친 직장인을 위로하는 책"}),
        _cards_json(),
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: rec.model())
    turns = [
        {"role": "user", "text": "위로되는 책 추천해줘"},
        # BE 는 챗봇 줄을 reply + 줄바꿈 + followup 으로 싣는다. 보기는 followup 안에 있다
        {
            "role": "assistant",
            "text": BROAD["reply"]
            + "\n"
            + asking.followup(BROAD["question"], tuple(BROAD["options"])),
        },
    ]

    data = client.post(
        "/recommendations/chat",
        json=_request(
            message=BROAD["options"][0],
            recent_turns=turns,
            spec={**INITIAL_SPEC, "semantic": "위로가 되는 책"},
        ),
    ).json()["data"]

    assert "이번 메시지는 그 답이다" in rec.prompts[0]
    assert rec.scope_calls == 0
    assert data["cards"]


def test_카드_프롬프트에_이번_메시지와_최근_대화와_걸린_조건이_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    turns = [
        {"role": "user", "text": "퇴근길에 읽을 소설"},
        {"role": "assistant", "text": "세 권이에요."},
    ]
    spec = {**INITIAL_SPEC, "semantic": "퇴근길에 읽을 소설"}
    rec = _Recorder(json.dumps({"filters": {"price_max": 20000}}), _cards_json())
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
    rec = _Recorder("{}", _cards_json(reply=long_reply))
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

    def _call(prompt_value) -> AIMessage:
        nonlocal calls
        if prompt_value.to_messages()[0].content.startswith(SCOPE_PREFIX):
            return AIMessage(content='{"scope": "clear"}')
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
    rec = _Recorder(json.dumps({"semantic": "위로가 되는 책"}), _cards_json())
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
