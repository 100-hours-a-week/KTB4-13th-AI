"""⑦ LLM 이 도구를 고르고 코드가 검사·실행하는 반복(#356). LangGraph 로 만든다(9/21 팀 결정).

순서: 도구 고르기(LLM) → 부른 도구를 순서대로 검사하고 실행(코드, tools.execute) → 결과를 LLM 에게 돌려주고
다시 고르기. 최대 MAX_TOOL_STEPS 번 반복하고, 다 썼거나 턴 시간이 ANSWER_MARGIN_SECONDS 만큼만 남았으면 마지막
호출은 도구를 못 쓰게 해서 항상 글로 된 답으로 끝낸다. 결과는 /agent/act 응답이 아니라 "그래프 실행 결과"(AgentRun)다.
응답 조립과 숫자·행동 검사는 마무리 단계(7단계)가 한다.

LLM 호출은 invoke_chain 을 거친다. /health 의 LLM 상태와 provider 예외 변환을 ③⑤와 같은 길로 묶기 위해서다.
"""

import asyncio
import json
import operator
from dataclasses import dataclass, field
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

from app.agent import checkout as checkout_plan
from app.agent import descriptors, evidence, guard, prompts
from app.agent.compare import Comparer
from app.agent.errors import ToolErrorCode
from app.agent.reference import TurnContext
from app.agent.reviews import ReviewSummarizer
from app.agent.schemas import AgentActRequest, ToolCall, ToolName
from app.agent.tools import ARG_MODELS, delegated, execute, picked_ref, view_result
from app.agent.view import llm_view
from app.gateway.book_finder import BookFinder
from app.gateway.book_info import BookInfo, InfoUnavailable
from app.gateway.commerce import (
    CartLine,
    CommerceError,
    CommerceErrorCode,
    CommerceGateway,
    Restorable,
)
from app.gateway.commerce_snapshot import CartAction, diff_cart
from app.gateway.llm import invoke_chain

# 도구 반복 상한. 처음 명세는 3번이었다. 카드 밖 책 찾기(#631)가 들어오며 한 턴이 "장바구니 보기 → 주제마다 검색
# → 담기 → 품절이면 다시 찾아 담기"처럼 길어질 수 있어 넉넉히 6번으로 둔다. 반복이 길어져 느려지는 것은 횟수가
# 아니라 아래 시간 예산이 막는다. 모델이 도구를 더 부르려 할 때만 반복이 늘어나므로 짧은 턴은 그대로다.
MAX_TOOL_STEPS = 6
# 반복 상한은 횟수만 막는다. LLM 이 한 번에 수십 개를 부르는 것도 막는다.
MAX_CALLS_PER_STEP = 5
# 한 턴의 시간 제한(agent_turn_timeout_seconds)이 이만큼 남으면 도구를 더 쓰지 않고 답을 쓰게 한다. 제한을 넘기면
# 504 로 턴 전체가 실패해 그때까지 한 일도 버려지므로, 마지막 답 쓰기 호출이 끝날 시간을 남기고 멈춘다. 제한이
# 짧으면(평가·테스트) 제한의 1/3 만 남긴다. 운영의 30초에서는 10초다.
ANSWER_MARGIN_SECONDS = 10.0


def _deadline(turn_seconds: float | None) -> float | None:
    """도구를 그만 쓸 때(perf_counter 값). 제한이 없으면 None."""
    if turn_seconds is None:
        return None
    margin = min(ANSWER_MARGIN_SECONDS, turn_seconds / 3)
    return perf_counter() + max(0.0, turn_seconds - margin)


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
    # BE 가 반영할 장바구니 변경(#489). 처음과 끝 장바구니의 차이다
    actions: list[CartAction] = field(default_factory=list)
    # "주문해 줘"로 책을 담았을 때 주문 화면에 채울 쿠폰과 포인트(#631). 그 밖에는 None
    checkout: checkout_plan.Plan | None = None
    # 턴 시간이 다 되어 가서 도구를 그만 쓰고 답하게 했다(반복 상한 전에)
    out_of_time: bool = False


