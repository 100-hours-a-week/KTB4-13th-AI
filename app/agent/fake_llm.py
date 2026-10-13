"""도구 바인딩이 되는 테스트용 가짜 LLM(#356).

llm_mock 이 쓰는 FakeListChatModel 은 bind_tools 가 안 돼서 그래프를 시험할 수 없다. 이 가짜는 미리 정한 답을
순서대로 돌려주고, 받은 메시지와 바인딩 인자를 남겨서 "LLM 이 무엇을 받았는지"를 테스트가 볼 수 있게 한다.
평가 실행기(10단계)가 LLM 없이 시험할 때도 쓴다.
"""

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agent.descriptors import llm_name
from app.agent.schemas import ToolName


def ai_text(text: str) -> AIMessage:
    """도구 없이 글로만 답하는 LLM 의 답."""
    return AIMessage(content=text)


def ai_calls(*calls: tuple[ToolName | str, dict[str, Any]]) -> AIMessage:
    """도구를 부르는 LLM 의 답. 호출은 (도구, 인자)이고 도구 이름은 LLM 이 쓰는 이름으로 바꿔 담는다.

    ToolName 이 아닌 문자열을 주면 그대로 쓴다(없는 도구를 부르는 경우를 시험할 때).
    """
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": llm_name(name) if isinstance(name, ToolName) else name,
                "args": args,
                "id": f"call_{index}",
            }
            for index, (name, args) in enumerate(calls)
        ],
    )


class _Bound:
    def __init__(self, model: "ScriptedModel", kwargs: dict[str, Any]) -> None:
        self._model = model
        self._kwargs = kwargs

    async def ainvoke(self, messages: Sequence[BaseMessage], *_: Any, **__: Any):
        return self._model._next(messages, self._kwargs)


class ScriptedModel:
    """replies 를 앞에서부터 하나씩 돌려준다. 예외를 넣으면 그 차례에 던진다."""

    def __init__(self, replies: Sequence[AIMessage | BaseException]) -> None:
        self._replies = list(replies)
        # 호출마다 받은 메시지 전체
        self.calls: list[list[BaseMessage]] = []
        # 호출마다 쓴 바인딩 인자(tool_choice 등)
        self.bind_kwargs: list[dict[str, Any]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []

    def bind_tools(self, tools: Sequence[dict[str, Any]], **kwargs: Any) -> _Bound:
        self.tools_seen.append(list(tools))
        return _Bound(self, kwargs)

    async def ainvoke(self, messages: Sequence[BaseMessage], *_: Any, **__: Any):
        """도구 없이 부르는 경우(허용한 도구가 하나도 없을 때)."""
        return self._next(messages, {"tools": False})

    def _next(self, messages: Sequence[BaseMessage], kwargs: dict[str, Any]):
        self.calls.append(list(messages))
        self.bind_kwargs.append(kwargs)
        if not self._replies:
            raise AssertionError(
                "가짜 LLM 의 답이 모자란다. 그래프가 예상보다 더 불렀다"
            )
        reply = self._replies.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply
