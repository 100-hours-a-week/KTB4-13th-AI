"""④ /recommendations/feed 의 요청 모양과 검사 규칙.

요청 본문 없이 쿼리 파라미터로 받는다(명세 ④). 쿼리 값은 모두 글자로 오므로, ①⑥과 달리
"15" 같은 숫자 글자는 숫자로 읽는다. 대신 모르는 키와 같은 키 두 번은 막는다.
"""

from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.core.validation import INT32_MAX, INT32_MIN, sanitize_string

DEFAULT_SIZE = 15
MAX_SIZE = 50

Surface = Literal["home", "recommend_more"]
Sort = Literal["match", "newest", "price_asc"]

# home 은 정렬·필터를 받지 않는다(명세 ④). 보내면 무시하지 않고 400 이다 — 무시하면 BE 가
# 잘못 보내도 알 수 없다(#105).
_RECOMMEND_MORE_ONLY = (
    "sort",
    "category",
    "pub_year_from",
    "pub_year_to",
    "match_score_min",
)


class FeedRequest(BaseModel):
    # 허용 파라미터 밖의 키가 오면 400(명세 ④).
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(ge=INT32_MIN, le=INT32_MAX)
    surface: Surface
    sort: Sort = "match"
    category: str | None = Field(default=None, min_length=1)
    pub_year_from: int | None = Field(default=None, ge=INT32_MIN, le=INT32_MAX)
    pub_year_to: int | None = Field(default=None, ge=INT32_MIN, le=INT32_MAX)
    match_score_min: int | None = Field(default=None, ge=0, le=100)
    size: int = Field(default=DEFAULT_SIZE, ge=1, le=MAX_SIZE)
    cursor: str | None = Field(default=None, min_length=1)

    # NUL·짝 없는 서로게이트가 DB나 인코딩 단계에서야 터지면 500이 난다.
    # 여기서 걸러 400으로 만든다(#201).
    @field_validator("category", "cursor", mode="after")
    @classmethod
    def _sanitize(cls, value: str | None) -> str | None:
        return sanitize_string(value) if value is not None else value

    @model_validator(mode="after")
    def _check_rules(self) -> Self:
        if self.surface == "home":
            given = self.model_fields_set.intersection(_RECOMMEND_MORE_ONLY)
            if given:
                raise ValueError(f"home 은 정렬·필터를 받지 않는다: {sorted(given)}")
        # 뒤집힌 구간은 조용히 0건이 되어 "책이 없다"로 보인다. 요청 오류로 알린다(①과 같음).
        if (
            self.pub_year_from is not None
            and self.pub_year_to is not None
            and self.pub_year_from > self.pub_year_to
        ):
            raise ValueError("pub_year_from > pub_year_to")
        return self


def parse_query(items: list[tuple[str, str]]) -> FeedRequest | None:
    """쿼리 (키, 값) 목록이 계약에 맞으면 요청 객체, 아니면 None.

    같은 키가 두 번 오면(user_id=1&user_id=2) 어느 값을 쓸지 모호해 None 이다.
    """
    keys = [key for key, _ in items]
    if len(keys) != len(set(keys)):
        return None
    try:
        return FeedRequest.model_validate(dict(items))
    except ValidationError:
        return None