class _State(TypedDict):
    messages: Annotated[list[BaseMessage], operator.add]
    tool_calls: Annotated[list[ToolCall], operator.add]
    timings: Annotated[list[StepTiming], operator.add]
    iterations: int
    llm_calls: int
    # 마지막 도구 고르기 호출이 시간 예산을 넘겨 도구를 못 쓰게 한 호출이었다
    out_of_time: bool


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


def _reply(ctx: TurnContext, call: ToolCall, call_id: str, name: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(llm_view(ctx, call), ensure_ascii=False),
        tool_call_id=call_id,
        name=name,
    )


# 장바구니 규칙(재고, 30권, 수량, 판매 중지, 없는 항목)에 걸린 쓰기. 책을 가리키는 근거가 없어 막힌 것
# (REFERENCE_NOT_STATED 등)은 그 책만 되묻으면 되므로 넣지 않는다
_RULE_FAILURES = frozenset(code.value for code in CommerceErrorCode)


def _added(result: dict[str, Any]) -> dict[str, Any]:
    """코드가 담은 결과를 모델에게 보여 줄 모양으로. 성공이면 ok, 아니면 그 이유만."""
    if result.get("ok") is True:
        return {"ok": True}
    return {"ok": False, "error": result.get("error")}


def _roll_back(calls: list[ToolCall]) -> bool:
    """한 묶음에서 쓰기가 성공하기도 하고 장바구니 규칙에 걸리기도 했나."""
    writes = [c for c in calls if c.name in guard.WRITE_TOOLS]
    return any(c.result.get("ok") is True for c in writes) and any(
        c.result.get("error") in _RULE_FAILURES for c in writes
    )


async def _rolled_back(
    ctx: TurnContext, gateway: CommerceGateway, calls: list[ToolCall]
) -> list[ToolCall]:
    """되돌린 묶음의 기록. 성공한 쓰기는 되돌렸다고 적고, 규칙에 걸린 호출은 그 이유를 그대로 둔다.

    같은 묶음의 장바구니 조회는 되돌리기 전 장바구니를 읽었다. 되돌린 장바구니로 다시 만든다. 그대로 두면
    "담지 않았어요" 옆에 그 책이 담긴 목록과 합계가 LLM 에게 가고, 다음 묶음의 "장바구니 n번째"도 그 목록으로
    푼다(pending_cart, #493 리뷰).
    """
    viewed = any(c.name == ToolName.CART_VIEW and "items" in c.result for c in calls)
    lines = await gateway.get_cart(ctx.user_id) if viewed else None
    if lines is not None:
        ctx.pending_cart = lines
    failed = {"ok": False, "error": ToolErrorCode.BATCH_ROLLED_BACK.value}
    out: list[ToolCall] = []
    for call in calls:
        if call.name in guard.WRITE_TOOLS and call.result.get("ok") is True:
            call = call.model_copy(update={"result": failed})
        elif (
            lines is not None
            and call.name == ToolName.CART_VIEW
            and "items" in call.result
        ):
            call = call.model_copy(update={"result": view_result(lines)})
        out.append(call)
    return out


# 책 정보·리뷰(#650). 읽을 곳(info)이 있을 때만 준다
_INFO_TOOLS = frozenset({ToolName.BOOK_DETAIL, ToolName.BOOK_REVIEWS})


def _offered(
    ctx: TurnContext, finder: BookFinder | None, info: BookInfo | None = None
) -> list[ToolName] | None:
    """LLM 에게 줄 도구. 책 찾기는 찾을 곳(finder)이 있을 때만(#631), 책 정보·리뷰는 읽을 곳(info)이 있을 때만
    준다(#650). None 이면 전부다."""
    hidden: set[ToolName] = set()
    if finder is None:
        hidden.add(ToolName.BOOK_SEARCH)
    if info is None:
        hidden |= _INFO_TOOLS
    if not hidden:
        return ctx.allow_tools
    names = ctx.allow_tools if ctx.allow_tools is not None else list(ARG_MODELS)
    return [name for name in names if name not in hidden]


