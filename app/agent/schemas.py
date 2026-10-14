"""⑦ /agent/act 의 요청·응답 모양과 요청 검사 규칙(명세 ⑦, #350).

명세: 위키 저장소 docs/ai/1-model-api/spec.md ⑦ (PR #225). 책은 ISBN 으로만 가리킨다(#307).
요청은 BE 가 보내므로 계약에 안 맞으면 400 이고, 응답은 이 모델로 만들어 모양이 어긋나지 않게 한다.
"""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from app.core.isbn import Isbn13
from app.core.validation import INT32_MAX, INT32_MIN, sanitize_string

# 명세 ⑦: message 는 1–200자, context_cards 는 최대 10개(넘으면 400).
MAX_MESSAGE_CHARS = 200
MAX_CONTEXT_CARDS = 10
# 명세에 숫자가 없다. ⑤ 의 conversation_id 와 같은 상한을 둔다.
MAX_CONVERSATION_ID_CHARS = 200
# 최근 대화(#397). ③ 의 recent_turns 와 같다: 한 턴 200자, 20턴을 넘으면 최근 20턴만 쓴다.
# 다른 점은 한 줄이 200자를 넘을 때다. ③ 은 400 이고 ⑦ 은 뒤쪽 200자만 쓴다(#402).
MAX_RECENT_TURNS = 20
MAX_TURN_CHARS = 200


class ToolName(StrEnum):
    """tool_calls[].name 과 allow_tools 에 올 수 있는 이름. 명세 ⑦ 의 tool 표와 같다."""

    # BE 에 시키는 것
    CART_VIEW = "cart.view"
    CART_ADD = "cart.add"
    CART_UPDATE = "cart.update"
    CART_REMOVE = "cart.remove"
    BOOK_OFFER = "book.offer"
    # AI 서버 안에서 처리하는 것
    SELECTION_PICK = "selection.pick"
    BOOK_COMPARE = "book.compare"
    UI_GUIDE = "ui.guide"


class _Strict(BaseModel):
    # "123" 을 숫자로, "true" 를 참으로 슬쩍 바꿔 받지 않는다.
    model_config = ConfigDict(strict=True)

    # NUL·짝 없는 서로게이트가 인코딩 단계에서야 터지면 500이 난다. 여기서 걸러 400으로 만든다(#201).
    @field_validator("*", mode="after")
    @classmethod
    def _sanitize(cls, value: object) -> object:
        if isinstance(value, str):
            return sanitize_string(value)
        if isinstance(value, list):
            return [sanitize_string(v) if isinstance(v, str) else v for v in value]
        return value


# --- 요청 ---


class Turn(_Strict):
    """최근 대화 한 줄. 사용자와 에이전트(AI)의 말이다."""

    role: Literal["user", "assistant"]
    text: str = Field(max_length=MAX_TURN_CHARS)


class ContextCard(_Strict):
    """직전 턴에 보여 준 추천 카드. 가격과 재고는 받지 않는다(이번 턴의 tool 결과로만 말한다)."""

    isbn: Isbn13
    rank: int = Field(ge=1)
    match_score: int = Field(ge=0, le=100)


class UserContext(_Strict):
    logged_in: bool


def _trim_turn(item: object) -> object:
    """한 줄이 200자를 넘으면 뒤쪽 200자만 남긴다(#402).

    ⑦ 의 답변에는 길이 상한이 없어서 호출자가 그대로 실으면 넘을 수 있다. 400 으로 거절하면 긴 답 다음 턴
    전체가 실패한다. 참고용 문맥이라 잘라도 손해가 적고, 제안과 질문("~담을까요?")은 말 끝에 있어서 뒤쪽을
    남긴다. 형식이 틀린 항목은 그대로 두어 strict 검사가 400 으로 거절한다.
    """
    if (
        isinstance(item, dict)
        and isinstance(item.get("text"), str)
        and len(item["text"]) > MAX_TURN_CHARS
    ):
        return {**item, "text": item["text"][-MAX_TURN_CHARS:]}
    return item


