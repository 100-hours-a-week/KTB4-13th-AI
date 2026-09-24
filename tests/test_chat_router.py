"""③ POST /recommendations/chat 라우터 테스트 — 요청 검사와 응답 모양.

DB(후보검색)와 LLM(spec 갱신·카드생성)은 가짜로 바꿔 끼운다. 진짜 SQL·LLM
호출은 여기서 검증하지 않는다 — 여기서는 계약(상태 코드·응답 봉투·검증 경계)만
본다.

한 요청 안에서 LLM이 두 번(1단계 spec 갱신 → 3단계 카드 생성) 불린다.
`get_chat_model`을 한 번만 patch하면 두 호출이 같은 가짜 모델을 타므로,
두 답을 순서대로 돌려주는 `_sequenced_model`을 쓴다.
"""

import json

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.main import app
from app.routers import chat

client = TestClient(app)


def _fake_model(reply: str) -> RunnableLambda:
    """정해진 답을 돌려주는 가짜 모델. 체인의 모델 칸에 그대로 끼워진다."""
    return RunnableLambda(lambda _prompt: AIMessage(content=reply))


def _sequenced_model(*replies: str) -> RunnableLambda:
    """호출될 때마다 다음 답을 순서대로 돌려주는 가짜 모델.

    `get_chat_model`을 patch하는 lambda가 매번 이 함수를 새로 부르면 안 된다
    (그러면 호출마다 처음부터 다시 시작한다) — 인스턴스 하나를 만들어서
    `monkeypatch.setattr(chat, "get_chat_model", lambda: model)`처럼 같은
    객체를 돌려줘야 두 호출이 이어서 소비된다.
    """
    it = iter(replies)
    return RunnableLambda(lambda _prompt: AIMessage(content=next(it)))


def _failing_model() -> RunnableLambda:
    """연결 실패를 흉내 낸다. 진짜 openai 예외를 던져서 게이트웨이의 예외 변환까지 함께 본다."""

    def _raise(_prompt) -> AIMessage:
        raise openai.APIConnectionError(
            request=httpx.Request("POST", "http://localhost:11434/v1")
        )

    return RunnableLambda(_raise)


INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}


def _spec_reply(**patch) -> str:
    """1단계(spec 갱신) 자리에 끼울 가짜 LLM 응답 — 이번 턴에 바뀌는 값만 담는다."""
    return json.dumps(patch, ensure_ascii=False)