def build_graph(
    model: Any,
    gateway: CommerceGateway,
    ctx: TurnContext,
    comparer: Comparer | None = None,
    finder: BookFinder | None = None,
    deadline: float | None = None,
    info: BookInfo | None = None,
    summarizer: ReviewSummarizer | None = None,
):
    """model 은 bind_tools 가 되는 LLM(ChatOpenAI 나 ScriptedModel)이다.

    deadline(perf_counter 값)이 지나면 도구를 더 쓰지 않고 답하게 한다. None 이면 반복 상한만 본다.
    info 가 있으면 책 정보·리뷰 도구를 준다(#650).
    """
    tools = descriptors.tool_definitions(
        _offered(ctx, finder, info), checkout=ctx.checkout_enabled
    )
    # 허용한 도구가 하나도 없으면 도구 없이 부른다(빈 도구 목록은 공급자가 거절한다)
    can_choose = model.bind_tools(tools) if tools else model
    # 반복을 다 썼거나 시간이 다 되어 갈 때의 마지막 호출. 도구는 그대로 두고 부르지만 못 쓰게 한다
    must_answer = model.bind_tools(tools, tool_choice="none") if tools else model
    # 도구를 그만 쓸 마감까지 남은 초. 마감은 LLM 을 부르기 전에만 보므로, 한 반복 안에서 느린 검색이 차례로 돌아
    # 마감을 넘기지 않게 book.search 가 이것을 본다(#637 리뷰)
    time_left = None if deadline is None else (lambda: deadline - perf_counter())

    async def choose(state: _State) -> dict[str, Any]:
        out_of_time = deadline is not None and perf_counter() >= deadline
        may_use_tools = state["iterations"] < MAX_TOOL_STEPS and not out_of_time
        runnable = can_choose if may_use_tools else must_answer
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
            "out_of_time": out_of_time and state["iterations"] < MAX_TOOL_STEPS,
        }

    def route(state: _State) -> Literal["act", "end"]:
        last = state["messages"][-1]
        wants_tools = bool(
            getattr(last, "tool_calls", None)
            or getattr(last, "invalid_tool_calls", None)
        )
        # 도구를 못 쓰게 한 마지막 호출이 그래도 도구를 부르면 실행하지 않고 끝낸다
        if (
            wants_tools
            and state["iterations"] < MAX_TOOL_STEPS
            and not state["out_of_time"]
        ):
            return "act"
        return "end"

    async def act(state: _State) -> dict[str, Any]:
        ai = state["messages"][-1]
        started = perf_counter()
        replies: list[ToolMessage] = []
        recorded: list[ToolCall] = []
        # 실행한 호출의 (replies 안의 자리, LLM 이 준 호출 id·이름). 되돌린 호출의 답을 고쳐 쓸 때 쓴다
        slots: list[tuple[int, str, str]] = []
        # 한 번에 쓰기를 둘 이상 시키면 하나라도 장바구니 규칙에 걸릴 때 모두 되돌린다(#488, #489)
        writes = [
            r
            for r in ai.tool_calls
            if descriptors.FROM_LLM_NAME.get(r["name"]) in guard.WRITE_TOOLS
        ]
        point = (
            gateway.checkpoint()
            if len(writes) >= 2 and isinstance(gateway, Restorable)
            else None
        )

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
                ctx,
                gateway,
                name,
                requested["args"],
                comparer=comparer,
                finder=finder,
                time_left=time_left,
                info=info,
                summarizer=summarizer,
            )
            slots.append((len(replies), requested["id"], requested["name"]))
            recorded.append(call)
            replies.append(_reply(ctx, call, requested["id"], requested["name"]))
        if point is not None and _roll_back(recorded):
            gateway.restore(point)
            recorded = await _rolled_back(ctx, gateway, recorded)
            for (at, call_id, call_name), call in zip(slots, recorded, strict=True):
                replies[at] = _reply(ctx, call, call_id, call_name)
            # 되돌린 쓰기는 바꾼 것이 아니다. 앞 묶음에서 성공한 쓰기만 남았으면 그것으로 다시 정한다
            ctx.changed_cart = any(
                c.name in guard.WRITE_TOOLS and c.result.get("ok") is True
                for c in state["tool_calls"]
            )
        # 골라서 담으라고 맡긴 말이면 고른 책을 코드가 담는다(#631). 모델에게 맡기면 고르기만 하고 끝냈다
        # (평가 checkout-05). 담은 결과는 고르기 결과에 붙여 모델에게 돌려준다
        # 뒤에서부터 본다. 담은 기록을 고르기 기록 바로 뒤에 끼워 넣어도 앞의 자리가 밀리지 않는다
        for position in reversed(range(len(recorded))):
            call = recorded[position]
            if not (
                call.name == ToolName.SELECTION_PICK
                and call.result.get("ok") is True
                and delegated(ctx)
            ):
                continue
            adds = []
            # 고른 책은 한 묶음이다. 하나라도 장바구니 규칙에 걸리면 모두 되돌린다. 모델이 cart.add 를 한 번에 여럿
            # 시켰을 때와 같다(#488, #489). 코드가 담는 것은 위의 되돌리기 검사 뒤에 돌아서 여기서 따로 한다
            added_from = (
                gateway.checkpoint() if isinstance(gateway, Restorable) else None
            )
            # 한 번에 담는 수는 모델이 도구를 한 번에 부를 때와 같은 상한이다. 넘는 책은 담지 않고 이유를 알린다
            skipped: list[str] = []
            for book in call.result["chosen"]:
                ref = picked_ref(ctx, book["isbn"])
                if ref is None:
                    continue
                if len(adds) >= MAX_CALLS_PER_STEP:
                    skipped.append(book["isbn"])
                    continue
                adds.append(
                    await execute(ctx, gateway, ToolName.CART_ADD, {"book": ref})
                )
            if added_from is not None and _roll_back(adds):
                gateway.restore(added_from)
                adds = await _rolled_back(ctx, gateway, adds)
                # 되돌린 쓰기는 바꾼 것이 아니다. 남은 쓰기(앞 묶음과 이 묶음의 다른 호출)로 다시 정한다
                ctx.changed_cart = any(
                    c.name in guard.WRITE_TOOLS and c.result.get("ok") is True
                    for c in [*state["tool_calls"], *recorded, *adds]
                )
            recorded[position + 1 : position + 1] = adds
            at, call_id, call_name = slots[position]
            updated = call.model_copy(
                update={
                    "result": {
                        **call.result,
                        "added": [
                            {"isbn": a.arguments.get("isbn"), **_added(a.result)}
                            for a in adds
                        ]
                        + [
                            {
                                "isbn": isbn,
                                "ok": False,
                                "error": ToolErrorCode.TOO_MANY_CALLS.value,
                            }
                            for isbn in skipped
                        ],
                    }
                }
            )
            recorded[position] = updated
            replies[at] = _reply(ctx, updated, call_id, call_name)

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


