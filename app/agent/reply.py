"""⑦ 마무리: LLM 의 답을 이번 턴의 도구 기록과 대조해 검사하고 /agent/act 응답으로 만든다(#358).

6단계에서 실제 모델로 재 봤더니 LLM 이 (1) 도구를 한 번도 안 부르고 "담았어요"라고 답했고 (2) 합계를 지어냈다
(15,000원은 가격 조회 결과에 있어서 "도구 결과에 있는 숫자인가" 검사로는 못 잡는다). 프롬프트에 "ok 일 때만
말하라"고 써 뒀는데도 일어났으므로, LLM 이 한 말을 코드가 기록과 대조한다(명세 ⑦ grounding 규칙).

- 행동 검사: "담았어요"류 완료 표현은 같은 종류의 쓰기 도구가 이번 턴에 성공했을 때만 남긴다
- 숫자 검사: "합계" 뒤의 금액은 마지막 cart.view 의 total 과 같아야 하고, 그 뒤에 쓰기가 성공했으면 인정하지
  않는다. 그 밖의 금액은 도구 결과에 있는 값(가격, 가격×수량, 합계)이어야 한다
- 위반한 문장만 지운다. 지운 뒤 남는 답이 없으면 GroundingFailed(422)다. 한 문장 안에 맞는 말과 틀린 말이
  섞여 있으면 문장 전체를 지운다(어디가 틀렸는지 문장을 쪼개 판단하지 않는다)
"""

import re
from dataclasses import dataclass
from itertools import combinations

from app.agent import guard
from app.agent.graph import AgentRun
from app.agent.prompts import NOT_SHOPPING
from app.agent.schemas import (
    AgentActData,
    Button,
    ResolvedReference,
    ToolCall,
    ToolName,
)
from app.agent.tools import UI_TARGETS
from app.agent.view import label

# 반복을 다 쓰거나 LLM 이 빈 글로 끝났을 때, 지운 문장도 없을 때 쓰는 안내
FALLBACK_REPLY = "요청을 끝까지 처리하지 못했어요. 다시 한 번 말씀해 주세요."
CART_SCREEN = "CART-001"

# 완료 표현. 같은 종류의 쓰기 도구가 성공했을 때만 쓸 수 있다.
# "담지 못했어요", "이미 담겨 있어요", "담을까요?"는 완료 표현이 아니라서 걸리지 않는다.
_ACTION_CLAIMS: dict[ToolName, re.Pattern[str]] = {
    ToolName.CART_ADD: re.compile(
        r"(담았|담아\s*(드렸|두었|놓았)|담아드렸|추가했|추가해\s*드렸|넣었|넣어\s*(드렸|두었))"
    ),
    ToolName.CART_REMOVE: re.compile(
        r"(뺐|빼\s*(드렸|놓았)|빼드렸|삭제했|삭제해\s*드렸|제거했|제거해\s*드렸|지웠)"
    ),
    ToolName.CART_UPDATE: re.compile(
        r"(바꿨|바꿔\s*(드렸|놓았)|바꿔드렸|변경했|변경해\s*드렸|수정했|줄였|늘렸|조정했)"
    ),
}

# "못 담았어요", "안 담았어요"는 하지 못했다는 말이다. "담았"에 걸려 완료 표현으로 오인되지 않게 먼저 뺀다.
_NEGATED_ACTION = re.compile(
    r"(?<![가-힣])(못|안)\s*(담|넣|추가|빼|뺐|삭제|제거|지웠|바꿨|바꿔|변경|수정|줄였|늘렸|조정)[가-힣]*"
)
_MONEY = re.compile(r"(\d[\d,]*)\s*원")
# "합계는 52,000원"처럼 합계 말 바로 뒤의 금액. 말과 금액 사이에 숫자가 끼면(총 2권, 52,000원) 합계 주장이
# 아니다. 그 금액은 아래의 일반 금액 검사를 받는다.
_TOTAL_CLAIM = re.compile(
    r"(합계|총액|총합|총\s*금액|전체\s*금액|총)[^\d\n]{0,12}?(\d[\d,]*)\s*원"
)
_DIFFERENCE_WORDS = re.compile(r"차이|더\s*(비싸|싸|저렴)|비싸|저렴|아낄|절약")
_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")


