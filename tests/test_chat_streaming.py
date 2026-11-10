"""③ 스트리밍 전송(SSE, AI #236) 테스트 — reply 조각 꺼내기와 이벤트 계약.

DB(후보검색)와 LLM 은 가짜다. 카드 생성 LLM 은 GenericFakeChatModel 이라 흘려 받을 때 글이 공백 단위 조각으로
온다. 실제로 조각이 네트워크로 바로 나가는지(모아 보내지 않는지)는 TestClient 로는 못 본다 — 띄운 서버에
`curl -N -H 'Accept: text/event-stream'` 으로 붙여 본다.
"""

import asyncio
import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.chat.streaming import ReplyPreview, closed_reply
from app.main import app
from app.routers import chat

client = TestClient(app)

SSE = {"Accept": "text/event-stream"}

INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}

LLM_REPLY = '비 오는 날엔 "달러구트 꿈 백화점"이\n잘 어울려요.'
CARD_JSON = json.dumps(
    {
        "reply": LLM_REPLY,
        "cards": [
            {
                "book_id": 1088,
                "reason_short": "잔잔한 판타지예요.",
                "reason_long": "비 오는 날 차분히 읽기 좋은 꿈 이야기입니다.",
            }
        ],
    },
    ensure_ascii=False,
)


def _request(**overrides) -> dict:
    body = {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": "비 오는 날 읽을 책 추천해줘",
        "recent_turns": [],
        "exclude_isbns": [],
        "image_ref": None,
    }
    body.update(overrides)
    return body


def _events(text: str) -> list[tuple[str, dict]]:
    """SSE 본문을 (이벤트 이름, data) 목록으로 읽는다."""
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def _model(*replies: str) -> GenericFakeChatModel:
    """호출마다 다음 답을 낸다. 1단계(spec 갱신)는 한 번에, 3단계(카드)는 흘려서 받아 간다."""
    return GenericFakeChatModel(messages=iter(AIMessage(content=r) for r in replies))


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


# --- closed_reply ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("", None, id="빈_글"),
        pytest.param('{"cards": [', None, id="reply_키_전"),
        pytest.param('{"reply": ', None, id="값_시작_전"),
        pytest.param('{"reply": "비 오는', None, id="쓰는_중"),
        pytest.param('{"reply": "끝에 \\', None, id="걸친_백슬래시"),
        pytest.param(
            '{"reply": "따옴표 \\"', None, id="이스케이프한_따옴표는_끝이_아니다"
        ),
        pytest.param('{"reply": "다 썼어요", "cards": [', "다 썼어요", id="닫힘"),
        pytest.param('{"reply" : "공백도"', "공백도", id="콜론_앞뒤_공백"),
        pytest.param(
            '{"reply": "따옴표 \\"책\\""', '따옴표 "책"', id="따옴표_이스케이프"
        ),
        pytest.param('{"reply": "첫 줄\\n둘째"', "첫 줄\n둘째", id="줄바꿈_이스케이프"),
        pytest.param('{"reply": "웃음\\uD83D\\uDE00"', "웃음😀", id="대리쌍"),
        pytest.param(
            '{"reply": "날것\n줄바꿈"', "날것\n줄바꿈", id="이스케이프_안_한_줄바꿈"
        ),
    ],
)
def test_closed_reply는_다_쓴_reply만_푼다(text: str, expected: str | None) -> None:
    assert closed_reply(text) == expected


def test_한_글자씩_와도_reply를_다_쓴_때_한_번만_다듬어_보낸다() -> None:
    raw = json.dumps(
        {"reply": '따옴표 "책", 역슬래시 \\ 😀', "cards": [{"book_id": 1}]},
        ensure_ascii=True,  # \uXXXX 와 대리쌍까지 섞이게 한다
    )
    # (보낼 때까지 온 글, 보낸 글)
    sent: list[tuple[str, str]] = []

    async def feed_all() -> None:
        seen = ""

        async def sink(piece: str) -> None:
            sent.append((seen, piece))

        preview = ReplyPreview(sink, str.upper)
        for end in range(1, len(raw) + 1):
            seen = raw[:end]
            await preview.feed(seen)

    asyncio.run(feed_all())

    # 닫는 따옴표가 온 조각에서 보낸다. 카드를 쓰는 동안 기다리지 않는다
    closed_at = raw.index('", "cards"') + 1
    assert sent == [(raw[:closed_at], '따옴표 "책", 역슬래시 \\ 😀'.upper())]