class AgentActRequest(_Strict):
    # LLM 이 정하지 않고 이 값을 그대로 쓴다.
    user_id: int = Field(ge=INT32_MIN, le=INT32_MAX)
    conversation_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_CONVERSATION_ID_CHARS
    )
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    context_cards: list[ContextCard] = Field(
        default_factory=list, max_length=MAX_CONTEXT_CARDS
    )
    focused_isbn: Isbn13 | None = None
    # 이번 말 앞의 대화. 되물은 뒤의 "1번이요", "네"가 무슨 행동인지 알려면 필요하다(#397). 서버는 대화를
    # 저장하지 않으므로 BE 가 보낸다. 20턴을 넘으면 최근 20턴만 쓰고(③과 같다), 한 줄이 200자를 넘으면 뒤쪽
    # 200자만 쓴다(③은 400 이지만 ⑦ 의 답변은 길이 상한이 없어서 다르다, #402). 항목 형식이 틀리면 400 이다.
    recent_turns: list[Turn] = Field(default_factory=list)
    user_context: UserContext
    allow_tools: list[ToolName] | None = None
    idempotency_key: str = Field(min_length=1)

    @field_validator("message", mode="after")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message가 비어있음")
        return value

    @field_validator("recent_turns", mode="before")
    @classmethod
    def _recent_turns_tail(cls, value: object) -> object:
        # null 은 빈 목록이다. 줄 수 상한을 넘으면 앞을 자르고 최근 것만 쓴다(③과 같다, 20턴).
        # 한 줄이 200자를 넘으면 뒤쪽 200자만 쓴다(_trim_turn)
        if value is None:
            return []
        if isinstance(value, list):
            return [_trim_turn(item) for item in value[-MAX_RECENT_TURNS:]]
        return value

    # BE 는 비어 있는 목록을 null 로 보낼 수 있다(⑤⑥과 같게).
    @field_validator("context_cards", mode="before")
    @classmethod
    def _null_is_empty(cls, value: object) -> object:
        return [] if value is None else value

    # strict 모드는 문자열을 Enum 으로 바꿔 주지 않아서 직접 바꾼다. 모르는 이름은 400 이다(명세 ⑦).
    @field_validator("allow_tools", mode="before")
    @classmethod
    def _known_tool_names(cls, value: object) -> object:
        if not isinstance(value, list):
            return value  # None 이거나 목록이 아니면 strict 검사가 판단한다
        names = []
        for item in value:
            try:
                names.append(ToolName(item))
            except (ValueError, TypeError):
                raise ValueError("모르는 tool 이름") from None
        return names


def parse_request(payload: Any) -> AgentActRequest | None:
    """계약에 맞으면 요청 객체, 아니면 None 을 돌려준다."""
    if not isinstance(payload, dict):
        return None
    try:
        return AgentActRequest.model_validate(payload)
    except ValidationError:
        return None


# --- 응답 ---


class _Out(BaseModel):
    # 응답을 만드는 쪽(4~7단계)이 칸 이름을 틀리면 조용히 사라지지 않고 바로 터지게 한다.
    model_config = ConfigDict(extra="forbid")


class ToolCall(_Out):
    name: ToolName
    arguments: dict[str, Any]
    result: dict[str, Any]
    # false 면 그 결과가 답변 내용을 뒷받침하지 못한다(grounding 규칙)
    grounded: bool


class ResolvedReference(_Out):
    """ "이거, 1번"이 가리키는 책. 하나로 정해지지 않으면 isbns 가 빈 목록이다."""

    ref: str
    isbns: list[Isbn13]


class SelectionConstraints(_Out):
    budget_total: int
    count: int


class SelectionItem(_Out):
    isbn: Isbn13
    price: int
    match_score: int


class Selection(_Out):
    goal: Literal["budget_pick"]
    constraints: SelectionConstraints
    chosen: list[SelectionItem]
    total_price: int
    alternatives_considered: int


ButtonAction = Literal[
    "confirm_reference",
    "confirm_selection",
    "reselect",
    "open_book",
    "navigate",
    "dismiss",
]

# action 마다 따라오는 칸이 정해져 있다(명세 5장 action 표). 없으면 None.
_COMPANION_FIELD: dict[str, str | None] = {
    "confirm_reference": "isbn",
    "open_book": "isbn",
    "confirm_selection": "isbns",
    "navigate": "target",
    "reselect": None,
    "dismiss": None,
}


class Button(_Out):
    label: str
    action: ButtonAction
    isbn: Isbn13 | None = None
    isbns: list[Isbn13] | None = Field(default=None, min_length=1)
    target: str | None = None

    @model_validator(mode="after")
    def _companion_field(self) -> Self:
        expected = {_COMPANION_FIELD[self.action]} - {None}
        present = {
            name
            for name in ("isbn", "isbns", "target")
            if getattr(self, name) is not None
        }
        if present != expected:
            raise ValueError(
                f"{self.action} 버튼은 {sorted(expected) or '추가 칸 없음'}만 가져야 한다"
            )
        return self

    # 명세 예시는 해당하는 칸만 싣는다. 비어 있는 칸을 null 로 내보내지 않는다.
    @model_serializer(mode="wrap")
    def _drop_unused(self, handler: Any) -> dict[str, Any]:
        return {k: v for k, v in handler(self).items() if v is not None}


class AgentActData(_Out):
    reply: str
    # true 면 쇼핑 요청이 아니라서 아무것도 하지 않았다. 호출자는 일반 챗봇 흐름으로 처리한다.
    not_shopping: bool
    tool_calls: list[ToolCall]
    resolved_reference: ResolvedReference | None
    selection: Selection | None
    buttons: list[Button]

    @model_validator(mode="after")
    def _not_shopping_does_nothing(self) -> Self:
        if self.not_shopping and (
            self.reply
            or self.tool_calls
            or self.resolved_reference
            or self.selection
            or self.buttons
        ):
            raise ValueError(
                "not_shopping 이면 아무것도 하지 않았으므로 나머지가 비어 있어야 한다"
            )
        return self

    @classmethod
    def not_shopping_reply(cls) -> Self:
        return cls(
            reply="",
            not_shopping=True,
            tool_calls=[],
            resolved_reference=None,
            selection=None,
            buttons=[],
        )