class GroundingFailed(Exception):
    """검사로 문장을 지웠더니 사용자에게 보여 줄 답이 남지 않았다(422 grounding_failed)."""

    def __init__(self, removed: list[str]) -> None:
        super().__init__("grounding_failed")
        self.removed = removed


@dataclass
class Finished:
    data: AgentActData
    removed: list[str]  # 지운 문장의 이유 코드. 검사가 몇 건 걸러 냈는지 재는 데 쓴다


@dataclass
class _Facts:
    succeeded: set[ToolName]  # 이번 턴에 성공한 쓰기 도구
    allowed_money: set[int]
    prices: list[int]
    total: int | None  # 인정하는 합계. 마지막 조회 뒤에 쓰기가 성공했으면 None
    view_index: int | None  # 마지막 cart.view 기록의 위치


def _money(text: str) -> int:
    return int(text.replace(",", ""))


def _facts(calls: list[ToolCall]) -> _Facts:
    succeeded: set[ToolName] = set()
    allowed: set[int] = set()
    prices: list[int] = []
    view_index: int | None = None
    for index, call in enumerate(calls):
        if call.name in guard.WRITE_TOOLS and call.result.get("ok") is True:
            succeeded.add(call.name)
        if call.name == ToolName.CART_VIEW and "items" in call.result:
            view_index = index
            allowed.add(call.result["total"])
            for item in call.result["items"]:
                if item["price"] is not None:
                    allowed.update({item["price"], item["price"] * item["quantity"]})
                    prices.append(item["price"])
        if call.name == ToolName.BOOK_OFFER and "offers" in call.result:
            for offer in call.result["offers"]:
                allowed.add(offer["price"])
                prices.append(offer["price"])

    total: int | None = None
    if view_index is not None:
        wrote_after = any(
            c.name in guard.WRITE_TOOLS and c.result.get("ok") is True
            for c in calls[view_index + 1 :]
        )
        # 조회한 뒤에 장바구니가 바뀌었다면 그 합계는 이미 낡았다. 다시 조회해야 한다
        total = None if wrote_after else calls[view_index].result["total"]
    return _Facts(succeeded, allowed, prices, total, view_index)


def _check(sentence: str, facts: _Facts) -> tuple[str, int | None] | None:
    """문장이 기록과 어긋나면 (이유, grounded 를 false 로 할 기록 위치), 괜찮으면 None."""
    if sentence.rstrip().endswith("?"):
        return None  # 사용자에게 묻는 문장은 주장이 아니다

    claimable = _NEGATED_ACTION.sub("", sentence)
    for tool, pattern in _ACTION_CLAIMS.items():
        if pattern.search(claimable) and tool not in facts.succeeded:
            return "ACTION_NOT_DONE", None

    claimed_totals = list(_TOTAL_CLAIM.finditer(sentence))
    for match in claimed_totals:
        if facts.total is None or _money(match.group(2)) != facts.total:
            return "TOTAL_MISMATCH", facts.view_index

    rest = sentence
    for match in reversed(claimed_totals):
        rest = rest[: match.start()] + rest[match.end() :]
    differences = (
        {abs(a - b) for a, b in combinations(facts.prices, 2)}
        if _DIFFERENCE_WORDS.search(sentence)
        else set()
    )
    for match in _MONEY.finditer(rest):
        amount = _money(match.group(1))
        if amount not in facts.allowed_money and amount not in differences:
            return "NUMBER_NOT_FROM_TOOLS", None
    return None


def _units(text: str) -> list[list[str]]:
    """줄마다 문장으로 나눈다. 줄 구조(목록 등)를 지키려고 줄을 먼저 나눈다."""
    return [
        [s for s in _SENTENCE_END.split(line.strip()) if s]
        for line in text.split("\n")
        if line.strip()
    ]


