"""⑦ tool 을 실행하기 전에 막는 규칙(#354).

LLM 이 무엇을 고르든 이 검사를 통과하지 못하면 BE 를 부르지 않는다. 책을 가리킬 수 있는지(reference.py)와
BE 가 거절하는 것(수량, 재고, 한도)은 여기서 다루지 않는다.
"""

from app.agent.errors import ToolErrorCode
from app.agent.reference import TurnContext
from app.agent.schemas import ToolName

# 장바구니를 바꾸는 도구. 로그인하지 않았으면 막는다(읽기는 막지 않는다).
WRITE_TOOLS = frozenset({ToolName.CART_ADD, ToolName.CART_UPDATE, ToolName.CART_REMOVE})


def check(ctx: TurnContext, name: ToolName) -> ToolErrorCode | None:
    """실행해도 되면 None, 막아야 하면 그 이유."""
    if ctx.allow_tools is not None and name not in ctx.allow_tools:
        return ToolErrorCode.TOOL_NOT_ALLOWED
    if name in WRITE_TOOLS and not ctx.logged_in:
        return ToolErrorCode.LOGIN_REQUIRED
    return None