def _request(**overrides) -> dict:
    body = {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": "비 오는 날 읽을 책 추천해줘",
        "recent_turns": [],
        "exclude_book_ids": [],
        "image_ref": None,
    }
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def fake_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB를 안 타고 책 한 권짜리 후보를 돌려준다."""

    async def _fake(spec, exclude_book_ids, user_id) -> list[dict]:
        return [
            {
                "book_id": 1088,
                "title": "달러구트 꿈 백화점",
                "author": "이미예",
                "description": "잠든 사이 꿈을 사고파는 상점 이야기.",
            }
        ]

    monkeypatch.setattr(chat, "get_candidates", _fake)


def test_명세의_초기_spec으로_보내면_200과_계약_봉투를_돌려준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_reply = (
        '{"reply": "비 오는 날 분위기에 맞춰 골라봤어요.", '
        '"cards": [{"book_id": 1088, "reason_short": "잔잔한 판타지예요.",'
        ' "reason_long": "비 오는 날과 잘 어울리는 따뜻한 이야기입니다."}]}'
    )
    model = _sequenced_model(_spec_reply(), card_reply)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "recommend_success"
    data = body["data"]
    # 명세: 6개 필드(reply·spec·recognition·cards·followup·buttons)와 degraded가
    # 항상 존재해야 한다.
    assert data.keys() == {
        "reply",
        "spec",
        "recognition",
        "cards",
        "followup",
        "buttons",
        "degraded",
    }
    assert data["spec"].keys() == INITIAL_SPEC.keys()
    assert data["degraded"] is False
    # reply는 LLM이 이 턴에 만든 문장 그대로다 — 고정 문구가 아니다(#158).
    assert data["reply"] == "비 오는 날 분위기에 맞춰 골라봤어요."
    assert len(data["cards"]) == 1
    assert data["cards"][0]["book_id"] == 1088
    assert data["cards"][0]["reason_short"] == "잔잔한 판타지예요."


def test_LLM이_실패하면_degraded_true로_200을_돌려준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat, "get_chat_model", _failing_model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["degraded"] is True
    assert data["cards"] == []
    # 빈 말풍선을 보여주지 않는다(#123).
    assert data["reply"] != ""


def test_카드_생성_LLM_실패가_로그에_남는다(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """1단계는 성공하고 3단계(카드 생성)만 실패하는 경우 — 로그가 남는지 본다(#141).

    1단계 실패는 이미 logger.exception으로 남는데 3단계만 조용히 삼켰다 —
    그러면 이 경로의 LLM 장애를 감지할 방법이 로그도 상태코드도 없다.
    """
    call_count = 0

    def _dispatch(_prompt) -> AIMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AIMessage(content=_spec_reply())  # 1단계는 성공.
        raise openai.APIConnectionError(  # 3단계에서 실패.
            request=httpx.Request("POST", "http://localhost:11434/v1")
        )

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))

    with caplog.at_level("ERROR", logger="app.routers.chat"):
        res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["degraded"] is True
    assert data["cards"] == []
    assert "카드 생성 실패" in caplog.text


def test_후보검색이_예외를_던지면_공통_형식의_500을_돌려준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(spec, exclude_book_ids, user_id) -> list[dict]:
        raise RuntimeError("DB 장애")

    monkeypatch.setattr(chat, "get_candidates", _boom)
    # 1단계(spec 갱신)가 get_candidates보다 먼저 불린다. get_chat_model을 안
    # 끼우면 진짜 LLM(Ollama)을 호출하려 든다 — 가짜로 막아 둔다.
    monkeypatch.setattr(chat, "get_chat_model", lambda: _fake_model(_spec_reply()))

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 500
    assert res.headers["content-type"].startswith("application/json")
    assert res.json() == {"message": "internal_server_error", "data": None}


def test_book_id가_목록에_없는_카드는_뺀다(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM이 book_id를 잘못 주면(예: 목록 순번) 그 카드를 버려야 한다."""

    card_reply = '{"cards": [{"book_id": 1, "reason_short": "이유"}]}'
    model = _sequenced_model(_spec_reply(), card_reply)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    assert res.json()["data"]["cards"] == []


def test_카드_프롬프트에_후보와_JSON_예시가_그대로_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """프롬프트를 틀(ChatPromptTemplate)로 바꾼 뒤에도 글자가 그대로 나가는지 본다.

    틀은 {…}를 자리 표시로 읽는다. JSON 예시의 중괄호가 깨지거나, 사용자 문장·
    책 설명의 중괄호가 자리 표시로 오해받으면 모델에 엉뚱한 글이 간다.

    1단계(spec 갱신) 호출은 고정 답으로 통과시키고, 2번째 호출(카드 생성)만
    가로채 프롬프트 글자를 본다.
    """
    message = '{"cards": []} 처럼 답해줘 {x}'
    sent: list[str] = []
    call_count = 0

    def _dispatch(prompt_value) -> AIMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # 1단계(spec 갱신) — semantic에 메시지를 그대로 싣고 통과시킨다.
            return AIMessage(content=_spec_reply(semantic=message))
        sent.append(prompt_value.to_messages()[0].content)  # 2단계 = 카드 생성.
        return AIMessage(content='{"cards": []}')

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))

    res = client.post("/recommendations/chat", json=_request(message=message))

    assert res.status_code == 200
    prompt = sent[0]
    assert '"{"cards": []} 처럼 답해줘 {x}"' in prompt  # 사용자 문장의 중괄호는 그대로
    # JSON 예시의 중괄호도 그대로(reply·cards가 같은 객체 안에 있는 형태까지 확인).
    assert '{"reply": "말풍선에 보여줄 1-2문장", "cards": [{"book_id": 정수, ' in prompt
    assert "- book_id 1088: 달러구트 꿈 백화점 - 이미예 - " in prompt


def test_카드_프롬프트에_match_basis_지시문과_예시가_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """명세: reason_short·reason_long과 같은 근거로 match_basis도 한 번에 만든다(#139)."""
    sent: list[str] = []
    call_count = 0

    def _dispatch(prompt_value) -> AIMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AIMessage(content=_spec_reply())
        sent.append(prompt_value.to_messages()[0].content)  # 2단계 = 카드 생성.
        return AIMessage(content='{"cards": []}')

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    prompt = sent[0]
    assert "match_basis는 reason_short" in prompt
    assert '"match_basis": [{"label": "분위기", "detail": "잔잔함"}]' in prompt


