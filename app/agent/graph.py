"""⑦ LLM 이 도구를 고르고 코드가 검사·실행하는 반복(#356). LangGraph 로 만든다(9/21 팀 결정).

순서: 도구 고르기(LLM) → 부른 도구를 순서대로 검사하고 실행(코드, tools.execute) → 결과를 LLM 에게 돌려주고
다시 고르기. 최대 MAX_TOOL_STEPS 번 반복하고, 다 썼으면 마지막 호출은 도구를 못 쓰게 해서 항상 글로 된
답으로 끝낸다. 결과는 /agent/act 응답이 아니라 "그래프 실행 결과"(AgentRun)다. 응답 조립과 숫자·행동
검사는 마무리 단계(7단계)가 한다.

LLM 호출은 invoke_chain 을 거친다. /health 의 LLM 상태와 provider 예외 변환을 ③⑤와 같은 길로 묶기 위해서다.
"""

import json
import operator
from dataclasses import dataclass
from time import perf_counter
from typing import Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END, START, StateGraph

from app.agent import descriptors, prompts
from app.agent.compare import Comparer
from app.agent.errors import ToolErrorCode
from app.agent.reference import TurnContext
from app.agent.schemas import AgentActRequest, ToolCall, ToolName
from app.agent.tools import execute
from app.agent.view import llm_view
from app.gateway.commerce import CommerceError, CommerceGateway
from app.gateway.llm import invoke_chain

# 명세 ⑦: tool 반복은 최대 3번
MAX_TOOL_STEPS = 3
# 반복 상한은 횟수만 막는다. LLM 이 한 번에 수십 개를 부르는 것도 막는다.
MAX_CALLS_PER_STEP = 5


@dataclass(frozen=True)
class StepTiming:
    kind: Literal["llm", "tools"]
    ms: float
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class AgentRun:
    """그래프를 한 번 돌린 결과."""

    final_text: str  # LLM 의 마지막 글. 비어 있을 수 있다
    not_shopping: bool  # 도구 없이 NOT_SHOPPING 으로만 답했다
    tool_calls: list[ToolCall]  # 실행했거나 막힌 호출의 기록(순서대로)
    iterations: int  # 도구를 실행한 반복 수(0~MAX_TOOL_STEPS)
    llm_calls: int
    hit_cap: bool  # 반복을 상한까지 다 썼다
    timings: list[StepTiming]
    ctx: TurnContext  # 끝난 뒤의 상태(마지막으로 조회한 장바구니 등)


class _State(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    tool_calls: Annotated[list[ToolCall], operator.add]
    timings: Annotated[list[StepTiming], operator.add]
    iterations: int
    llm_calls: int


def _error_message(
    call_id: str | None, name: str | None, code: ToolErrorCode
) -> ToolMessage:
    """실행하지 않은 호출에도 LLM 에게 답을 돌려줘야 한다(도구 호출마다 결과 메시지가 있어야 하는 규칙)."""
    return ToolMessage(
        content=json.dumps({"ok": False, "error": code.value}),
        tool_call_id=call_id or "unknown",
        name=name,
    )


def _text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") if isinstance(part, dict) else str(part)
        for part in content
    )


