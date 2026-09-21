"""③ /recommendations/chat 의 요청 모양과 spec 스키마. 라우터와 후보검색·카드생성이 같이 쓴다."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.search.schemas import SearchFilters

# 명세 ③
MAX_MESSAGE_CHARS = 200
MAX_RECENT_TURNS = 20
MAX_TURN_CHARS = 200


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True)


class SpecExact(_Strict):
    title: str | None = None
    author: str | None = None
    publisher: str | None = None


class Spec(_Strict):
    """추천 조건. 6개 키는 요청·응답 모두 항상 존재해야 한다(빈 값은 null/{}/[]).

    기본값을 일부러 안 둔다 — 기본값이 있으면 클라이언트가 키를 빠뜨려도
    조용히 채워져서 통과해버린다. 값이 비어도 되는 필드(semantic 등)는
    `X | None`으로 null은 허용하되, 키 자체는 빠지면 안 되게 한다.
    """

    intent: Literal["exact", "semantic"]
    exact: SpecExact
    filters: SearchFilters
    semantic: str | None
    anchor_book: int | None
    exclude: list[int]


class Turn(_Strict):
    role: str
    text: str = Field(max_length=MAX_TURN_CHARS)


class ChatRequest(_Strict):
    user_id: int
    consented: bool
    spec: Spec
    message: str | None = Field(default=None, max_length=MAX_MESSAGE_CHARS)
    recent_turns: list[Turn] = Field(default_factory=list)
    exclude_book_ids: list[int] = Field(default_factory=list)
    image_ref: str | None = None

    @model_validator(mode="after")
    def _message_xor_image(self) -> Self:
        # 명세: message 와 image_ref 가 둘 다 있거나 둘 다 없어도 400.
        # 이미지 턴은 후속 이슈라 지금은 image_ref 를 항상 None 으로만 받는다.
        if self.image_ref is not None:
            raise ValueError("image_ref는 아직 지원하지 않음(후속 이슈)")
        if not self.message or not self.message.strip():
            raise ValueError("message가 비어있음")
        return self
