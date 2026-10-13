"""⑦ 마무리: LLM 의 답을 이번 턴의 도구 기록과 대조해 검사하고 /agent/act 응답으로 만든다(#358).

6단계에서 실제 모델로 재 봤더니 LLM 이 (1) 도구를 한 번도 안 부르고 "담았어요"라고 답했고 (2) 합계를 지어냈다
(15,000원은 가격 조회 결과에 있어서 "도구 결과에 있는 숫자인가" 검사로는 못 잡는다). 프롬프트에 "ok 일 때만
말하라"고 써 뒀는데도 일어났으므로, LLM 이 한 말을 코드가 기록과 대조한다(명세 ⑦ grounding 규칙).

- 쓰기의 결과(담았다, 못 담았다)는 LLM 이 아니라 코드가 기록으로 문장을 만든다(_outcomes). 리뷰에서, 완료 표현을
  말투 목록으로 막으면 "추가되었습니다"처럼 목록에 없는 말이 새고, 일부만 성공해도 통과하고, 참말이 지워져
  422 가 나는 것을 확인했다. 그래서 LLM 이 쓴 완료 표현은 성공이든 실패든 지우고 코드 문장으로 바꾼다
- 숫자 검사: "합계" 뒤의 금액은 마지막 cart.view 의 total 과 같아야 하고, 그 뒤에 쓰기가 성공했으면 인정하지
  않는다. 그 밖의 금액은 도구 결과에 있는 값(가격, 가격×수량, 합계)이어야 한다. 책 제목 속의 글자나 숫자가
  걸리지 않도록 검사 전에 제목을 가린다
- 위반한 문장만 지운다. 코드가 만든 결과 문장이 있으면 답이 비지 않는다. 쓰기가 일어나지 않았는데 LLM 의 글이
  전부 지워지면 GroundingFailed(422)다. 한 문장 안에 맞는 말과 틀린 말이 섞여 있으면 문장 전체를 지운다
"""

import re
from dataclasses import dataclass
from itertools import combinations

from app.agent import guard
from app.agent.errors import ToolErrorCode
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
from app.gateway.commerce import CommerceErrorCode

# 반복을 다 쓰거나 LLM 이 빈 글로 끝났을 때, 지운 문장도 없을 때 쓰는 안내
FALLBACK_REPLY = "요청을 끝까지 처리하지 못했어요. 다시 한 번 말씀해 주세요."
CART_SCREEN = "CART-001"

# 완료 표현. 말투를 하나씩 막는 목록이라 빠진 말이 있을 수 있다. 그래서 이 목록은 "LLM 이 완료를 말했나"를
# 넓게 잡는 데만 쓰고, 사용자에게 가는 결과 문장은 이 목록과 상관없이 코드가 만든다(_outcomes).
# 하는 중이거나 상태를 말하는 "담겨 있어요", 약속하는 "담아 드릴게요", 묻는 "담을까요?"는 완료가 아니라서 걸리지 않는다.
_JOSA = r"(이|가|를|을|도)?"  # "추가가 완료되었어요", "삭제를 완료했어요"
_DID = (
    rf"{_JOSA}\s*(했|하였|됐|되었|되어|완료|해\s*(드렸|드립|뒀|두었|놨|놓았|줬|주었))"
)
_GAVE = r"(드렸|드립|뒀|두었|놨|놓았|줬|주었)"
_ACTION_CLAIMS: dict[ToolName, re.Pattern[str]] = {
    ToolName.CART_ADD: re.compile(
        rf"(담았|담겼|담기었|담아\s*{_GAVE}|담기{_DID}|추가{_DID}|넣었|넣어\s*{_GAVE}|등록{_DID})"
    ),
    ToolName.CART_REMOVE: re.compile(
        rf"(뺐|빼\s*{_GAVE}|빠졌|빠지었|삭제{_DID}|제거{_DID}|지웠|지워\s*({_GAVE}|졌))"
    ),
    ToolName.CART_UPDATE: re.compile(
        rf"(바꿨|바꾸었|바꿔\s*{_GAVE}|바뀌었|변경{_DID}|수정{_DID}|줄였|줄어들었|늘렸|늘어났|조정{_DID})"
    ),
}

