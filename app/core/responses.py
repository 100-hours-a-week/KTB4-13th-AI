"""공통 응답 형식 { message, data }.

FastAPI 기본 오류는 `{"detail": ...}` 이라 명세의 형식과 다르다.
공통 예외 핸들러가 들어오기 전까지 각 라우터가 이 함수로 직접 맞춘다.
"""

from typing import Any

from fastapi.responses import JSONResponse


def success(
    message: str,
    data: Any,
    headers: dict[str, str] | None = None,
    status: int = 200,
) -> JSONResponse:
    """status 는 200 이 아닌 성공(③ 폴링의 202 등)에만 넘긴다."""
    return JSONResponse(
        {"message": message, "data": data}, status_code=status, headers=headers
    )


def error(
    status: int, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    """성공과 같은 모양이고 data 만 null 이다."""
    return JSONResponse(
        {"message": message, "data": None}, status_code=status, headers=headers
    )
