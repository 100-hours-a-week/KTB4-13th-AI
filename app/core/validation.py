"""여러 스키마가 같이 쓰는 검사 — int32 범위·문자열 안전성(#201).

Postgres의 `integer` 컬럼은 int32 범위를 벗어나면 에러를 던지고, 문자열 컬럼은 NUL(0x00)을
아예 못 담는다. 짝 없는 서로게이트(예: JSON의 `\\ud800`처럼 escape 하나만 온 값)는 파이썬
str로는 만들어지지만 `.encode()`(UTF-8 전송·해시 계산 등 어디서든 결국 한 번은 한다)에서
`UnicodeEncodeError`(ValueError 상속)를 던진다 — search._fingerprint()의 `raw.encode()`가
실제 예. 스키마가 이 값들을 그냥 통과시키면 DB나 인코딩 단계에서야 터져서 요청 오류(400)
대신 서버 오류(500)로 새어나간다. 각 라우터가 이미 소유한 스키마 파일에서 이 함수들을 가져다
쓴다 — 검사 로직만 공유하고, 어느 필드에 적용할지는 각 엔드포인트가 정한다.
"""

INT32_MIN = -2_147_483_648
INT32_MAX = 2_147_483_647


def reject_nul(value: str) -> str:
    """문자열에 NUL이 있으면 ValueError — pydantic field_validator에서 그대로 쓴다."""
    if "\x00" in value:
        raise ValueError("NUL(0x00) 문자는 쓸 수 없다")
    return value


def reject_unencodable(value: str) -> str:
    """짝 없는 서로게이트처럼 UTF-8로 인코딩할 수 없는 문자열이면 ValueError."""
    try:
        value.encode()
    except UnicodeEncodeError as exc:
        raise ValueError(
            "인코딩할 수 없는 문자(예: 짝 없는 서로게이트)가 있다"
        ) from exc
    return value


def sanitize_string(value: str) -> str:
    """스키마 field_validator에서 부르는 한 곳 — NUL·인코딩 불가 문자를 함께 막는다."""
    return reject_unencodable(reject_nul(value))