# "못 담았어요", "안 담았어요"는 하지 못했다는 말이다. "담았"에 걸려 완료 표현으로 오인되지 않게 먼저 뺀다.
_NEGATED_ACTION = re.compile(
    r"(?<![가-힣])(못|안)\s*(담|넣|추가|빼|뺐|삭제|제거|지웠|바꿨|바꿔|변경|수정|줄였|늘렸|조정)[가-힣]*"
)
_MONEY = re.compile(r"(\d[\d,]*)\s*원")
# "합계는 52,000원"처럼 합계 말 바로 뒤의 금액. 말과 금액 사이에 숫자가 끼면(총 2권, 52,000원) 합계 주장이
# 아니다. 그 금액은 아래의 일반 금액 검사를 받는다. "총"은 숫자 바로 앞에서만 합계 말이다. 글자 하나라서
# "총, 균, 쇠"나 "권총"처럼 제목에 든 글자까지 합계로 읽으면 참말이 지워진다.
_TOTAL_CLAIM = re.compile(
    r"(합계|총액|총합|총\s*금액|전체\s*금액)[^\d\n]{0,12}?(\d[\d,]*)\s*원"
    r"|(총)\s*(\d[\d,]*)\s*원"
)
# 책 제목. 제목 속의 글자가 완료 표현이나 금액으로 읽히지 않게 검사 전에 가린다.
_BRACKETED_TITLE = re.compile(r"[《〈「『][^》〉」』\n]{1,100}[》〉」』]")
_MASK = "〔책〕"
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
    titles: dict[str, str]  # ISBN -> 이번 턴에 도구가 알려 준 책 제목


def _money(text: str) -> int:
    return int(text.replace(",", ""))


def _facts(run: AgentRun) -> _Facts:
    calls = run.tool_calls
    succeeded: set[ToolName] = set()
    allowed: set[int] = set()
    prices: list[int] = []
    view_index: int | None = None
    titles = {line.isbn: line.title for line in run.ctx.cart_snapshot or []}
    for index, call in enumerate(calls):
        if call.name in guard.WRITE_TOOLS and call.result.get("ok") is True:
            succeeded.add(call.name)
        if call.name == ToolName.CART_VIEW and "items" in call.result:
            view_index = index
            allowed.add(call.result["total"])
            for item in call.result["items"]:
                titles[item["isbn"]] = item["title"]
                if item["price"] is not None:
                    allowed.update({item["price"], item["price"] * item["quantity"]})
                    prices.append(item["price"])
        if call.name == ToolName.BOOK_OFFER and "offers" in call.result:
            for offer in call.result["offers"]:
                titles[offer["isbn"]] = offer["title"]
                allowed.add(offer["price"])
                prices.append(offer["price"])
        if call.result.get("error") == ToolErrorCode.ALREADY_IN_CART.value:
            isbn = call.arguments.get("isbn")
            if isinstance(isbn, str) and isinstance(call.result.get("title"), str):
                titles[isbn] = call.result["title"]

    total: int | None = None
    if view_index is not None:
        wrote_after = any(
            c.name in guard.WRITE_TOOLS and c.result.get("ok") is True
            for c in calls[view_index + 1 :]
        )
        # 조회한 뒤에 장바구니가 바뀌었다면 그 합계는 이미 낡았다. 다시 조회해야 한다
        total = None if wrote_after else calls[view_index].result["total"]
    return _Facts(succeeded, allowed, prices, total, view_index, titles)


def _mask(sentence: str, titles: dict[str, str]) -> str:
    """검사할 때만 책 제목을 가린다. "《총, 균, 쇠》는 22,000원"의 "총"이 합계 말로, 제목 속 금액이 가격
    주장으로 읽히지 않게 하려는 것이다. 사용자에게 가는 글은 가리지 않는다."""
    masked = _BRACKETED_TITLE.sub(_MASK, sentence)
    for title in sorted(
        {t for t in titles.values() if len(t) >= 2}, key=len, reverse=True
    ):
        masked = masked.replace(title, _MASK)
    return masked