async def prefetch_card_titles(ctx: TurnContext, gateway: CommerceGateway) -> None:
    """직전 카드 책의 제목을 코드가 먼저 읽어 [이번 요청의 상태]에 보여 준다.

    카드는 번호만 보여 주고 제목은 LLM 이 book.offer 로 읽게 했더니 흔한 흐름 두 가지가 막혔다(11/10 평가 v13).
    - 카드가 보일 때 "2번 빼 주세요": LLM 은 제목이 보이는 장바구니 목록만 보고 장바구니 2번째로 읽어서, 코드가 두
      책을 짚어 되물었다(remove-13, 3번 다)
    - 한 책을 물은 뒤 "하나 주세요": 대화에서 이야기한 책을 알아보려면 카드 제목이 다 있어야 해서(evidence.needs_card_titles)
      LLM 이 제목을 읽기 전에는 그 책을 알 수 없었다
    BE 가 카드 책의 제목·가격·재고를 요청(offers)에 실어 보내므로(A안) BE 를 다시 부르지 않는다. 가격과 재고는 보여
    주지 않는다. 답의 가격은 book.offer 결과로만 말한다(답변 검사의 근거). 읽지 못하면 지금까지처럼 번호만 보여 준다.
    """
    missing = list(dict.fromkeys(c.isbn for c in ctx.cards if c.isbn not in ctx.titles))
    if not missing:
        return
    try:
        offers = await gateway.get_offers(missing)
    except CommerceError:
        return
    ctx.titles.update({offer.isbn: offer.title for offer in offers})