def build_graph(
    model: Any,
    gateway: CommerceGateway,
    ctx: TurnContext,
    comparer: Comparer | None = None,
):
    """model 은 bind_tools 가 되는 LLM(ChatOpenAI 나 ScriptedModel)이다."""
    tools = descriptors.tool_definitions(ctx.allow_tools)
    # 허용한 도구가 하나도 없으면 도구 없이 부른다(빈 도구 목록은 공급자가 거절한다)
    can_choose = model.bind_tools(tools) if tools else model
    # 반복을 다 썼을 때의 마지막 호출. 도구는 그대로 두고 부르지만 못 쓰게 한다
    must_answer = model.bind_tools(tools, tool_choice="none") if tools else model

    async def choose(state: _State) -> dict[str, Any]:
        runnable = can_choose if state["iterations"] < MAX_TOOL_STEPS else must_answer
        started = perf_counter()
        ai: AIMessage = await invoke_chain(runnable, state["messages"])
        usage = getattr(ai, "usage_metadata", None) or {}
        timing = StepTiming(
            "llm",
            (perf_counter() - started) * 1000,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
        )
        return {
            "messages": [ai],
            "timings": [timing],
            "llm_calls": state["llm_calls"] + 1,
        }

    def route(state: _State) -> Literal["act", "end"]:
        last = state["messages"][-1]
        wants_tools = bool(
            getattr(last, "tool_calls", None)
            or getattr(last, "invalid_tool_calls", None)
        )
        # 도구를 못 쓰게 한 마지막 호출이 그래도 도구를 부르면 실행하지 않고 끝낸다
        if wants_tools and state["iterations"] < MAX_TOOL_STEPS:
            return "act"
        return "end"

    async def act(state: _State) -> dict[str, Any]:
        ai = state["messages"][-1]
        started = perf_counter()
        replies: list[ToolMessage] = []
        recorded: list[ToolCall] = []

        # 순서대로 하나씩 실행한다. 장바구니 번호는 LLM 이 본 목록 기준이라 순서가 바뀌면 안 된다
        for index, requested in enumerate(ai.tool_calls):
            name = descriptors.FROM_LLM_NAME.get(requested["name"])
            if name is None:
                replies.append(
                    _error_message(
                        requested["id"], requested["name"], ToolErrorCode.UNKNOWN_TOOL
                    )
                )
                continue
            if index >= MAX_CALLS_PER_STEP:
                replies.append(
                    _error_message(
                        requested["id"], requested["name"], ToolErrorCode.TOO_MANY_CALLS
                    )
                )
                continue
            call = await execute(
                ctx, gateway, name, requested["args"], comparer=comparer
            )
            recorded.append(call)
            replies.append(
                ToolMessage(
                    content=json.dumps(llm_view(ctx, call), ensure_ascii=False),
                    tool_call_id=requested["id"],
                    name=requested["name"],
                )
            )
        # JSON 이 깨진 호출. 실행하지 않고 이유만 돌려준다
        for index, broken in enumerate(getattr(ai, "invalid_tool_calls", None) or []):
            replies.append(
                _error_message(
                    broken.get("id") or f"invalid-{index}",
                    broken.get("name"),
                    ToolErrorCode.INVALID_ARGUMENTS,
                )
            )

        # 이 묶음의 결과를 LLM 에게 돌려주는 지금부터, 이 묶음에서 조회한 장바구니가 번호의 기준이다.
        # 묶음 안에서는 올리지 않는다. LLM 이 아직 받지 못한 목록으로 "장바구니 n번째"를 풀면 안 된다.
        ctx.commit_cart_view()

        timing = StepTiming("tools", (perf_counter() - started) * 1000)
        return {
            "messages": replies,
            "tool_calls": recorded,
            "timings": [timing],
            "iterations": state["iterations"] + 1,
        }

    builder = StateGraph(_State)
    builder.add_node("choose", choose)
    builder.add_node("act", act)
    builder.add_edge(START, "choose")
    builder.add_conditional_edges("choose", route, {"act": "act", "end": END})
    builder.add_edge("act", "choose")
    return builder.compile()


# 장바구니를 다루는 도구. 이 중 하나라도 쓸 수 있어야 장바구니를 미리 읽어 보여 준다(allow_tools 를 지킨다)
_CART_TOOLS = frozenset(
    {ToolName.CART_VIEW, ToolName.CART_ADD, ToolName.CART_UPDATE, ToolName.CART_REMOVE}
)


async def prefetch_cart(ctx: TurnContext, gateway: CommerceGateway) -> None:
    """로그인한 사용자의 장바구니를 코드가 먼저 읽어 번호를 풀 목록으로 삼는다(#387).

    "파이썬 책 3권으로 바꿔줘"처럼 장바구니 속 책을 제목으로 말하면, LLM 이 장바구니를 보기 전에는 어느 책인지
    몰라 되묻거나 조회와 번호를 한 묶음에 섞었다(기준선의 수량 변경 33%). 번호 붙은 목록을 처음부터 보여 준다.
    읽지 못하면(BE 거절, 로그아웃) 지금까지와 같이 LLM 이 cart.view 로 읽는다.
    """
    if not ctx.logged_in:
        return
    if ctx.allow_tools is not None and not _CART_TOOLS & set(ctx.allow_tools):
        return
    try:
        ctx.cart_snapshot = await gateway.get_cart(ctx.user_id)
    except CommerceError:
        return


async def run_agent(
    req: AgentActRequest,
    *,
    model: Any,
    gateway: CommerceGateway,
    comparer: Comparer | None = None,
) -> AgentRun:
    """요청 하나를 그래프로 처리한다. LLM 장애는 LLMUnavailableError 로 올라간다.

    comparer 가 없으면 book.compare 는 쓸 수 없다. 소개글을 읽는 호출은 도구를 고르는 model 과 따로
    한다(소개글 속 지시문이 도구 선택에 섞이지 않게).
    """
    ctx = TurnContext.from_request(req)
    await prefetch_cart(ctx, gateway)
    graph = build_graph(model, gateway, ctx, comparer)
    out: _State = await graph.ainvoke(
        {
            "messages": [
                SystemMessage(prompts.SYSTEM_PROMPT),
                SystemMessage(prompts.state_message(ctx)),
                HumanMessage(req.message),
            ],
            "tool_calls": [],
            "timings": [],
            "iterations": 0,
            "llm_calls": 0,
        }
    )
    final_text = _text(out["messages"][-1])
    return AgentRun(
        final_text=final_text,
        not_shopping=not out["tool_calls"]
        and final_text.strip() == prompts.NOT_SHOPPING,
        tool_calls=out["tool_calls"],
        iterations=out["iterations"],
        llm_calls=out["llm_calls"],
        hit_cap=out["iterations"] >= MAX_TOOL_STEPS,
        timings=out["timings"],
        ctx=ctx,
    )