def _check(sentence: str, facts: _Facts) -> tuple[str, int | None] | None:
    """문장이 기록과 어긋나면 (이유, grounded 를 false 로 할 기록 위치), 괜찮으면 None."""
    if sentence.rstrip().endswith("?"):
        return None  # 사용자에게 묻는 문장은 주장이 아니다

    masked = _mask(sentence, facts.titles)
    claimable = _NEGATED_ACTION.sub("", masked)
    claimed = [tool for tool, p in _ACTION_CLAIMS.items() if p.search(claimable)]
    if claimed:
        # 한 문장이 여러 일을 말하면 하나라도 안 한 일이 있을 때 거짓이다. 한 일이면 코드가 결과 문장으로
        # 따로 말하므로 LLM 의 말은 지운다
        if any(tool not in facts.succeeded for tool in claimed):
            return "ACTION_NOT_DONE", None
        return "ACTION_RESTATED", None

    claimed_totals = list(_TOTAL_CLAIM.finditer(masked))
    for match in claimed_totals:
        amount = _money(match.group(2) or match.group(4))
        if facts.total is None or amount != facts.total:
            return "TOTAL_MISMATCH", facts.view_index

    rest = masked
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


# --- 쓰기의 결과 문장: LLM 이 아니라 코드가 기록으로 만든다 ---

_DIGITS_WITH_BATCHIM = frozenset("013678")  # 영 일 삼 육 칠 팔


def _has_batchim(word: str) -> bool | None:
    """마지막 글자에 받침이 있나. 한글과 숫자만 안다. 모르면 None."""
    last = word.rstrip()[-1:]
    if "가" <= last <= "힣":
        return (ord(last) - ord("가")) % 28 != 0
    if last.isdigit():
        return last in _DIGITS_WITH_BATCHIM
    return None


def _particle(word: str, with_batchim: str, without_batchim: str) -> str:
    batchim = _has_batchim(word)
    if batchim is None:
        return f"{with_batchim}({without_batchim})"
    return with_batchim if batchim else without_batchim


# BE 가 거절한 이유(CommerceErrorCode)와 이미 담긴 책을 담지 않은 것. 이 코드들이 결과에 있으면 BE 까지
# 갔던 호출이다. 그 앞에서 막힌 것(책을 못 가리킴, 인자 오류 등)은 LLM 이 되묻거나 설명한다.
_REACHED_BE = frozenset(
    {code.value for code in CommerceErrorCode} | {ToolErrorCode.ALREADY_IN_CART.value}
)
LOGIN_REPLY = "로그인이 필요해요. 로그인한 뒤에 다시 말씀해 주세요."


def _book(run: AgentRun, titles: dict[str, str], isbn: str) -> tuple[str, str]:
    """(사용자에게 보일 이름, 조사를 고를 때 볼 말). 제목을 모르면 사용자가 말한 번호로 부른다."""
    if title := titles.get(isbn):
        return f"《{title}》", title
    for card in run.ctx.cards:
        if card.isbn == isbn:
            return f"{card.rank}번 책", "책"
    if run.ctx.focused_isbn == isbn:
        return "지금 보시는 책", "책"
    return "선택하신 책", "책"


