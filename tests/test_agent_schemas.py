"""⑦ /agent/act 의 요청 검사와 응답 모양 테스트(#350). DB·LLM 없이 돈다.

요청은 BE 가 보내므로 계약에 안 맞으면 400 이다. 응답은 명세 ⑦ 의 예시 JSON(tests/data)이
그대로 모델에 들어가고 같은 모양으로 나와야 한다. 명세 예시와 모델이 어긋나면 여기서 먼저 걸린다.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.agent.schemas import (
    MAX_CONTEXT_CARDS,
    MAX_CONVERSATION_ID_CHARS,
    MAX_MESSAGE_CHARS,
    MAX_RECENT_TURNS,
    MAX_TURN_CHARS,
    AgentActData,
    Button,
    ToolName,
    parse_request,
)

EXAMPLES = json.loads(
    (Path(__file__).parent / "data" / "agent_act_examples.json").read_text(
        encoding="utf-8"
    )
)
ISBN_A, ISBN_B = "9788900000011", "9788900000028"


def _request(**overrides) -> dict:
    """명세 ⑦의 입력 예시."""
    body = json.loads(json.dumps(EXAMPLES["request"]))
    body.update(overrides)
    return body


def _without(body: dict, key: str) -> dict:
    return {k: v for k, v in body.items() if k != key}


# --- 요청: 통과 ---


def test_명세의_요청_예시는_통과한다() -> None:
    req = parse_request(EXAMPLES["request"])

    assert req is not None
    assert req.user_id == 123
    assert req.context_cards[0].isbn == "9788900000011"
    assert req.focused_isbn == "9788900000011"
    assert req.user_context.logged_in is True
    assert req.allow_tools is None


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"conversation_id": None}, id="conversation_id_null"),
        pytest.param({"focused_isbn": None}, id="focused_isbn_null"),
        pytest.param({"allow_tools": None}, id="allow_tools_null"),
        pytest.param({"context_cards": []}, id="카드_없음"),
        pytest.param({"message": "가" * MAX_MESSAGE_CHARS}, id="메시지_200자"),
        pytest.param(
            {"conversation_id": "c" * MAX_CONVERSATION_ID_CHARS},
            id="conversation_id_200자",
        ),
        pytest.param({"user_context": {"logged_in": False}}, id="로그인_안_함"),
        pytest.param({"user_id": 2_147_483_647}, id="user_id_int32_상한"),
        # 옛 명세의 칸을 보내도 400이 아니다. 모르는 칸은 무시한다.
        pytest.param({"book_id": 1088, "price": 13500}, id="모르는_칸_무시"),
        pytest.param(
            {"user_context": {"logged_in": True, "has_default_address": True}},
            id="옛_user_context_칸_무시",
        ),
    ],
)
def test_선택_칸과_경계값은_통과한다(overrides) -> None:
    assert parse_request(_request(**overrides)) is not None


def test_선택_칸이_통째로_없어도_통과한다() -> None:
    body = {
        "user_id": 1,
        "message": "재고 있어?",
        "user_context": {"logged_in": True},
        "idempotency_key": "idem_1",
    }

    req = parse_request(body)

    assert req is not None
    assert req.conversation_id is None
    assert req.context_cards == []
    assert req.focused_isbn is None
    assert req.allow_tools is None


def test_카드가_null이면_빈_목록으로_받는다() -> None:
    # BE는 비어 있는 목록을 null로 보낼 수 있다(⑤⑥과 같게).
    req = parse_request(_request(context_cards=None))

    assert req is not None
    assert req.context_cards == []


def test_카드는_최대_10개까지_받는다() -> None:
    card = {"isbn": ISBN_A, "rank": 1, "match_score": 80}

    assert parse_request(_request(context_cards=[card] * MAX_CONTEXT_CARDS)) is not None
    assert (
        parse_request(_request(context_cards=[card] * (MAX_CONTEXT_CARDS + 1))) is None
    )


def test_allow_tools는_알려진_이름을_ToolName으로_받는다() -> None:
    req = parse_request(_request(allow_tools=["cart.view", "book.offer"]))

    assert req is not None
    assert req.allow_tools == [ToolName.CART_VIEW, ToolName.BOOK_OFFER]
    assert all(isinstance(name, ToolName) for name in req.allow_tools)


# --- 요청: 거절 ---


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="목록_본문"),
        pytest.param("문자열", id="문자열_본문"),
        pytest.param(None, id="null_본문"),
        pytest.param(123, id="숫자_본문"),
    ],
)
def test_본문이_객체가_아니면_거절한다(payload) -> None:
    assert parse_request(payload) is None


@pytest.mark.parametrize(
    "key", ["user_id", "message", "user_context", "idempotency_key"]
)
def test_필수_칸이_없으면_거절한다(key) -> None:
    assert parse_request(_without(_request(), key)) is None


@pytest.mark.parametrize(
    "overrides",
    [
        # user_id
        pytest.param({"user_id": "123"}, id="user_id_문자열"),
        pytest.param({"user_id": 1.5}, id="user_id_실수"),
        pytest.param({"user_id": 2_147_483_648}, id="user_id_int32_초과"),
        pytest.param({"user_id": True}, id="user_id_불리언"),
        # message
        pytest.param({"message": ""}, id="메시지_빈_문자열"),
        pytest.param({"message": "   "}, id="메시지_공백만"),
        pytest.param({"message": "가" * (MAX_MESSAGE_CHARS + 1)}, id="메시지_201자"),
        pytest.param({"message": 123}, id="메시지_숫자"),
        pytest.param({"message": None}, id="메시지_null"),
        # conversation_id
        pytest.param({"conversation_id": ""}, id="conversation_id_빈_문자열"),
        pytest.param(
            {"conversation_id": "c" * (MAX_CONVERSATION_ID_CHARS + 1)},
            id="conversation_id_201자",
        ),
        # idempotency_key
        pytest.param({"idempotency_key": ""}, id="멱등_키_빈_문자열"),
        pytest.param({"idempotency_key": None}, id="멱등_키_null"),
        # user_context
        pytest.param({"user_context": {}}, id="logged_in_없음"),
        pytest.param({"user_context": {"logged_in": "true"}}, id="logged_in_문자열"),
        pytest.param({"user_context": None}, id="user_context_null"),
        # focused_isbn
        pytest.param({"focused_isbn": "978-8-9000-0001-1"}, id="focused_isbn_하이픈"),
        pytest.param({"focused_isbn": "978890000001"}, id="focused_isbn_12자리"),
        pytest.param({"focused_isbn": 9788900000011}, id="focused_isbn_숫자"),
        # allow_tools
        pytest.param({"allow_tools": ["order.preview"]}, id="없앤_tool_이름"),
        pytest.param({"allow_tools": ["cart.view", "nope"]}, id="모르는_tool_섞임"),
        pytest.param({"allow_tools": "cart.view"}, id="allow_tools_문자열"),
        pytest.param({"allow_tools": [1]}, id="allow_tools_숫자"),
        pytest.param({"allow_tools": [["cart.view"]]}, id="allow_tools_중첩"),
        # context_cards
        pytest.param({"context_cards": "카드"}, id="카드_문자열"),
        pytest.param({"context_cards": [None]}, id="카드_null_항목"),
    ],
)
def test_계약에_안_맞으면_거절한다(overrides) -> None:
    assert parse_request(_request(**overrides)) is None


@pytest.mark.parametrize(
    "card",
    [
        pytest.param(
            {"isbn": "978-8-9000-0001-1", "rank": 1, "match_score": 80},
            id="isbn_하이픈",
        ),
        pytest.param(
            {"isbn": "97889000000", "rank": 1, "match_score": 80}, id="isbn_짧음"
        ),
        pytest.param({"isbn": ISBN_A, "rank": 0, "match_score": 80}, id="rank_0"),
        pytest.param({"isbn": ISBN_A, "rank": 1.5, "match_score": 80}, id="rank_실수"),
        pytest.param({"isbn": ISBN_A, "rank": 1, "match_score": 101}, id="점수_101"),
        pytest.param({"isbn": ISBN_A, "rank": 1, "match_score": -1}, id="점수_음수"),
        pytest.param({"isbn": ISBN_A, "rank": 1, "match_score": 92.5}, id="점수_실수"),
        pytest.param({"isbn": ISBN_A, "rank": 1}, id="점수_없음"),
        pytest.param({"rank": 1, "match_score": 80}, id="isbn_없음"),
        # 옛 명세처럼 book_id만 보내면 책을 알 수 없다
        pytest.param({"book_id": 1088, "rank": 1, "match_score": 80}, id="book_id만"),
    ],
)
def test_카드가_계약에_안_맞으면_거절한다(card) -> None:
    assert parse_request(_request(context_cards=[card])) is None


@pytest.mark.parametrize("field", ["message", "idempotency_key", "conversation_id"])
@pytest.mark.parametrize("bad", ["\x00", "\ud800"], ids=["NUL", "짝_없는_서로게이트"])
def test_NUL과_짝_없는_서로게이트가_든_문자열은_거절한다(field, bad) -> None:
    # 그대로 두면 DB나 인코딩 단계에서야 터져 500이 난다(#201)
    assert parse_request(_request(**{field: f"앞{bad}뒤"})) is None


def test_허용_목록의_문자열도_NUL이_있으면_거절한다() -> None:
    assert parse_request(_request(allow_tools=["cart.view\x00"])) is None


# --- 응답: 명세 예시 ---


@pytest.mark.parametrize("name", list(EXAMPLES["responses"]))
def test_명세의_응답_예시는_모델을_통과하고_같은_모양으로_나온다(name) -> None:
    example = EXAMPLES["responses"][name]

    data = AgentActData.model_validate(example["data"])

    assert data.model_dump(mode="json") == example["data"]


def test_예시_응답_모두에_not_shopping_칸이_있다() -> None:
    # 칸이 빠진 예시가 있으면 호출자(BE)가 이 칸이 항상 있는지 헷갈린다
    for example in EXAMPLES["responses"].values():
        assert "not_shopping" in example["data"]


def test_쇼핑_요청이_아님_응답은_아무것도_하지_않은_모양이다() -> None:
    data = AgentActData.not_shopping_reply()

    assert data.model_dump(mode="json") == EXAMPLES["responses"]["not_shopping"]["data"]


def test_not_shopping인데_답이나_버튼이_있으면_만들_수_없다() -> None:
    base = EXAMPLES["responses"]["not_shopping"]["data"]

    with pytest.raises(ValidationError):
        AgentActData.model_validate({**base, "reply": "담았어요"})
    with pytest.raises(ValidationError):
        AgentActData.model_validate(
            {**base, "buttons": [{"label": "닫기", "action": "dismiss"}]}
        )


def test_응답에_모르는_칸이나_없앤_tool_이름이_있으면_만들_수_없다() -> None:
    # 응답을 만드는 쪽이 칸 이름을 틀리면 조용히 사라지지 않고 바로 터져야 한다
    base = EXAMPLES["responses"]["add"]["data"]

    with pytest.raises(ValidationError):
        AgentActData.model_validate({**base, "confidence": 0.9})

    wrong_tool = json.loads(json.dumps(base))
    wrong_tool["tool_calls"][0]["name"] = "order.preview"
    with pytest.raises(ValidationError):
        AgentActData.model_validate(wrong_tool)


# --- 응답: 버튼 ---


@pytest.mark.parametrize(
    "button",
    [
        pytest.param(
            {"label": "네", "action": "confirm_reference", "isbn": ISBN_A},
            id="confirm_reference",
        ),
        pytest.param(
            {"label": "담기", "action": "confirm_selection", "isbns": [ISBN_A, ISBN_B]},
            id="confirm_selection",
        ),
        pytest.param(
            {"label": "상세", "action": "open_book", "isbn": ISBN_A}, id="open_book"
        ),
        pytest.param(
            {"label": "장바구니", "action": "navigate", "target": "CART-001"},
            id="navigate",
        ),
        pytest.param({"label": "다른 조합", "action": "reselect"}, id="reselect"),
        pytest.param({"label": "아니요", "action": "dismiss"}, id="dismiss"),
    ],
)
def test_action에_맞는_칸만_가진_버튼은_만들어지고_그_칸만_나온다(button) -> None:
    built = Button.model_validate(button)

    assert built.model_dump(mode="json") == button


@pytest.mark.parametrize(
    "button",
    [
        pytest.param({"label": "네", "action": "confirm_reference"}, id="isbn_빠짐"),
        pytest.param({"label": "담기", "action": "confirm_selection"}, id="isbns_빠짐"),
        pytest.param(
            {"label": "담기", "action": "confirm_selection", "isbns": []},
            id="isbns_빈_목록",
        ),
        pytest.param({"label": "이동", "action": "navigate"}, id="target_빠짐"),
        pytest.param(
            {"label": "다른 조합", "action": "reselect", "isbn": ISBN_A},
            id="reselect에_isbn",
        ),
        pytest.param(
            {"label": "아니요", "action": "dismiss", "target": "CART-001"},
            id="dismiss에_target",
        ),
        pytest.param(
            {"label": "네", "action": "confirm_reference", "isbns": [ISBN_A]},
            id="칸_이름_바뀜",
        ),
        pytest.param(
            {
                "label": "네",
                "action": "confirm_reference",
                "isbn": ISBN_A,
                "target": "X",
            },
            id="칸_넘침",
        ),
        pytest.param({"label": "주문", "action": "confirm_order"}, id="없앤_action"),
        pytest.param(
            {"label": "네", "action": "confirm_reference", "isbn": "978-8-9000-0001-1"},
            id="isbn_하이픈",
        ),
    ],
)
def test_action과_칸이_안_맞는_버튼은_만들_수_없다(button) -> None:
    # 7단계(버튼 붙이기)가 잘못된 버튼을 만들 수 없게 모델에서 막는다
    with pytest.raises(ValidationError):
        Button.model_validate(button)


# --- 최근 대화 (#397) ---


def _turns(n: int) -> list[dict]:
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "text": f"말{i}"}
        for i in range(n)
    ]


def test_최근_대화가_없으면_빈_목록이다() -> None:
    req = parse_request(_request())

    assert req is not None
    assert req.recent_turns == []


def test_최근_대화를_사용자와_에이전트의_말로_받는다() -> None:
    turns = [
        {"role": "user", "text": "그거 담아줘"},
        {"role": "assistant", "text": "카드 1번을 담을까요?"},
    ]

    req = parse_request(_request(recent_turns=turns))

    assert req is not None
    assert [(t.role, t.text) for t in req.recent_turns] == [
        ("user", "그거 담아줘"),
        ("assistant", "카드 1번을 담을까요?"),
    ]


def test_최근_대화가_null이면_빈_목록으로_받는다() -> None:
    req = parse_request(_request(recent_turns=None))

    assert req is not None
    assert req.recent_turns == []


def test_20턴을_넘으면_최근_20턴만_쓴다() -> None:
    # ③과 같다: 개수 상한을 넘어도 400이 아니라 최근 것만 쓴다
    req = parse_request(_request(recent_turns=_turns(MAX_RECENT_TURNS + 3)))

    assert req is not None
    assert len(req.recent_turns) == MAX_RECENT_TURNS
    assert req.recent_turns[-1].text == f"말{MAX_RECENT_TURNS + 2}"
    assert req.recent_turns[0].text == "말3"


@pytest.mark.parametrize(
    "turns",
    [
        pytest.param([{"role": "bot", "text": "안녕"}], id="모르는_role"),
        pytest.param([{"role": "user"}], id="text_없음"),
        pytest.param([{"text": "안녕"}], id="role_없음"),
        pytest.param(
            [{"role": "user", "text": "가" * (MAX_TURN_CHARS + 1)}], id="201자"
        ),
        pytest.param([{"role": "user", "text": 1}], id="text_숫자"),
        pytest.param(["안녕"], id="문자열_항목"),
        pytest.param({"role": "user", "text": "안녕"}, id="목록_아님"),
        pytest.param([{"role": "user", "text": "앞\x00뒤"}], id="NUL_문자"),
    ],
)
def test_최근_대화의_형식이_틀리면_거절한다(turns) -> None:
    # 개수는 잘라서 쓰지만 항목의 형식, 길이, 값이 틀리면 400이다(③과 같다)
    assert parse_request(_request(recent_turns=turns)) is None


def test_200자까지는_받는다() -> None:
    turns = [{"role": "user", "text": "가" * MAX_TURN_CHARS}]

    assert parse_request(_request(recent_turns=turns)) is not None


def test_최근_대화의_모르는_칸은_무시한다() -> None:
    # ⑦ 요청은 모르는 칸을 무시한다(옛 칸을 보내도 400이 아니다). 항목 안도 같다
    turns = [{"role": "user", "text": "안녕", "extra": 1}]

    req = parse_request(_request(recent_turns=turns))

    assert req is not None
    assert [(t.role, t.text) for t in req.recent_turns] == [("user", "안녕")]