def test_LLM이_준_match_basis가_카드에_그대로_담긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_reply = (
        '{"cards": [{"book_id": 1088, "reason_short": "이유", '
        '"match_basis": [{"label": "분위기", "detail": "잔잔함"}, '
        '{"label": "가격", "detail": "조건 충족"}]}]}'
    )
    model = _sequenced_model(_spec_reply(), card_reply)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    card = res.json()["data"]["cards"][0]
    assert card["match_basis"] == [
        {"label": "분위기", "detail": "잔잔함"},
        {"label": "가격", "detail": "조건 충족"},
    ]


def test_match_basis_형식이_틀리면_빈_배열로_폴백한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_reply = (
        '{"cards": [{"book_id": 1088, "reason_short": "이유", '
        '"match_basis": "잔잔한 분위기"}]}'  # 리스트가 아님
    )
    model = _sequenced_model(_spec_reply(), card_reply)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    assert res.json()["data"]["cards"][0]["match_basis"] == []


def test_parse_match_basis_형식_안_맞는_항목은_거른다() -> None:
    raw = [
        {"label": "분위기", "detail": "잔잔함"},
        {"label": "가격"},  # detail 없음
        {"detail": "판타지"},  # label 없음
        "그냥 문자열",  # dict가 아님
        {"label": 1, "detail": "숫자 라벨"},  # 타입이 틀림
    ]
    assert chat._parse_match_basis(raw) == [{"label": "분위기", "detail": "잔잔함"}]


def test_parse_match_basis_리스트가_아니면_빈_배열() -> None:
    assert chat._parse_match_basis("문자열") == []
    assert chat._parse_match_basis(None) == []


def test_카드_프롬프트에_reply_지시문과_예시가_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """명세: reply는 그 턴의 추천 내용을 반영한 1-2문장이어야 한다(#158).

    카드 생성과 같은 호출에서 reply도 함께 만들라는 지시문·JSON 예시가
    실제로 프롬프트에 실리는지 본다.
    """
    sent: list[str] = []
    call_count = 0

    def _dispatch(prompt_value) -> AIMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return AIMessage(content=_spec_reply())
        sent.append(prompt_value.to_messages()[0].content)
        return AIMessage(content='{"cards": []}')

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    prompt = sent[0]
    assert "reply는 고른 책을 언급하며" in prompt
    assert '{"reply": "말풍선에 보여줄 1-2문장", "cards": [{"book_id": 정수, ' in prompt


def test_카드_생성_LLM이_준_reply가_말풍선에_그대로_담긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card_reply = (
        '{"reply": "이 책 어때요? 비 오는 날과 잘 어울려요.", '
        '"cards": [{"book_id": 1088, "reason_short": "이유"}]}'
    )
    model = _sequenced_model(_spec_reply(), card_reply)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    assert res.json()["data"]["reply"] == "이 책 어때요? 비 오는 날과 잘 어울려요."


def test_reply가_빈_문자열이면_카드가_있어도_규칙_기반_문구로_대체된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """match_basis처럼(#139) reply도 형식이 틀리면(빈 문자열) 조용히 폴백한다."""
    card_reply = '{"reply": "", "cards": [{"book_id": 1088, "reason_short": "이유"}]}'
    model = _sequenced_model(_spec_reply(), card_reply)
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    assert res.json()["data"]["reply"] == "골라봤어요."


def test_후보가_없으면_규칙_기반_못_찾음_안내로_reply를_채운다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """검색 결과가 없으면 generate_cards가 카드 생성 LLM을 아예 안 부른다 —

    이때 "골라봤어요."를 그대로 쓰면 아무것도 안 골랐는데 골랐다는 문구가
    나가 어색하다(#158). 규칙 기반 못 찾음 안내로 바뀌어야 한다.
    """

    async def _empty(spec, exclude_book_ids, user_id) -> list[dict]:
        return []

    monkeypatch.setattr(chat, "get_candidates", _empty)
    # spec 갱신 호출 한 번만 일어나야 한다 — 카드 생성 호출까지 일어나면
    # _fake_model이 같은 답을 또 줘서 조용히 통과해버리므로, 여기서는 카드
    # 생성이 실제로 스킵되는지를 reply 값으로 간접 확인한다.
    monkeypatch.setattr(chat, "get_chat_model", lambda: _fake_model(_spec_reply()))

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["cards"] == []
    assert data["degraded"] is False
    assert (
        data["reply"]
        == "조건에 맞는 책을 아직 못 찾았어요. 다른 조건으로 다시 찾아볼까요?"
    )