def _outcome(name: ToolName, shown: str, plain: str, call: ToolCall) -> str:
    """쓰기 호출 하나의 결과를 사용자에게 말한다. 수량은 코드가 채운 인자에서 읽는다."""
    eul = _particle(plain, "을", "를")
    eun = _particle(plain, "은", "는")
    result = call.result
    error = result.get("error")
    quantity = call.arguments.get("quantity")
    count = quantity if isinstance(quantity, int) else None

    if name == ToolName.CART_ADD:
        if result.get("ok") is True:
            if count is not None and count > 1:
                return f"{shown} {count}권을 장바구니에 담았어요."
            return f"{shown}{eul} 장바구니에 담았어요."
        if error == ToolErrorCode.ALREADY_IN_CART.value:
            have = result.get("quantity")
            has = f" {have}권" if isinstance(have, int) else ""
            return f"{shown}{eun} 이미 장바구니에{has} 담겨 있어서 그대로 뒀어요."
        if error == CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK.value:
            return f"{shown}{eun} 재고가 부족해서 담지 못했어요."
        if error == CommerceErrorCode.PRODUCT_NOT_FOUND.value:
            return f"{shown}{eun} 지금 살 수 없는 책이라 담지 못했어요."
        if error == CommerceErrorCode.CART_ITEM_LIMIT_EXCEEDED.value:
            return f"장바구니가 가득 차서 {shown}{eul} 담지 못했어요."
        if error == CommerceErrorCode.INVALID_CART_ITEM_QUANTITY.value:
            return f"수량이 올바르지 않아 {shown}{eul} 담지 못했어요."
        return f"{shown}{eul} 담지 못했어요."

    if name == ToolName.CART_UPDATE:
        if result.get("ok") is True and count is not None:
            return f"{shown}의 수량을 {count}권으로 바꿨어요."
        if error == CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK.value:
            return f"재고가 부족해서 {shown}의 수량을 바꾸지 못했어요."
        if error == CommerceErrorCode.INVALID_CART_ITEM_QUANTITY.value:
            return f"수량이 올바르지 않아 {shown}의 수량을 바꾸지 못했어요."
        if error == CommerceErrorCode.CART_ITEM_NOT_FOUND.value:
            return f"{shown}{eun} 장바구니에 없어서 수량을 바꾸지 못했어요."
        return f"{shown}의 수량을 바꾸지 못했어요."

    # CART_REMOVE
    if result.get("ok") is True:
        return f"{shown}{eul} 장바구니에서 뺐어요."
    if error == CommerceErrorCode.CART_ITEM_NOT_FOUND.value:
        return f"{shown}{eun} 장바구니에 없어요."
    return f"{shown}{eul} 빼지 못했어요."


def _outcomes(run: AgentRun, titles: dict[str, str]) -> list[str]:
    """이번 턴에 BE 까지 간 쓰기와 로그인 때문에 막힌 쓰기의 결과 문장. 같은 책에 같은 일을 여러 번 했으면
    마지막 시도만 말한다(재고 부족 뒤에 수량을 줄여 다시 담은 경우 등)."""
    attempts: dict[tuple[ToolName, str], ToolCall] = {}
    login_needed = False
    for call in run.tool_calls:
        if call.name not in guard.WRITE_TOOLS:
            continue
        if call.result.get("error") == ToolErrorCode.LOGIN_REQUIRED.value:
            login_needed = True
            continue
        isbn = call.arguments.get("isbn")
        reached = (
            call.result.get("ok") is True or call.result.get("error") in _REACHED_BE
        )
        # 책을 풀기 전에 막힌 호출의 isbn 은 LLM 이 준 값이라 쓰지 않는다. BE 까지 간 호출만 코드가 채운 값이다
        if not reached or not isinstance(isbn, str):
            continue
        attempts.pop(
            (call.name, isbn), None
        )  # 말하는 순서는 마지막으로 시도한 때를 따른다
        attempts[(call.name, isbn)] = call

    sentences = [
        _outcome(name, *_book(run, titles, isbn), call)
        for (name, isbn), call in attempts.items()
    ]
    if login_needed:
        sentences.append(LOGIN_REPLY)
    return sentences


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

    facts = _facts(run)
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

    # 쓰기의 결과는 코드가 먼저 말한다. LLM 의 글이 전부 지워지거나 비어도, 이미 바뀐 장바구니를 두고
    # 422 나 "처리하지 못했어요"가 나가지 않는다
    outcome_lines = _outcomes(run, facts.titles)
    reply = "\n".join([" ".join(outcome_lines)] * bool(outcome_lines) + kept_lines)
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
