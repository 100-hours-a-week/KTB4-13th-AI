"""⑦ 실행 전 검사(app/agent/guard.py) 테스트(#354). LLM 도 BE 도 쓰지 않는다."""

import pytest

from app.agent import guard
from app.agent.errors import ToolErrorCode
from app.agent.reference import TurnContext
from app.agent.schemas import ToolName

WRITE = [ToolName.CART_ADD, ToolName.CART_UPDATE, ToolName.CART_REMOVE]
READ = [ToolName.CART_VIEW, ToolName.BOOK_OFFER]


def _ctx(*, logged_in: bool = True, allow_tools=None) -> TurnContext:
    return TurnContext(
        user_id=1,
        logged_in=logged_in,
        cards=[],
        focused_isbn=None,
        allow_tools=allow_tools,
    )


def test_쓰기_도구는_장바구니를_바꾸는_세_개뿐이다() -> None:
    # 읽기에 쓰기를 섞어 두면 로그인 검사가 새 도구를 놓친다. 도구를 보태면 이 목록도 본다
    assert guard.WRITE_TOOLS == set(WRITE)


@pytest.mark.parametrize("name", list(ToolName))
def test_모두_허용하고_로그인했으면_통과한다(name) -> None:
    assert guard.check(_ctx(), name) is None


@pytest.mark.parametrize("name", WRITE)
def test_로그인하지_않았으면_쓰기_도구를_막는다(name) -> None:
    assert guard.check(_ctx(logged_in=False), name) == ToolErrorCode.LOGIN_REQUIRED


@pytest.mark.parametrize("name", READ)
def test_로그인하지_않아도_읽기_도구는_막지_않는다(name) -> None:
    assert guard.check(_ctx(logged_in=False), name) is None


def test_허용_목록에_없는_도구는_막는다() -> None:
    ctx = _ctx(allow_tools=[ToolName.CART_VIEW])

    assert guard.check(ctx, ToolName.CART_VIEW) is None
    assert guard.check(ctx, ToolName.CART_ADD) == ToolErrorCode.TOOL_NOT_ALLOWED
    assert guard.check(ctx, ToolName.BOOK_OFFER) == ToolErrorCode.TOOL_NOT_ALLOWED


def test_빈_허용_목록은_아무것도_허용하지_않는다() -> None:
    # None(모두 허용)과 [](아무것도 안 함)은 다르다
    ctx = _ctx(allow_tools=[])

    for name in ToolName:
        assert guard.check(ctx, name) == ToolErrorCode.TOOL_NOT_ALLOWED


def test_허용_목록_검사가_로그인_검사보다_먼저다() -> None:
    # 허용하지 않은 도구는 이유를 로그인으로 알려 주지 않는다
    ctx = _ctx(logged_in=False, allow_tools=[ToolName.CART_VIEW])

    assert guard.check(ctx, ToolName.CART_ADD) == ToolErrorCode.TOOL_NOT_ALLOWED


def test_허용한_쓰기_도구도_로그인하지_않았으면_막는다() -> None:
    ctx = _ctx(logged_in=False, allow_tools=[ToolName.CART_ADD])

    assert guard.check(ctx, ToolName.CART_ADD) == ToolErrorCode.LOGIN_REQUIRED
