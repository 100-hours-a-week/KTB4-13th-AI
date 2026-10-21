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
        "TOOL_NOT_IMPLEMENTED"  # 이 도구를 실행할 부품이 연결되지 않았다
    )
    LOGIN_REQUIRED = "LOGIN_REQUIRED"  # 로그인하지 않았는데 쓰기 도구다
    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"  # 모르는 칸, 자료형, 빈 목록
    UNKNOWN_TOOL = "UNKNOWN_TOOL"  # LLM 이 없는 도구 이름을 불렀다(그래프가 막는다)
    TOO_MANY_CALLS = (
        "TOO_MANY_CALLS"  # 한 번에 부를 수 있는 도구 수를 넘었다(그래프가 막는다)
    )

    # 책을 하나로 가리킬 수 없다 — 실행하지 않는다
    BOOK_NOT_IN_CARDS = "BOOK_NOT_IN_CARDS"
    BOOK_NOT_IN_CART = "BOOK_NOT_IN_CART"
    CART_NOT_LOADED = "CART_NOT_LOADED"  # 장바구니를 조회하기 전에 장바구니 번호를 썼다
    NO_FOCUSED_BOOK = "NO_FOCUSED_BOOK"
    AMBIGUOUS_BOOK = "AMBIGUOUS_BOOK"
    # 사용자의 말에 그 책을 가리키는 번호·제목·"전부" 같은 근거가 없는데 LLM 이 골랐다(짐작해서 실행하지 않는다)
    REFERENCE_NOT_STATED = "REFERENCE_NOT_STATED"
    # 책은 정해졌지만 사용자가 담기, 빼기, 수량 변경을 시키지 않았다("1번이 마음에 들어", #531)
    ACTION_NOT_STATED = "ACTION_NOT_STATED"

    # 도구가 일부러 안 한 것
    ALREADY_IN_CART = (
        "ALREADY_IN_CART"  # 이미 담긴 책이라 담지 않았다(담으면 수량이 줄 수 있다)
    )
    # AI 가 이번 턴에 고른 책이라 사용자 확인 없이 담지 않았다(확인 버튼으로 담는다)
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    # 한 번에 시킨 쓰기 중 하나가 장바구니 규칙에 걸려서 이것도 되돌렸다(일부만 담기지 않게, #488 #489)
    BATCH_ROLLED_BACK = "BATCH_ROLLED_BACK"
    # 이번 턴에 장바구니를 이미 바꿨거나 조합을 이미 골라서 다른 쪽은 하지 않았다. 다음 말로 따로 해야 한다.
    # 골라 담기는 확인 버튼으로 담고 쓰기는 이미 했다고 말한 것이라 BE 가 한 응답에 같이 받지 않는다(#489)
    SEPARATE_TURN_REQUIRED = "SEPARATE_TURN_REQUIRED"
    # 예산 안에서 그 권수를 채울 조합이 없다(재고 있는 직전 카드 안에서만 고른다)
    NO_COMBINATION = "NO_COMBINATION"
    # 비교하려는 책에 소개글이 없다
    NO_DESCRIPTION = "NO_DESCRIPTION"
    # 소개글을 읽어 차이를 요약하는 호출이 실패했다
    COMPARE_FAILED = "COMPARE_FAILED"


class RefError(Exception):
    """책을 하나로 가리킬 수 없다. 실행하지 않고 이 코드를 결과에 남긴다."""

    def __init__(self, code: ToolErrorCode) -> None:
        super().__init__(code.value)
        self.code = code