def test_다듬어서_비면_보내지_않는다() -> None:
    sent: list[str] = []

    async def sink(piece: str) -> None:
        sent.append(piece)

    async def feed() -> None:
        preview = ReplyPreview(sink, lambda _reply: "")
        await preview.feed('{"reply": "지울 말", "cards": []}')

    asyncio.run(feed())

    assert sent == []


# --- POST /recommendations/chat, Accept: text/event-stream ---


def test_SSE면_reply를_먼저_보내고_done에_JSON_경로와_같은_envelope를_보낸다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream_model = _model("{}", CARD_JSON)
    monkeypatch.setattr(chat, "get_chat_model", lambda: stream_model)
    res = client.post("/recommendations/chat", json=_request(), headers=SSE)

    json_model = _model("{}", CARD_JSON)
    monkeypatch.setattr(chat, "get_chat_model", lambda: json_model)
    expected = client.post("/recommendations/chat", json=_request()).json()

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/event-stream")
    assert res.headers["X-Request-Id"]
    assert _events(res.text) == [("delta", {"text": LLM_REPLY}), ("done", expected)]
    assert expected["data"]["reply"] == LLM_REPLY


def test_최종_답에서_빠질_막연한_끝_질문은_먼저_보내지도_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 11/02 로컬 실측: 글자마다 보낼 때 "어떤 스타일이 끌리세요?"가 보였다가 done 에서 사라졌다
    card_json = json.dumps(
        {
            "reply": "비 오는 날엔 『달러구트 꿈 백화점』이 잘 어울려요. 어떤 스타일이 끌리세요?",
            "cards": json.loads(CARD_JSON)["cards"],
        },
        ensure_ascii=False,
    )
    model = _model("{}", card_json)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request(), headers=SSE)

    events = _events(res.text)
    final = "비 오는 날엔 『달러구트 꿈 백화점』이 잘 어울려요."
    assert events[0] == ("delta", {"text": final})
    assert events[1][0] == "done"
    assert events[1][1]["data"]["reply"] == final


def test_Accept가_없으면_지금처럼_JSON_한_번이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _model("{}", CARD_JSON)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.headers["content-type"].startswith("application/json")
    assert res.json()["data"]["reply"] == LLM_REPLY


def test_요청_검사_오류는_SSE여도_상태_코드로_답한다() -> None:
    res = client.post("/recommendations/chat", json=_request(message=""), headers=SSE)

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_카드_LLM이_실패하면_조각_없이_degraded_done을_보낸다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FailingStream(GenericFakeChatModel):
        async def _astream(self, *args, **kwargs):
            raise openai.APIConnectionError(
                request=httpx.Request("POST", "http://localhost:11434/v1")
            )
            yield  # pragma: no cover — 비동기 생성기로 만든다

    model = _FailingStream(messages=iter([AIMessage(content="{}")]))
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request(), headers=SSE)

    events = _events(res.text)
    assert [name for name, _ in events] == ["done"]
    assert events[0][1]["data"]["degraded"] is True


def test_LLM_없이_답하는_갈래도_done_하나로_끝난다() -> None:
    # 검색·추천을 해 봐야 소용없는 요청은 LLM 을 안 부른다. 조각은 0회다(명세: delta 0회 이상)
    res = client.post(
        "/recommendations/chat", json=_request(message="ㅋㅋㅋ"), headers=SSE
    )

    events = _events(res.text)
    assert [name for name, _ in events] == ["done"]
    assert events[0][1]["data"]["reply"] == "무슨 말씀인지 잘 모르겠어요."


def test_후보_검색이_실패하면_error_이벤트에_오류_envelope를_싣는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _broken(*args, **kwargs):
        raise RuntimeError("DB 끊김")

    monkeypatch.setattr(chat, "get_candidates", _broken)
    model = _model("{}")
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request(), headers=SSE)

    assert res.status_code == 200  # 헤더는 이미 나갔다
    assert _events(res.text) == [
        ("error", {"message": "internal_server_error", "data": None})
    ]


def test_처리하지_못한_예외도_error_이벤트로_끝난다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _crash(*args, **kwargs):
        raise RuntimeError("버그")

    monkeypatch.setattr(chat, "_recommend", _crash)

    res = client.post("/recommendations/chat", json=_request(), headers=SSE)

    assert _events(res.text) == [
        ("error", {"message": "internal_server_error", "data": None})
    ]
