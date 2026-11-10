"""POST /shelf/scans 의 요청·응답 모양(#615, BE #509). 라우터와 테스트가 같이 쓴다."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.validation import INT32_MAX, INT32_MIN, sanitize_string
from app.photo.scan import Item, ScanResult

# S3 미리 서명한 주소는 임시 자격 증명(X-Amz-Security-Token)이 붙으면 2천 자 안팎이다. 넉넉하게 잡는다
MAX_IMAGE_REF_CHARS = 8192


class ScanRequest(BaseModel):
    # "123" 같은 문자열을 숫자로 슬쩍 바꿔 받지 않는다. 타입이 계약과 다르면 400 이다
    model_config = ConfigDict(strict=True)

    # 누가 찍었는지는 책을 찾는 데 쓰지 않는다. 계약(BE #509)에 맞춰 받고 검사만 한다
    user_id: int = Field(ge=INT32_MIN, le=INT32_MAX)
    # AI 가 사진을 읽을 S3 GET 주소. 허용한 주소인지는 사진을 받을 때 본다(app/photo/fetch.py)
    image_ref: str = Field(min_length=1, max_length=MAX_IMAGE_REF_CHARS)

    @field_validator("image_ref", mode="after")
    @classmethod
    def _sanitize(cls, value: str) -> str:
        return sanitize_string(value)


def parse_request(payload: Any) -> ScanRequest | None:
    """계약에 맞으면 요청 객체, 아니면 None. 모르는 칸은 400 이 아니라 무시한다(다른 API 와 같다)."""
    if not isinstance(payload, dict):
        return None
    try:
        return ScanRequest.model_validate(payload)
    except ValidationError:
        return None


def _item(item: Item) -> dict[str, Any]:
    book = item.book
    return {
        "state": item.state,
        "read_text": item.read_text,
        "book": None
        if book is None
        else {
            "book_id": book.book_id,
            "isbn": book.isbn,
            "title": book.title,
            "author": book.author,
            "publisher": book.publisher,
            "year": book.pub_year,
            "cover_url": book.cover_url,
        },
        # 책장 위 책 위치는 아직 주지 않는다. 지금은 모델이 글자만 옮겨 적고 윤곽은 찾지 않는다
        "spines": [],
    }


def to_data(result: ScanResult) -> dict[str, Any]:
    return {
        "photo_type": result.photo_type,
        "items": [_item(item) for item in result.items],
    }
