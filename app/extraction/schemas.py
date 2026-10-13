"""⑤ /preferences/extractions 의 요청 모양과 검사 규칙(명세 ⑤)."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.validation import INT32_MAX, INT32_MIN, sanitize_string

# 명세 ⑤: 대화는 최근 40턴만 쓴다. 넘어도 400 이 아니다.
MAX_CONVERSATION_TURNS = 40
# 명세 ⑤: 메시지 하나는 4,000자까지. 넘으면 400 이다.
MAX_TURN_CHARS = 4000
# 명세에 숫자가 없다. 뽑은 취향마다 출처로 되돌려 주는 값이라 X-Request-Id 와 같은 상한을 둔다.
MAX_CONVERSATION_ID_CHARS = 200


class _Strict(BaseModel):
    # "123" 을 숫자로, "true" 를 참으로 슬쩍 바꿔 받지 않는다.
    model_config = ConfigDict(strict=True)

    # NUL·짝 없는 서로게이트가 인코딩 단계에서야 터지면 500이 난다. 여기서 걸러 400으로 만든다(#201).
    @field_validator("*", mode="after")
    @classmethod
    def _sanitize(cls, value: object) -> object:
        if isinstance(value, str):
            return sanitize_string(value)
        return value


class Turn(_Strict):
    role: str = Field(min_length=1)
    text: str = Field(max_length=MAX_TURN_CHARS)


class ExistingPreference(_Strict):
    """이미 저장된 취향. 같은 취향을 또 뽑지 않으려고 받는다."""

    type: str
    value: str


class ExtractionRequest(_Strict):
    user_id: int = Field(ge=INT32_MIN, le=INT32_MAX)
    # 개인화 추천에 동의했는지(③과 같은 값). false 면 뽑지 않는다.
    consented: bool
    # 대화 세션 하나의 전체 턴. 빈 목록이면 뽑을 것이 없다.
    conversation: list[Turn]
    # 뽑은 취향의 출처로 그대로 되돌려 준다.
    conversation_id: str = Field(min_length=1, max_length=MAX_CONVERSATION_ID_CHARS)
    existing_preferences: list[ExistingPreference] = Field(default_factory=list)

    # BE 는 비어 있는 목록을 null 로 보낼 수 있다(⑥과 같게, #301).
    @field_validator("existing_preferences", mode="before")
    @classmethod
    def _null_is_empty(cls, value: object) -> object:
        return [] if value is None else value

    def used_conversation(self) -> list[Turn]:
        """뽑는 데 쓰는 턴. 대화가 길면 최근 것만 본다(명세 ⑤)."""
        return self.conversation[-MAX_CONVERSATION_TURNS:]


def parse_request(payload: Any) -> ExtractionRequest | None:
    """계약에 맞으면 요청 객체, 아니면 None 을 돌려준다."""
    if not isinstance(payload, dict):
        return None
    try:
        return ExtractionRequest.model_validate(payload)
    except ValidationError:
        return None