def _capture_spec_prompt(sent: list[str]) -> RunnableLambda:
    """1단계(spec 갱신) 프롬프트 글자를 sent에 담아두고, 2단계는 빈 카드로 통과시킨다."""
    call_count = 0

    def _dispatch(prompt_value) -> AIMessage:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            sent.append(prompt_value.to_messages()[0].content)
            return AIMessage(content=_spec_reply())
        return AIMessage(content='{"cards": []}')

    return RunnableLambda(_dispatch)


def test_recent_turns가_spec_프롬프트에_실린다(monkeypatch: pytest.MonkeyPatch) -> None:
    """recent_turns가 실제로 1단계 프롬프트에 들어가는지 본다(#124).

    가짜 모델의 답은 항상 고정이라 "이해했는지"는 확인할 수 없다 — 대신
    프롬프트에 실제로 실렸는지(입력)만 확인한다.
    """
    sent: list[str] = []
    monkeypatch.setattr(chat, "get_chat_model", lambda: _capture_spec_prompt(sent))

    res = client.post(
        "/recommendations/chat",
        json=_request(
            recent_turns=[
                {"role": "user", "text": "비 오는 날 읽을 책 추천해줘"},
                {"role": "assistant", "text": "달러구트 꿈 백화점 추천드려요"},
            ]
        ),
    )

    assert res.status_code == 200
    prompt = sent[0]
    assert "비 오는 날 읽을 책 추천해줘" in prompt
    assert "달러구트 꿈 백화점 추천드려요" in prompt


def test_recent_turns가_없으면_안내_문구가_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """빈 대화(첫 턴)에서도 프롬프트 자리가 비지 않는지 본다."""
    sent: list[str] = []
    monkeypatch.setattr(chat, "get_chat_model", lambda: _capture_spec_prompt(sent))

    res = client.post("/recommendations/chat", json=_request(recent_turns=[]))

    assert res.status_code == 200
    assert "최근 대화 없음" in sent[0]


def test_recent_turns가_20턴_넘으면_최근_20개만_쓴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """명세: 최대 20턴, 초과분은 최근 20턴만 사용."""
    sent: list[str] = []
    monkeypatch.setattr(chat, "get_chat_model", lambda: _capture_spec_prompt(sent))

    turns = [{"role": "user", "text": f"턴{i}"} for i in range(25)]
    res = client.post("/recommendations/chat", json=_request(recent_turns=turns))

    assert res.status_code == 200
    prompt = sent[0]
    assert "턴24" in prompt  # 가장 최근
    assert "턴5" in prompt  # 최근 20개(인덱스 5~24)의 첫 턴
    assert "턴0" not in prompt  # 20개를 넘어가 잘려나감


def test_SPEC_PROMPT에_톤_조정_지시문과_예시가_실린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """분위기·톤 조정 표현을 semantic에 반영하라는 지시문·예시가 실제로 프롬프트에

    나가는지 본다(#132). "너무 무겁지 않은 걸로" 같은 말이 지시문 없이는
    버려지는 문제였다 — 실제 반영 여부는 가짜 모델로는 확인할 수 없으니(모델이
    고정 답만 돌려줌), 여기서는 지시문·예시가 프롬프트에 실제로 포함되는지만
    본다. 실제 LLM(Ollama) 검증 결과는 PR 본문에 남긴다.
    """
    sent: list[str] = []

    def _dispatch(prompt_value) -> AIMessage:
        sent.append(prompt_value.to_messages()[0].content)
        return AIMessage(content=_spec_reply())

    monkeypatch.setattr(chat, "get_chat_model", lambda: RunnableLambda(_dispatch))

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    prompt = sent[0]
    assert "분위기나 톤을" in prompt
    assert "통째로 다시 써라" in prompt
    assert '"semantic": "비 오는 날 읽을 무겁지 않은 책"' in prompt


