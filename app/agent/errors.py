"""⑦ tool 이 막히거나 거절됐을 때 결과에 적는 오류 코드(#354).

tool 은 예외를 던지지 않고 결과에 `{"ok": false, "error": "<코드>"}` 로 남긴다(명세 ⑦: BE tool 실패 흡수는
모두 200). BE 가 거절한 것은 BE 의 오류 이름(CommerceErrorCode)을 그대로 쓰고, 여기에는 AI 서버가 실행하기
전에 막은 것만 둔다. 코드 이름만 적고 책 제목이나 사용자 발화는 싣지 않는다.
"""

from enum import StrEnum


class ToolErrorCode(StrEnum):
    # 도구 자체를 못 쓴다
    TOOL_NOT_ALLOWED = "TOOL_NOT_ALLOWED"  # 이번 요청의 allow_tools 에 없다
    TOOL_NOT_IMPLEMENTED = (
        "TOOL_NOT_IMPLEMENTED"  # 아직 만들지 않았다(selection.pick 등)
    )
    LOGIN_REQUIRED = "LOGIN_REQUIRED"  # 로그인하지 않았는데 쓰기 도구다
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"  # 모르는 칸, 자료형, 빈 목록

    # 책을 하나로 가리킬 수 없다 — 실행하지 않는다
    BOOK_NOT_IN_CARDS = "BOOK_NOT_IN_CARDS"
    BOOK_NOT_IN_CART = "BOOK_NOT_IN_CART"
    CART_NOT_LOADED = "CART_NOT_LOADED"  # 장바구니를 조회하기 전에 장바구니 번호를 썼다
    NO_FOCUSED_BOOK = "NO_FOCUSED_BOOK"
    AMBIGUOUS_BOOK = "AMBIGUOUS_BOOK"

    # 도구가 일부러 안 한 것
    ALREADY_IN_CART = (
        "ALREADY_IN_CART"  # 이미 담긴 책이라 담지 않았다(담으면 수량이 줄 수 있다)
    )


class RefError(Exception):
    """책을 하나로 가리킬 수 없다. 실행하지 않고 이 코드를 결과에 남긴다."""

    def __init__(self, code: ToolErrorCode) -> None:
        super().__init__(code.value)
        self.code = code
