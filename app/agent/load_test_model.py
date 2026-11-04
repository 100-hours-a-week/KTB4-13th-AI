"""쇼핑 부하 테스트에서 정해진 시나리오만 재현하는 모델."""

import asyncio
import re

from app.agent.fake_llm import ScriptedModel, ai_calls, ai_text
from app.agent.schemas import AgentActRequest, ToolName


class _DelayedBound:
    def __init__(self, bound, delay: float):
        self.bound = bound
        self.delay = delay

    async def ainvoke(self, *args, **kwargs):
        await asyncio.sleep(self.delay)
        return await self.bound.ainvoke(*args, **kwargs)


class LoadTestModel:
    def __init__(self, req: AgentActRequest, delay: float):
        number = re.search(r"(\d+)\s*번", req.message)
        rank = int(number.group(1)) if number else 1
        if "담" in req.message and req.context_cards:
            replies = [
                ai_calls((ToolName.CART_ADD, {"book": {"source": "card", "n": rank}})),
                ai_text("확인해 주세요."),
            ]
        elif "장바구니" in req.message:
            replies = [ai_calls((ToolName.CART_VIEW, {})), ai_text("확인해 주세요.")]
        else:
            # 지원 시나리오 밖의 발화도 provider 호출 없이 정상적인 되묻기로 끝낸다.
            # 이 경우는 도구 처리 성능 표본으로 집계하지 않아야 한다.
            replies = [ai_text("어떤 책을 원하시나요?")]
        self.model = ScriptedModel(replies)
        self.delay = max(0, delay)

    def bind_tools(self, tools, **kwargs):
        return _DelayedBound(self.model.bind_tools(tools, **kwargs), self.delay)

    async def ainvoke(self, *args, **kwargs):
        await asyncio.sleep(self.delay)
        return await self.model.ainvoke(*args, **kwargs)
