"""⑥ /preferences/profile 의 요청 모양과 검사 규칙."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import get_settings
from app.core.validation import INT32_MAX, INT32_MIN, sanitize_string

# 명세 ⑥: 넘으면 400 인 개수 상한
MAX_READING_TIMES = 5
MAX_CRITERIA = 3
MAX_CATEGORIES = 3
MAX_TAGS = 9
# 명세 ⑥: 넘어도 400 이 아니라 잘라 쓰는 개수. 좋아한 책은 앞에서부터, 기억은 최근 것부터.
# 요청에 항목별 시각이 없어 배열 순서로 가린다. 기억은 배열 뒤쪽을 최근으로 본다(이슈 #90).
USED_LIKED_BOOKS = 50
USED_MEMORIES = 500


class _Strict(BaseModel):
    # "123" 을 숫자로 슬쩍 바꿔 받지 않는다. NaN·Infinity 도 막는다 — 벡터에 하나만
    # 섞여도 가중평균이 통째로 NaN 이 되어 취향 벡터 전체가 쓸모없어진다.
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    # NUL·짝 없는 서로게이트가 DB나 인코딩 단계에서야 터지면 500이 난다.
    # 여기서 걸러 400으로 만든다(#201).
    @field_validator("*", mode="after")
    @classmethod
    def _sanitize(cls, value: object) -> object:
        if isinstance(value, str):
            return sanitize_string(value)
        if isinstance(value, list):
            return [sanitize_string(v) if isinstance(v, str) else v for v in value]
        return value


class Onboarding(_Strict):
    reading_times: list[str] = Field(default_factory=list, max_length=MAX_READING_TIMES)
    criteria: list[str] = Field(default_factory=list, max_length=MAX_CRITERIA)
    categories: list[str] = Field(default_factory=list, max_length=MAX_CATEGORIES)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    liked_book_ids: list[Annotated[int, Field(ge=INT32_MIN, le=INT32_MAX)]] = Field(
        default_factory=list
    )


class Memory(_Strict):
    type: str
    value: str
    vector: list[float]
    dim: int

    @model_validator(mode="after")
    def _matches_index(self) -> Self:
        # 차원이 다른 벡터는 책 벡터와 비교할 수 없다(명세 ⑥: 400).
        if self.dim != get_settings().embedding_dim:
            raise ValueError("dim 이 인덱스 차원과 다르다")
        # dim 만 맞고 길이가 다르면 검사는 통과하고 가중평균에서 500 이 난다.
        if len(self.vector) != self.dim:
            raise ValueError("vector 길이가 dim 과 다르다")
        return self


class ProfileRequest(_Strict):
    user_id: int = Field(ge=INT32_MIN, le=INT32_MAX)
    # 빈 키끼리는 모두 같은 키가 되어 멱등 처리에서 서로 부딪친다.
    idempotency_key: str = Field(min_length=1)
    # 온보딩을 건너뛴 사용자는 {} 를 보낸다. 칸 자체가 없으면 400 이다(명세).
    onboarding: Onboarding
    memories: list[Memory] = Field(default_factory=list)

    def used_liked_book_ids(self) -> list[int]:
        return self.onboarding.liked_book_ids[:USED_LIKED_BOOKS]

    def used_memories(self) -> list[Memory]:
        return self.memories[-USED_MEMORIES:]