async def run_agent(
    req: AgentActRequest,
    *,
    model: Any,
    gateway: CommerceGateway,
    comparer: Comparer | None = None,
    finder: BookFinder | None = None,
    checkout: bool = False,
    turn_seconds: float | None = None,
    info: BookInfo | None = None,
    book_info: bool = False,
    summarizer: ReviewSummarizer | None = None,
) -> AgentRun:
    """요청 하나를 그래프로 처리한다. LLM 장애는 LLMUnavailableError 로 올라간다.

    comparer 가 없으면 book.compare 는 쓸 수 없다. 소개글을 읽는 호출은 도구를 고르는 model 과 따로
    한다(소개글 속 지시문이 도구 선택에 섞이지 않게).

    checkout 이 켜져 있으면(#631) 골라서 담으라고 맡긴 말에 확인 없이 담고, "주문해 줘"로 담았으면 주문 화면에
    채울 쿠폰과 포인트를 고른다. finder 가 있으면 카드 밖의 책을 찾는 book.search 를 준다.

    info 는 BE 공개 상품 정보다(#650). book_info 가 켜져 있으면 책 정보·리뷰 도구를 주고(리뷰 요약은 summarizer 가
    한다), checkout 이 켜져 있으면 "주문해 줘"로 담은 카드·장바구니 책의 쿠폰 대상을 여기서 읽는다.

    turn_seconds 는 부르는 쪽이 이 턴에 거는 시간 제한이다. 남은 시간이 ANSWER_MARGIN_SECONDS(짧은 제한이면 그
    1/3)보다 적으면 도구를 그만 쓰고 답하게 한다. None 이면 반복 상한만 본다.
    """
    deadline = _deadline(turn_seconds)
    ctx = TurnContext.from_request(req)
    ctx.checkout_enabled = checkout
    if not checkout:
        finder = None  # 기능이 꺼져 있으면 찾을 곳이 있어도 책 찾기를 주지 않는다
    offered_info = info if book_info else None
    ctx.book_info_enabled = offered_info is not None
    await prefetch_cart(ctx, gateway)
    await prefetch_card_titles(ctx, gateway)
    ctx.topic = evidence.topic_name(ctx)
    start = list(ctx.cart_snapshot or [])
    graph = build_graph(
        model, gateway, ctx, comparer, finder, deadline, offered_info, summarizer
    )
    out: _State = await graph.ainvoke(
        {
            "messages": [
                SystemMessage(
                    prompts.system_prompt(
                        search=finder is not None,
                        order=checkout,
                        info=offered_info is not None,
                    )
                ),
                SystemMessage(prompts.state_message(ctx)),
                HumanMessage(req.message),
            ],
            "tool_calls": [],
            "timings": [],
            "iterations": 0,
            "llm_calls": 0,
            "out_of_time": False,
        }
    )
    final_text = _text(out["messages"][-1])
    actions: list[CartAction] = []
    plan: checkout_plan.Plan | None = None
    if any(
        call.name in guard.WRITE_TOOLS and call.result.get("ok") is True
        for call in out["tool_calls"]
    ):
        end = await gateway.get_cart(ctx.user_id)
        actions = diff_cart(start, end)
        if checkout and ctx.logged_in and evidence.wants_order(ctx):
            coupon_ids = await _coupon_targets(ctx, actions, info, deadline)
            plan = _plan(ctx, actions, end, coupon_ids)
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
        actions=actions,
        checkout=plan,
        out_of_time=out["out_of_time"],
    )


