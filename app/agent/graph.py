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
from app.agent.errors import ToolErrorCode
from app.agent.reference import TurnContext
from app.agent.schemas import AgentActRequest, ToolCall
from app.agent.tools import execute
from app.agent.view import llm_view
from app.gateway.commerce import CommerceGateway
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


def build_graph(model: Any, gateway: CommerceGateway, ctx: TurnContext):
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
            call = await execute(ctx, gateway, name, requested["args"])
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


async def run_agent(
    req: AgentActRequest, *, model: Any, gateway: CommerceGateway
) -> AgentRun:
    """요청 하나를 그래프로 처리한다. LLM 장애는 LLMUnavailableError 로 올라간다."""
    ctx = TurnContext.from_request(req)
    graph = build_graph(model, gateway, ctx)
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
