"""① /search 의 요청 모양과 검사 규칙. 라우터와 검색 로직이 같이 쓴다."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.validation import INT32_MAX, INT32_MIN, sanitize_string

# 명세 ①
MAX_QUERY_CHARS = 200
DEFAULT_SIZE = 15
MAX_SIZE = 50

Sort = Literal["relevance", "newest", "price_asc", "price_desc", "popular"]


class _Strict(BaseModel):
    # "15" 같은 문자열을 숫자로 슬쩍 바꿔 받지 않는다. 타입이 계약과 다르면 400 이다.
    model_config = ConfigDict(strict=True)

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


class SearchFilters(_Strict):
    category: str | None = None
    price_min: int | None = Field(default=None, ge=0, le=INT32_MAX)
    price_max: int | None = Field(default=None, ge=0, le=INT32_MAX)
    pub_year_from: int | None = Field(default=None, ge=INT32_MIN, le=INT32_MAX)
    pub_year_to: int | None = Field(default=None, ge=INT32_MIN, le=INT32_MAX)
    in_stock_only: bool = False

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> Self:
        # 뒤집힌 구간은 조용히 0건이 되어 "책이 없다"로 보인다. 요청 오류로 알린다.
        if (
            self.price_min is not None
            and self.price_max is not None
            and self.price_min > self.price_max
        ):
            raise ValueError("price_min > price_max")
        if (
            self.pub_year_from is not None
            and self.pub_year_to is not None
            and self.pub_year_from > self.pub_year_to
        ):
            raise ValueError("pub_year_from > pub_year_to")
        return self


class SearchRequest(_Strict):
    query: str = Field(max_length=MAX_QUERY_CHARS)
    filters: SearchFilters = Field(default_factory=SearchFilters)
    sort: Sort = "relevance"
    cursor: str | None = None
    size: int = Field(default=DEFAULT_SIZE, ge=1, le=MAX_SIZE)