def _plain(text: str) -> str:
    """마크다운 강조를 벗긴다. 앱 화면은 마크다운을 그리지 않는다."""
    return text.replace("**", "").replace("__", "").replace("`", "")


def _resolved(run: AgentRun) -> ResolvedReference | None:
    """실행한 도구가 풀어 낸 책. 코드가 채운 ISBN 만 쓴다."""
    isbns: list[str] = []
    for call in run.tool_calls:
        if "isbn" in call.arguments:
            found = [call.arguments["isbn"]]
        elif "isbns" in call.arguments:
            found = list(call.arguments["isbns"])
        else:
            continue
        for isbn in found:
            # 책을 풀기 전에 막힌 호출은 LLM 이 준 인자가 그대로 남아 있다. 요청 안에서 온 책만 인정한다
            if (
                isinstance(isbn, str)
                and label(run.ctx, isbn) != "목록 밖의 책"
                and isbn not in isbns
            ):
                isbns.append(isbn)
    if not isbns:
        return None
    return ResolvedReference(
        ref=", ".join(label(run.ctx, i) for i in isbns), isbns=isbns
    )


_CART_TOOLS = {
    ToolName.CART_VIEW,
    ToolName.CART_ADD,
    ToolName.CART_UPDATE,
    ToolName.CART_REMOVE,
}


def _touched_cart(call: ToolCall) -> bool:
    if call.name not in _CART_TOOLS:
        return False
    return (
        call.result.get("ok") is True
        or "items" in call.result
        or call.result.get("error") == "ALREADY_IN_CART"
    )


def _buttons(calls: list[ToolCall]) -> list[Button]:
    buttons: list[Button] = []
    seen: set[str] = set()

    def navigate(text: str, target: str) -> None:
        if target not in seen:
            seen.add(target)
            buttons.append(Button(label=text, action="navigate", target=target))

    for call in calls:
        if call.name == ToolName.UI_GUIDE and call.result.get("ok") is True:
            target = call.arguments["target"]
            navigate(UI_TARGETS[target], target)
    if any(_touched_cart(call) for call in calls):
        navigate(UI_TARGETS[CART_SCREEN], CART_SCREEN)
    return buttons


def finish(run: AgentRun) -> Finished:
    """그래프 결과를 검사해 응답으로 만든다. 남는 답이 없으면 GroundingFailed."""
    if run.not_shopping:
        return Finished(AgentActData.not_shopping_reply(), [])

    text = _plain(run.final_text)
    if text.strip() == NOT_SHOPPING:
        text = ""  # 도구를 부른 뒤에 나온 약속어는 사용자에게 보이지 않는다

    facts = _facts(run.tool_calls)
    removed: list[str] = []
    flagged: set[int] = set()
    kept_lines: list[str] = []
    for line in _units(text):
        kept: list[str] = []
        for sentence in line:
            problem = _check(sentence, facts)
            if problem is None:
                kept.append(sentence)
                continue
            reason, flag_at = problem
            removed.append(reason)
            if flag_at is not None:
                flagged.add(flag_at)
            if reason == "ACTION_NOT_DONE":
                # 실패한 쓰기 호출이 있었다면 그 결과가 이 말을 뒷받침하지 못한 것이다
                flagged.update(
                    i
                    for i, c in enumerate(run.tool_calls)
                    if c.name in guard.WRITE_TOOLS and c.result.get("ok") is False
                )
        if kept:
            kept_lines.append(" ".join(kept))

    reply = "\n".join(kept_lines)
    if not reply:
        if removed:
            raise GroundingFailed(removed)
        reply = FALLBACK_REPLY

    calls = [
        c.model_copy(update={"grounded": False}) if i in flagged else c
        for i, c in enumerate(run.tool_calls)
    ]
    data = AgentActData(
        reply=reply,
        not_shopping=False,
        tool_calls=calls,
        resolved_reference=_resolved(run),
        selection=None,
        buttons=_buttons(calls),
    )
    return Finished(data, removed)