def test_LLM이_바꾼_semantic이_응답_spec에_반영된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1단계 결과가 실제로 응답의 spec에 실리는지 본다(더 이상 메시지 그대로 복붙이 아님)."""

    spec_reply = _spec_reply(semantic="비 오는 날 읽을 잔잔한 책")
    model = _sequenced_model(spec_reply, '{"cards": []}')
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    assert res.json()["data"]["spec"]["semantic"] == "비 오는 날 읽을 잔잔한 책"


def test_patch가_안_건드린_필터는_요청_spec_값이_유지된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """언급 안 된 값을 옮겨 적게 하다 예시값(false)을 베껴버리던 회귀 재현.

    in_stock_only=true로 온 요청에서, 1단계 patch가 semantic만 건드리면
    in_stock_only는 patch에 안 실려도 true로 남아야 한다.
    """

    spec_reply = _spec_reply(semantic="품절 아닌 책")
    model = _sequenced_model(spec_reply, '{"cards": []}')
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    request_spec = {**INITIAL_SPEC, "filters": {"in_stock_only": True}}
    res = client.post("/recommendations/chat", json=_request(spec=request_spec))

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["spec"]["filters"]["in_stock_only"] is True
    assert data["spec"]["semantic"] == "품절 아닌 책"


def test_exclude는_patch로_받은_id를_기존_목록에_더한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exclude를 매번 통째로 옮겨 적게 하면 모델이 옛 항목을 빠뜨렸을 때 되살아난다.

    기존 exclude=[1]인 요청에서 patch가 exclude=[2]만 주면, 응답 spec의
    exclude는 [1]이 사라지지 않고 [1, 2]가 돼야 한다.
    """

    spec_reply = _spec_reply(exclude=[2])
    model = _sequenced_model(spec_reply, '{"cards": []}')
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    request_spec = {**INITIAL_SPEC, "exclude": [1]}
    res = client.post("/recommendations/chat", json=_request(spec=request_spec))

    assert res.status_code == 200
    assert res.json()["data"]["spec"]["exclude"] == [1, 2]


def test_spec_갱신_응답이_patch로_병합해도_Spec_모양이_아니면_원래_spec_그대로_카드도_건너뛴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1단계가 지금 spec 위에 겹쳐도 Spec 모양이 안 되는 값(잘못된 intent)을 주면 검증에서 걸린다.

    이때는 (LLM 연결이 끊긴 것과 마찬가지로) 원래 spec을 그대로 쓰고, 3단계
    (카드 생성)까지 건너뛴다 — 두 번째 LLM 호출이 실제로 일어나지 않는지도
    같이 본다(호출됐다면 아래 카드용 답을 먹고 cards가 채워졌을 것).
    """

    model = _sequenced_model(
        '{"intent": "그런 낱말 없음"}', '{"cards": [{"book_id": 1088}]}'
    )
    monkeypatch.setattr(chat, "get_chat_model", lambda: model)

    res = client.post("/recommendations/chat", json=_request())

    assert res.status_code == 200
    data = res.json()["data"]
    assert data["degraded"] is True
    assert data["cards"] == []
    # 갱신 안 되고 요청에 보낸 spec 그대로. model_dump()는 filters의 생략된
    # 키도 채워 돌려주므로 요청 그대로의 {} 와는 모양이 다르다 — 파싱해서 비교.
    assert data["spec"]["filters"] == {
        "category": None,
        "price_min": None,
        "price_max": None,
        "pub_year_from": None,
        "pub_year_to": None,
        "in_stock_only": False,
    }
    assert {**data["spec"], "filters": {}} == INITIAL_SPEC


def test_message와_image_ref가_둘다_있으면_400이다() -> None:
    res = client.post(
        "/recommendations/chat",
        json=_request(image_ref="https://example.com/cover.jpg"),
    )
    assert res.status_code == 400


def test_message가_비어있으면_400이다() -> None:
    res = client.post("/recommendations/chat", json=_request(message=""))
    assert res.status_code == 400


def test_spec이_6개_키를_안_갖추면_422_spec_schema_violation이다() -> None:
    res = client.post(
        "/recommendations/chat", json=_request(spec={"intent": "semantic"})
    )
    assert res.status_code == 422
    assert res.json()["message"] == "spec_schema_violation"


def test_본문이_JSON이_아니면_400이다() -> None:
    res = client.post(
        "/recommendations/chat",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert res.status_code == 400