# 주문 턴에 카드·장바구니 책의 쿠폰 대상을 읽는 데 쓰는 최대 시간(초). 넘으면 그 책은 대상을 모르는 것으로 본다
COUPON_TARGET_SECONDS = 2.0


async def _coupon_targets(
    ctx: TurnContext,
    actions: list[CartAction],
    info: BookInfo | None,
    deadline: float | None,
) -> dict[str, frozenset[int]]:
    """이번 턴에 담은 카드·장바구니 책에 걸린 쿠폰 번호(ISBN → 쿠폰 번호, #650).

    찾은 책은 검색 때 이미 읽었다. 그 밖의 책은 요청의 상품 번호(BE#536)로 BE 공개 상품 상세를 읽는다. 쓸 쿠폰이
    없거나 읽을 곳이 없거나 시간이 모자라면 읽지 않는다(그 책은 대상을 모르는 것으로 보고, 사용자가 주문 화면에서
    고른다).
    """
    targets = {isbn: book.coupon_ids for isbn, book in ctx.found.items()}
    if info is None or not ctx.owned_coupons:
        return targets
    wanted = {
        action.isbn: pid
        for action in actions
        if action.op == "set"
        and action.isbn not in targets
        and (pid := ctx.product_ids.get(action.isbn)) is not None
    }
    if not wanted:
        return targets
    budget = COUPON_TARGET_SECONDS
    if deadline is not None:
        budget = min(budget, max(0.0, deadline - perf_counter()) + 1.0)
    try:
        async with asyncio.timeout(budget):
            details = await info.details(list(wanted.values()))
    except (TimeoutError, InfoUnavailable):
        return targets
    for isbn, pid in wanted.items():
        if pid in details:
            targets[isbn] = details[pid].coupon_ids
    return targets


def _plan(
    ctx: TurnContext,
    actions: list[CartAction],
    end: list[CartLine],
    coupon_targets: dict[str, frozenset[int]] | None = None,
) -> checkout_plan.Plan | None:
    """이번 턴에 담은 책(set)으로 주문 화면에 채울 쿠폰과 포인트를 고른다. 담은 책이 없으면 None.

    가격은 끝 장바구니의 값이다(찾은 책은 BE 상품 상세, 카드 책은 요청에 실린 BE 값). 쿠폰 대상은 coupon_targets
    (찾은 책과, 상품 번호로 읽은 카드·장바구니 책)에 있는 책만 안다.
    """
    if coupon_targets is None:
        coupon_targets = {isbn: book.coupon_ids for isbn, book in ctx.found.items()}
    prices = {line.isbn: line.price for line in end}
    lines = []
    for action in actions:
        price = prices.get(action.isbn)
        if action.op != "set" or action.quantity is None or price is None:
            continue
        coupon_ids = coupon_targets.get(action.isbn, frozenset())
        lines.append(
            checkout_plan.OrderLine(action.isbn, price, action.quantity, coupon_ids)
        )
    if not lines:
        return None
    return checkout_plan.plan(
        lines,
        ctx.owned_coupons,
        ctx.point_balance,
        use_coupon=not evidence.declines_discount(ctx, "쿠폰"),
        use_points=not evidence.declines_discount(ctx, "포인트"),
    )
