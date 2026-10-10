"""⑥ /preferences/profile 의 요청 모양과 검사 규칙."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.config import get_settings
from app.core.isbn import Isbn13
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
# 별점의 위쪽 끝. 가중치는 4.0 이상을 "좋음"으로 친다(app/core/history.py). BE 코드는 10.0 까지
# 받아서(#307 남은 결정), 10점 만점으로 오면 거의 모든 리뷰가 좋음이 되고 에러가 나지 않는다.
MAX_RATING = 5.0


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


def _null_as_empty(value: object) -> object:
    # BE 는 비어 있는 목록을 null 로 보낼 수 있다(#301). 빈 목록과 같게 본다.
    return [] if value is None else value


class Onboarding(_Strict):
    reading_times: list[str] = Field(default_factory=list, max_length=MAX_READING_TIMES)
    criteria: list[str] = Field(default_factory=list, max_length=MAX_CRITERIA)
    categories: list[str] = Field(default_factory=list, max_length=MAX_CATEGORIES)
    tags: list[str] = Field(default_factory=list, max_length=MAX_TAGS)
    liked_book_ids: list[Annotated[int, Field(ge=INT32_MIN, le=INT32_MAX)]] = Field(
        default_factory=list
    )
    # 같은 것을 ISBN 으로 받는 칸(#308). BE 가 ISBN 으로 옮기는 동안은 둘 다 받고, 다 옮기면
    # liked_book_ids 를 뺀다.
    liked_isbns: list[Isbn13] = Field(default_factory=list)

    # 온보딩의 칸은 전부 목록이다. 취향 카드를 지우면 그 칸이 null 로 온다.
    @field_validator("*", mode="before")
    @classmethod
    def _null_is_empty(cls, value: object) -> object:
        return _null_as_empty(value)


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


class Review(_Strict):
    isbn: Isbn13
    rating: float = Field(ge=0, le=MAX_RATING)
    # 프로필 계산은 별점만 쓴다. 본문 없는 리뷰 하나 때문에 요청 전체를 400 으로 돌려보내지 않는다.
    content: str | None = None


class History(_Strict):
    """산 책·나의 도서관 책·리뷰(#327). BE 가 부를 때마다 전체를 실어 보낸다."""

    # 결제가 끝났고 취소되지 않은 것만 온다.
    purchased_isbns: list[Isbn13] = Field(default_factory=list)
    library_isbns: list[Isbn13] = Field(default_factory=list)
    # 지워지지 않은 것만 온다.
    reviews: list[Review] = Field(default_factory=list)

    @field_validator("*", mode="before")
    @classmethod
    def _null_is_empty(cls, value: object) -> object:
        return _null_as_empty(value)


class ProfileRequest(_Strict):
    user_id: int = Field(ge=INT32_MIN, le=INT32_MAX)
    # 빈 키끼리는 모두 같은 키가 되어 멱등 처리에서 서로 부딪친다.
    idempotency_key: str = Field(min_length=1)
    # 온보딩을 건너뛴 사용자는 {} 를 보낸다. 칸 자체가 없으면 400 이다(명세).
    onboarding: Onboarding
    memories: list[Memory] = Field(default_factory=list)
    # 칸이 없거나 null 이면 BE 가 아직 보내지 않는 것이다. 이때는 복제 표의 이력으로 계산한다.
    # 빈 객체나 빈 목록은 "이력이 없다"는 뜻이라 None 과 다르다.
    history: History | None = None

    @field_validator("memories", mode="before")
    @classmethod
    def _null_is_empty(cls, value: object) -> object:
        return _null_as_empty(value)

    def used_liked_book_ids(self) -> list[int]:
        return self.onboarding.liked_book_ids[:USED_LIKED_BOOKS]

    def used_liked_isbns(self) -> list[str]:
        return self.onboarding.liked_isbns[:USED_LIKED_BOOKS]

    def used_memories(self) -> list[Memory]:
        return self.memories[-USED_MEMORIES:]
