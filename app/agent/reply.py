"""⑦ 마무리: LLM 의 답을 이번 턴의 도구 기록과 대조해 검사하고 /agent/act 응답으로 만든다(#358, #363).

6단계에서 실제 모델로 재 봤더니 LLM 이 (1) 도구를 한 번도 안 부르고 "담았어요"라고 답했고 (2) 합계를 지어냈다
(15,000원은 가격 조회 결과에 있어서 "도구 결과에 있는 숫자인가" 검사로는 못 잡는다). 프롬프트에 "ok 일 때만
말하라"고 써 뒀는데도 일어났으므로, LLM 이 한 말을 코드가 기록과 대조한다(명세 ⑦ grounding 규칙).

- 쓰기의 결과(담았다, 못 담았다)는 LLM 이 아니라 코드가 기록으로 문장을 만든다(_outcomes). 리뷰에서, 완료 표현을
  말투 목록으로 막으면 "추가되었습니다"처럼 목록에 없는 말이 새고, 일부만 성공해도 통과하고, 참말이 지워져
  422 가 나는 것을 확인했다. 그래서 LLM 이 쓴 완료 표현은 성공이든 실패든 지우고 코드 문장으로 바꾼다
- 숫자 검사: "합계" 뒤의 금액은 마지막 cart.view 의 total 이나 고른 조합의 total_price 와 같아야 하고, 그
  조회 뒤에 쓰기가 성공했으면 장바구니 합계는 인정하지 않는다. 그 밖의 금액은 도구 결과에 있는 값(가격,
  가격×수량, 합계)이어야 한다. 책 제목 속의 글자나 숫자가 걸리지 않도록 검사 전에 제목을 가린다
- 위반한 문장만 지운다. 코드가 만든 결과 문장이 있으면 답이 비지 않는다. 쓰기가 일어나지 않았는데 LLM 의 글이
  전부 지워지면 GroundingFailed(422)다. 한 문장 안에 맞는 말과 틀린 말이 섞여 있으면 문장 전체를 지운다
"""

import re
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from app.agent import evidence, guard
from app.agent.checkout import Plan
from app.agent.errors import ToolErrorCode
from app.agent.graph import AgentRun
from app.agent.prompts import NOT_SHOPPING
from app.agent.reference import TurnContext
from app.agent.schemas import (
    Action,
    AgentActData,
    Button,
    Checkout,
    ResolvedReference,
    Selection,
    SelectionConstraints,
    SelectionItem,
    ToolCall,
    ToolName,
)
from app.agent.tools import UI_TARGETS
from app.agent.view import label
from app.gateway.commerce import CartLine, CommerceErrorCode

# 반복을 다 쓰거나 LLM 이 빈 글로 끝났을 때, 지운 문장도 없을 때 쓰는 안내
FALLBACK_REPLY = "요청을 끝까지 처리하지 못했어요. 다시 한 번 말씀해 주세요."
CART_SCREEN = "CART-001"
# 주문 화면(#631, BE#533). "주문해 줘"로 책을 담은 턴에 장바구니 버튼 대신 붙인다. FE 는 이번에 담은 책만 이 화면에
# 넣고 checkout 의 쿠폰·포인트를 채운다. 이름은 BE·FE 와 정하면 바꾼다
ORDER_SCREEN = "ORDER-001"
ORDER_LABEL = "주문 화면으로"
COUPON_LATER = "쿠폰과 포인트는 주문 화면에서 적용돼요."
# 주문해 달라는데 이번 턴에 주문 화면으로 보낼 책을 못 정했을 때(모델이 order.open 을 부르지 않았다). 쇼핑이 아니라고
# ③으로 넘기면 엉뚱한 추천이 나오고("주문" → 추천 카드), 그대로 두면 "이미 담겨 있어서 그대로 뒀어요"로만 끝나 주문은
# 어떻게 하는지 모른다(11/10 dev). 책을 짚어 다시 말하게 하고 장바구니로도 보낸다. 이미 장바구니에 있는 책도 order.open 이
# 같은 수량으로 주문 화면에 올리므로(#664) "장바구니 책은 못 보낸다"고 말하지 않는다
ORDER_NO_BOOK = "주문 화면으로 보낼 책을 정하지 못했어요. 주문할 책을 번호나 제목으로 말씀해 주시거나, 장바구니에서 바로 주문해 주세요."

# 완료 표현. 말투를 하나씩 막는 목록이라 빠진 말이 있을 수 있다. 그래서 이 목록은 "LLM 이 완료를 말했나"를
# 넓게 잡는 데만 쓰고, 사용자에게 가는 결과 문장은 이 목록과 상관없이 코드가 만든다(_outcomes).
# 하는 중이거나 상태를 말하는 "담겨 있어요", 약속하는 "담아 드릴게요", 묻는 "담을까요?"는 완료가 아니라서 걸리지 않는다.
_JOSA = r"(이|가|를|을|도)?"  # "추가가 완료되었어요", "삭제를 완료했어요"
_DID = rf"{_JOSA}\s*(했|하였|됐|되었|되어|완료|해\s*(드렸|뒀|두었|놨|놓았|줬|주었))"
# "드렸"(과거)만 완료다. "담아 드립니다"는 "버튼을 누르시면 담아 드립니다"처럼 앞으로 해 주겠다는 안내라서 뺀다.
# 실제 모델이 조합 고르기 답에서 이렇게 말했고, 그 안내가 지워져 사용자가 버튼을 못 찾을 뻔했다(#381 측정)
_GAVE = r"(드렸|뒀|두었|놨|놓았|줬|주었)"
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

# 주문했다·결제했다는 말. ⑦ 은 주문서를 만들거나 결제하지 않으므로 늘 거짓이다(#631)
# "주문 화면에서 주문을 마무리해 주세요", "결제를 완료하시면 돼요"처럼 사용자에게 하라는 부탁·안내는 이미 했다는
# 말이 아니다. "완료"·"마무리" 바로 뒤에 부탁·안내 꼴이 오면 걸리지 않는다(#637 리뷰). "마무리했어요"는 걸린다
_ORDER_ASK = (
    r"(?:해\s*(?:주세요|주시|주실|보세요|보시|줘|야)|하(?:시|세|려|면|는|길|라)|할)"
)
_ORDER_CLAIM = re.compile(
    rf"(주문|결제|구매|구입){_JOSA}\s*"
    rf"(?:했|하였|됐|되었|되어|마쳤|끝냈|끝났|해\s*{_GAVE}|(?:완료|마무리)(?!{_ORDER_ASK}))"
)

# "못 담았어요", "안 담았어요"는 하지 못했다는 말이다. "담았"에 걸려 완료 표현으로 오인되지 않게 먼저 뺀다.
_NEGATED_ACTION = re.compile(
    r"(?<![가-힣])(못|안)\s*(담|넣|추가|빼|뺐|삭제|제거|지웠|바꿨|바꿔|변경|수정|줄였|늘렸|조정)[가-힣]*"
)
# 하지 않은 담기·빼기·수량 바꾸기를 앞으로 하겠다고 단정하는 말("장바구니 1번째 《한중록》을 빼드릴게요.")(#679).
# 과거형("뺐어요")만 보면 이 말은 그대로 나가서, 사용자는 빠질 줄 알았는데 장바구니가 그대로였다(11/11 dev 시험).
# 하지 않은 일이면 묻는 말로 바꾼다. 사용자가 "네"라고 하면 다음 턴에 그 책을 짚은 제안에 대한 동의로 처리된다.
# "원하시면 담아 드릴게요"처럼 조건을 단 말은 이미 제안이라 그대로 둔다.
# 묻는 말은 도구마다 한 꼴로 쓴다. 문지기의 행동 질문 꼴(app/gate/rules.py 의 _ACTION_QUESTION)이 알아보는 꼴이라야
# 다음 턴의 "네"가 장바구니 챗봇으로 간다. "추가해 드릴까요?"는 문지기가 모른다(#684 리뷰)
_PROMISE_FORMS: dict[ToolName, tuple[str, str, str, str]] = {
    # (… 드릴게요 앞의 말, … 게요 앞의 말, 바꿀 묻는 말, 짧게 바꿀 묻는 말)
    ToolName.CART_ADD: (
        r"담아|넣어|추가\s*해",
        r"담을|넣을|추가\s*할",
        "담아 드릴까요",
        "담을까요",
    ),
    ToolName.CART_REMOVE: (
        r"빼|삭제\s*해|제거\s*해|지워",
        r"뺄|삭제\s*할|제거\s*할|지울",
        "빼 드릴까요",
        "뺄까요",
    ),
    ToolName.CART_UPDATE: (
        r"바꿔|변경\s*해|수정\s*해|조정\s*해|늘려|줄여",
        r"바꿀|변경\s*할|수정\s*할|조정\s*할|늘릴|줄일",
        "바꿔 드릴까요",
        "바꿀까요",
    ),
}
_PROMISES: dict[ToolName, tuple[re.Pattern[str], re.Pattern[str]]] = {
    tool: (
        re.compile(rf"(?:{give})\s*드(?:릴게요|리겠(?:습니다|어요))"),
        re.compile(rf"(?:{will})게요"),
    )
    for tool, (give, will, _, _) in _PROMISE_FORMS.items()
}
_CONDITION = re.compile(r"[가-힣]면(?:\s|,|$)")
# 장바구니가 아닌 곳에서 빼거나 넣겠다는 말("그 책은 추천에서 뺄게요")은 장바구니 일이 아니다. 묻는 말로 바꾸면
# 다음 "네"가 장바구니 챗봇으로 간다(#684 리뷰). "장바구니 목록에서"는 장바구니다
_ELSEWHERE = re.compile(r"(?:추천|목록|후보|검색|결과)\s*에")
_END_MARK = re.compile(r"[.!~。]*\s*$")
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
    # 합계로 인정하는 금액. 장바구니 합계(마지막 조회 뒤에 쓰기가 성공했으면 빠진다)와 고른 조합의 합계
    totals: set[int]
    total_index: (
        int | None
    )  # 합계가 들어 있는 마지막 기록(cart.view, selection.pick)의 위치
    titles: dict[str, str]  # ISBN -> 이번 턴에 도구가 알려 준 책 제목


def _money(text: str) -> int:
    return int(text.replace(",", ""))


def _facts(run: AgentRun) -> _Facts:
    calls = run.tool_calls
    succeeded: set[ToolName] = set()
    allowed: set[int] = set()
    prices: list[int] = []
    totals: set[int] = set()
    view_index: int | None = None
    total_index: int | None = None
    titles = {line.isbn: line.title for line in run.ctx.cart_snapshot or []}
    for index, call in enumerate(calls):
        if call.name in guard.WRITE_TOOLS and call.result.get("ok") is True:
            succeeded.add(call.name)
            # 이미 담긴 책에 수량을 말해 담으면 수량을 바꾼 것이다(#489). "바꿨어요"도 사실이다
            if call.result.get("replaced") is True:
                succeeded.add(ToolName.CART_UPDATE)
        if call.name == ToolName.CART_VIEW and "items" in call.result:
            view_index = total_index = index
            allowed.add(call.result["total"])
            for item in call.result["items"]:
                titles[item["isbn"]] = item["title"]
                if item["price"] is not None:
                    allowed.update({item["price"], item["price"] * item["quantity"]})
                    prices.append(item["price"])
        if call.name == ToolName.BOOK_SEARCH and "books" in call.result:
            for book in call.result["books"]:
                titles[book["isbn"]] = book["title"]
                allowed.add(book["price"])
                prices.append(book["price"])
        if call.name == ToolName.BOOK_OFFER and "offers" in call.result:
            for offer in call.result["offers"]:
                titles[offer["isbn"]] = offer["title"]
                allowed.add(offer["price"])
                prices.append(offer["price"])
        if call.name == ToolName.BOOK_DETAIL and "books" in call.result:
            # 책 정보(#650): 판매가와 정가, 그 차이(할인 금액), 쿠폰의 정액 할인과 최소 주문 금액
            for book in call.result["books"]:
                titles[book["isbn"]] = book["title"]
                allowed.add(book["price"])
                prices.append(book["price"])
                if book["list_price"] is not None:
                    allowed.update(
                        {book["list_price"], book["list_price"] - book["price"]}
                    )
                for coupon in book.get("coupons") or []:
                    allowed.add(coupon["min_order_amount"])
                    if "discount_won" in coupon:
                        allowed.add(coupon["discount_won"])
        if call.name == ToolName.BOOK_REVIEWS and call.result.get("ok") is True:
            titles[call.result["isbn"]] = call.result["title"]
        if call.name == ToolName.SELECTION_PICK and call.result.get("ok") is True:
            total_index = index
            # 조합은 아직 담기 전이라 쓰기가 있어도 낡은 값이 되지 않는다
            totals.add(call.result["total_price"])
            allowed.add(call.result["total_price"])
            for book in call.result["chosen"]:
                titles[book["isbn"]] = book["title"]
                allowed.add(book["price"])
                prices.append(book["price"])
        if call.result.get("error") == ToolErrorCode.ALREADY_IN_CART.value:
            isbn = call.arguments.get("isbn")
            if isinstance(isbn, str) and isinstance(call.result.get("title"), str):
                titles[isbn] = call.result["title"]

    if view_index is not None:
        wrote_after = any(
            c.name in guard.WRITE_TOOLS and c.result.get("ok") is True
            for c in calls[view_index + 1 :]
        )
        # 조회한 뒤에 장바구니가 바뀌었다면 그 합계는 이미 낡았다. 다시 조회해야 한다
        if not wrote_after:
            totals.add(calls[view_index].result["total"])
    return _Facts(succeeded, allowed, prices, totals, total_index, titles)


def _mask(sentence: str, titles: dict[str, str]) -> str:
    """검사할 때만 책 제목을 가린다. "《총, 균, 쇠》는 22,000원"의 "총"이 합계 말로, 제목 속 금액이 가격
    주장으로 읽히지 않게 하려는 것이다. 사용자에게 가는 글은 가리지 않는다."""
    masked = _BRACKETED_TITLE.sub(_MASK, sentence)
    for title in sorted(
        {t for t in titles.values() if len(t) >= 2}, key=len, reverse=True
    ):
        masked = masked.replace(title, _MASK)
    return masked


def _promised(sentence: str, facts: _Facts) -> ToolName | None:
    """하지 않은 장바구니 변경을 하겠다고 단정한 문장이면 그 도구, 아니면 None."""
    if sentence.rstrip().endswith("?") or _CONDITION.search(sentence):
        return None
    if "장바구니" not in sentence and _ELSEWHERE.search(sentence):
        return None
    for tool, patterns in _PROMISES.items():
        if tool not in facts.succeeded and any(p.search(sentence) for p in patterns):
            return tool
    return None


def _as_question(sentence: str, tool: ToolName) -> str:
    """약속한 말을 그 도구의 묻는 꼴로 바꾼다("《한중록》을 삭제해 드릴게요." → "《한중록》을 빼 드릴까요?")."""
    give, will = _PROMISES[tool]
    _, _, ask, ask_short = _PROMISE_FORMS[tool]
    asked, n = give.subn(ask, sentence, count=1)
    if n == 0:
        asked = will.sub(ask_short, sentence, count=1)
    return _END_MARK.sub("?", asked, count=1)


def _told(calls: list[ToolCall]) -> set[ToolName]:
    """코드가 결과 문장을 따로 말하는 쓰기 도구. BE 까지 갔거나 로그인 때문에 막힌 호출이다(_outcomes).

    이런 도구를 하겠다는 약속은 묻는 말로 바꾸지 않고 지운다. BE 가 거절한 빼기 뒤에 "빼 드릴까요?"가 붙으면
    실패 문장과 어긋난다(#684 리뷰). 근거가 없어 막힌 호출은 BE 에 가지 않았으니 묻는 말로 바꾼다.
    """
    told = {
        call.name
        for call in calls
        if call.name in guard.WRITE_TOOLS
        and (
            call.result.get("ok") is True
            or call.result.get("error") in _REACHED_BE
            or call.result.get("error") == ToolErrorCode.LOGIN_REQUIRED.value
        )
    }
    # 주문 화면 열기(order.open, #664)가 성공했으면 짚은 책은 담겨 있고 주문 화면 결과는 코드가 말한다. 그 턴의
    # "담아 드릴게요"를 "담아 드릴까요?"로 바꾸면 이미 담은 책을 다시 묻는다
    if any(
        call.name == ToolName.ORDER_OPEN and call.result.get("ok") is True
        for call in calls
    ):
        told.add(ToolName.CART_ADD)
    return told


# 책을 번호로 짚은 말("카드 2번", "장바구니 1번째")
_NAMED_NUMBER = re.compile(r"\d+\s*번")
# 하지 않은 일을 했다고 쓴 문장을 묻는 말로 바꿀 때 쓰는 동사(#679)
_CLAIM_QUESTIONS = {
    ToolName.CART_ADD: "담을까요?",
    ToolName.CART_REMOVE: "뺄까요?",
    ToolName.CART_UPDATE: "바꿀까요?",
}


def _claim_question(sentence: str, facts: _Facts, calls: list[ToolCall]) -> str | None:
    """도구를 부르지도 않고 장바구니를 바꿨다고 쓴 문장("《한중록》을 장바구니에서 뺐어요.")을 묻는 말("《한중록》을
    장바구니에서 뺄까요?")로 바꾼다. 한 가지 일만 말했고 그 도구를 이 턴에 한 번도 안 불렀을 때만이다. 불렀다가
    실패했으면 실패를 코드가 따로 말한다."""
    if sentence.rstrip().endswith("?"):
        return None
    claimable = _NEGATED_ACTION.sub("", _mask(sentence, facts.titles))
    if _ORDER_CLAIM.search(claimable):
        return None
    claimed = [tool for tool, p in _ACTION_CLAIMS.items() if p.search(claimable)]
    if len(claimed) != 1 or any(call.name == claimed[0] for call in calls):
        return None
    match = _ACTION_CLAIMS[claimed[0]].search(sentence)
    if match is None:  # 제목을 가리기 전에는 다른 곳에서 걸린다
        return None
    head = sentence[: match.start()].rstrip()
    # 어느 책인지 짚은 문장만 묻는 말이 된다. "수량을 늘렸어요"를 "수량을 바꿀까요?"로 물으면 답할 수가 없다
    if not (_BRACKETED_TITLE.search(head) or _NAMED_NUMBER.search(head)):
        return None
    return f"{head} {_CLAIM_QUESTIONS[claimed[0]]}"


def _held_asks(calls: list[ToolCall]) -> list[str]:
    """근거가 없어 막힌 쓰기에 코드가 만든 되묻기 문장("장바구니 1번째 《한중록》을 뺄까요?", #531)."""
    asks = [
        call.result["ask"]
        for call in calls
        if call.name in guard.WRITE_TOOLS
        and call.result.get("ok") is False
        and isinstance(call.result.get("ask"), str)
    ]
    return list(dict.fromkeys(asks))


def _check(sentence: str, facts: _Facts) -> tuple[str, int | None] | None:
    """문장이 기록과 어긋나면 (이유, grounded 를 false 로 할 기록 위치), 괜찮으면 None."""
    if sentence.rstrip().endswith("?"):
        return None  # 사용자에게 묻는 문장은 주장이 아니다

    masked = _mask(sentence, facts.titles)
    if _ORDER_CLAIM.search(_NEGATED_ACTION.sub("", masked)):
        return "ORDER_NOT_DONE", None  # 주문서를 만들거나 결제하지 않는다(#631)
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
        if amount not in facts.totals:
            return "TOTAL_MISMATCH", facts.total_index

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
    {code.value for code in CommerceErrorCode}
    | {ToolErrorCode.ALREADY_IN_CART.value, ToolErrorCode.BATCH_ROLLED_BACK.value}
)
# 한 번에 시킨 쓰기 중 하나가 걸려서 함께 되돌린 것(#488, #489). 걸린 쪽의 이유는 그 호출의 문장이 말한다
_TOGETHER = "함께 부탁하신 것 중 하나가 안 돼서"
LOGIN_REPLY = "로그인이 필요해요. 로그인한 뒤에 다시 말씀해 주세요."


def _book(run: AgentRun, titles: dict[str, str], isbn: str) -> tuple[str, str]:
    """(사용자에게 보일 이름, 조사를 고를 때 볼 말). 제목을 모르면 사용자가 말한 번호로 부른다.

    카드 책은 도구가 제목을 알려 주지 않았어도 턴 시작에 미리 읽은 제목(요청의 offers)으로 "2번 《제목》"이라 부른다.
    "2번 책을 담았어요"만으로는 사용자가 보던 2번과 담긴 책이 달라도 알 수 없었다(#701).
    """
    if title := titles.get(isbn):
        return f"《{title}》", title
    for card in run.ctx.cards:
        if card.isbn == isbn:
            if title := run.ctx.titles.get(isbn):
                return f"{card.rank}번 《{title}》", title
            return f"{card.rank}번 책", "책"
    if run.ctx.focused_isbn == isbn:
        return "지금 보시는 책", "책"
    return "선택하신 책", "책"


def _left(result: dict[str, Any]) -> str:
    """재고 부족 결과에 남은 수량이 있으면 붙일 문장(#692). 0권이면 품절이라 "재고가 부족해서"로 충분하다."""
    left = result.get("stock")
    if isinstance(left, int) and not isinstance(left, bool) and left > 0:
        return f" 남은 재고는 {left}권이에요."
    return ""


def _outcome(name: ToolName, shown: str, plain: str, call: ToolCall) -> str:
    """쓰기 호출 하나의 결과를 사용자에게 말한다. 수량은 코드가 채운 인자에서 읽는다."""
    eul = _particle(plain, "을", "를")
    eun = _particle(plain, "은", "는")
    result = call.result
    error = result.get("error")
    quantity = call.arguments.get("quantity")
    count = quantity if isinstance(quantity, int) else None

    if name == ToolName.CART_ADD:
        if result.get("ok") is True and result.get("replaced") is True:
            return f"{shown}의 수량을 {count}권으로 바꿨어요."
        if result.get("ok") is True:
            if count is not None and count > 1:
                return f"{shown} {count}권을 장바구니에 담았어요."
            return f"{shown}{eul} 장바구니에 담았어요."
        if error == ToolErrorCode.BATCH_ROLLED_BACK.value:
            return f"{_TOGETHER} {shown}도 담지 않았어요."
        if error == ToolErrorCode.ALREADY_IN_CART.value:
            have = result.get("quantity")
            has = f" {have}권" if isinstance(have, int) else ""
            return f"{shown}{eun} 이미 장바구니에{has} 담겨 있어서 그대로 뒀어요."
        if error == CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK.value:
            return f"{shown}{eun} 재고가 부족해서 담지 못했어요.{_left(result)}"
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
        if error == ToolErrorCode.BATCH_ROLLED_BACK.value:
            return f"{_TOGETHER} {shown}의 수량도 바꾸지 않았어요."
        if error == CommerceErrorCode.INSUFFICIENT_PRODUCT_STOCK.value:
            return f"재고가 부족해서 {shown}의 수량을 바꾸지 못했어요.{_left(result)}"
        if error == CommerceErrorCode.INVALID_CART_ITEM_QUANTITY.value:
            return f"수량이 올바르지 않아 {shown}의 수량을 바꾸지 못했어요."
        if error == CommerceErrorCode.CART_ITEM_NOT_FOUND.value:
            return f"{shown}{eun} 장바구니에 없어서 수량을 바꾸지 못했어요."
        return f"{shown}의 수량을 바꾸지 못했어요."

    # CART_REMOVE
    if result.get("ok") is True:
        return f"{shown}{eul} 장바구니에서 뺐어요."
    if error == ToolErrorCode.BATCH_ROLLED_BACK.value:
        return f"{_TOGETHER} {shown}도 빼지 않았어요."
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
        earlier = attempts.get((call.name, isbn))
        if (
            earlier is not None
            and earlier.result.get("ok") is True
            and call.result.get("error") == ToolErrorCode.ALREADY_IN_CART.value
        ):
            # 이번 턴에 담은 책을 모델이 한 번 더 담으려 한 것이다(코드가 고른 책을 담은 뒤 등, #631). 담은 것이 사실이다
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


def _picked(calls: list[ToolCall]) -> ToolCall | None:
    """마지막으로 성공한 selection.pick 기록."""
    for call in reversed(calls):
        if call.name == ToolName.SELECTION_PICK and call.result.get("ok") is True:
            return call
    return None


# 찾은 책으로 고른 조합을 확인 버튼으로 받지 못할 때 붙이는 안내(#664)
PICK_SAY_TITLE = "담으시려면 담고 싶은 책 제목을 말씀해 주세요."


def _known_to_be(ctx: TurnContext, isbn: str) -> bool:
    """BE 가 이 턴의 요청으로 아는 책인가(카드, 장바구니, 지금 보는 책). BE 는 고른 조합(selection)의 책을 이 안에서만
    찾고, 없으면 턴 전체를 E502 로 거절한다(BE CallAgentChatUseCase.validate, 11/11 dev). 찾은 책(book.search)은
    BE 가 모른다."""
    return (
        any(card.isbn == isbn for card in ctx.cards)
        or any(line.isbn == isbn for line in ctx.cart_snapshot or [])
        or ctx.focused_isbn == isbn
    )


def _confirmable(ctx: TurnContext, picked: ToolCall) -> bool:
    """고른 조합을 확인 버튼(selection)으로 보낼 수 있는가. 찾은 책이 섞여 있으면 BE 가 거절하므로 보내지 않는다."""
    return all(_known_to_be(ctx, book["isbn"]) for book in picked.result["chosen"])


# 사용자의 말로 책이 하나로 정해지지 않아 막힌 쓰기. 그 호출의 책은 모델이 고른 것이다
_UNDECIDED = frozenset(
    {ToolErrorCode.REFERENCE_NOT_STATED.value, ToolErrorCode.AMBIGUOUS_BOOK.value}
)


def _resolved(run: AgentRun) -> ResolvedReference | None:
    """실행한 도구가 풀어 낸 책. 코드가 채운 ISBN 만 쓴다."""
    isbns: list[str] = []
    for call in run.tool_calls:
        # 근거 검사에 막힌 쓰기의 책은 모델이 고른 책이다. 사용자가 정한 책이 아니다(#418, #528)
        if call.result.get("error") in _UNDECIDED:
            continue
        if "isbn" in call.arguments:
            found = [call.arguments["isbn"]]
        elif "isbns" in call.arguments:
            found = list(call.arguments["isbns"])
        else:
            continue
        for isbn in found:
            # 책을 풀기 전에 막힌 호출은 LLM 이 준 인자가 그대로 남아 있다. 요청 안에서 온 책만 인정한다
            # 사용자가 가리킨 책만 "정해진 책"이다. 되묻으려고 선택지를 조회한 책은 정해진 것이 아니다(#387)
            if (
                isinstance(isbn, str)
                and label(run.ctx, isbn) != "목록 밖의 책"
                and isbn not in isbns
                and evidence.stated(run.ctx, isbn)
            ):
                isbns.append(isbn)
    parts = [label(run.ctx, i) for i in isbns]

    # 고른 조합. 사용자가 가리킨 책은 아니지만 "3만원 2권"이 가리키는 책이다. 코드가 고른 ISBN 이다
    picked = _picked(run.tool_calls)
    if picked is not None:
        parts.append(
            f"{picked.arguments['budget_total']:,}원 {picked.arguments['count']}권"
        )
        isbns.extend(
            book["isbn"]
            for book in picked.result["chosen"]
            if book["isbn"] not in isbns
        )

    if not isbns:
        return None
    return ResolvedReference(ref=", ".join(parts), isbns=isbns)


def _wrote(calls: list[ToolCall]) -> bool:
    return any(
        c.name in guard.WRITE_TOOLS and c.result.get("ok") is True for c in calls
    )


def _selection(calls: list[ToolCall], ctx: TurnContext) -> Selection | None:
    """코드가 고른 조합. 골라 담기를 하지 않았거나 고르지 못했으면 None.

    골라서 담으라고 맡겨 고른 책을 이미 담았으면(#631) 확인받을 조합이 아니다. BE 는 selection 이 있으면 actions 를
    보지 않고 담기 제안만 저장하므로 싣지 않는다.
    """
    picked = _picked(calls)
    if picked is None or _wrote(calls) or not _confirmable(ctx, picked):
        return None
    return Selection(
        goal="budget_pick",
        constraints=SelectionConstraints(**picked.arguments),
        chosen=[
            SelectionItem(
                isbn=book["isbn"], price=book["price"], match_score=book["match_score"]
            )
            for book in picked.result["chosen"]
        ],
        total_price=picked.result["total_price"],
        alternatives_considered=picked.result["alternatives_considered"],
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


def _buttons(calls: list[ToolCall], ctx: TurnContext, order: bool) -> list[Button]:
    logged_in = ctx.logged_in
    buttons: list[Button] = []
    seen: set[str] = set()

    # AI 가 고른 조합은 담지 않고 이 버튼으로 확인받는다. 로그인하지 않았으면 담을 수 없으니 만들지 않는다.
    # 골라서 담으라고 맡겨 이미 담았으면 확인받지 않는다(#631)
    picked = _picked(calls)
    if (
        picked is not None
        and logged_in
        and not _wrote(calls)
        and _confirmable(ctx, picked)
    ):
        isbns = [book["isbn"] for book in picked.result["chosen"]]
        buttons.append(Button(label="담기", action="confirm_selection", isbns=isbns))

    def navigate(text: str, target: str) -> None:
        if target not in seen:
            seen.add(target)
            buttons.append(Button(label=text, action="navigate", target=target))

    for call in calls:
        # 주문 화면으로 보내는 턴에는 다른 화면 안내를 붙이지 않는다. FE 가 바로 주문 화면으로 간다(#631)
        if (
            call.name == ToolName.UI_GUIDE
            and call.result.get("ok") is True
            and not order
        ):
            target = call.arguments["target"]
            navigate(UI_TARGETS[target], target)
    if order:
        # "주문해 줘"로 담았으면 장바구니 대신 주문 화면으로 보낸다(#631)
        navigate(ORDER_LABEL, ORDER_SCREEN)
    elif any(_touched_cart(call) for call in calls):
        navigate(UI_TARGETS[CART_SCREEN], CART_SCREEN)
    return buttons


def _coupon_name(name: str) -> str:
    return name if "쿠폰" in name else f"{name} 쿠폰"


# 앞 턴에 담아 둔 책으로 연 주문 화면(#678)에서 이름을 들어 말하는 책 수. 넘으면 "외 n종"으로 줄인다
_ORDERED_NAMED = 3


def _ordered_line(lines: list[CartLine]) -> str:
    """장바구니에 담아 둔 책으로 주문 화면을 연 결과. 책과 수량은 장바구니 값이다."""
    names = ", ".join(f"《{line.title}》" for line in lines[:_ORDERED_NAMED])
    if len(lines) > _ORDERED_NAMED:
        names += f" 외 {len(lines) - _ORDERED_NAMED}종"
    copies = sum(line.quantity for line in lines)
    # 한 권이면 "모두"를 붙이지 않는다("《한중록》 1권으로", #683 리뷰)
    counted = f"{names} {copies}권" if len(lines) == 1 else f"{names}, 모두 {copies}권"
    return f"장바구니에 담긴 {counted}으로 주문 화면을 열어 둘게요."


def _checkout_line(plan: Plan) -> str:
    """주문 화면에 채울 쿠폰·포인트 안내. 금액은 코드가 계산한 값이라 답변 검사를 거치지 않는다.

    사용자가 쓰지 말라고 한 것은 쓰지 않게 해 둔다고 알린다. 말없이 빼면 적용하지 말라는 말을 들었는지 모른다."""
    declined = [
        name
        for name, off in (
            ("쿠폰", plan.coupon_declined),
            ("포인트", plan.points_declined),
        )
        if off
    ]
    skip = (
        f"{'과 '.join(declined)}{'는' if declined[-1] == '포인트' else '은'} 쓰지 않게 해 둘게요. "
        if declined
        else ""
    )
    if plan.coupon is not None and plan.use_point > 0:
        applied = f"{_coupon_name(plan.coupon.name)}({plan.discount:,}원 할인)과 포인트 {plan.use_point:,}원을"
    elif plan.coupon is not None:
        applied = f"{_coupon_name(plan.coupon.name)}({plan.discount:,}원 할인)을"
    elif plan.use_point > 0:
        applied = f"포인트 {plan.use_point:,}원을"
    else:
        return f"{skip}주문 화면에서 확인하고 주문해 주세요."
    return f"{skip}주문 화면에 {applied} 적용해 둘게요. 확인하고 주문해 주세요."


def _cannot_order(run: AgentRun, *, words_only: bool = False) -> bool:
    """주문해 달라고 했는데 주문 화면으로 보낼 것이 없다(주문 기능이 켜져 있을 때만). words_only 면 주문·결제
    낱말만으로 된 말("주문", "결제할게요")만 본다."""
    if not run.ctx.checkout_enabled or run.checkout is not None:
        return False
    if words_only:
        return evidence.orders_alone(run.ctx)
    return evidence.wants_order(run.ctx)


def _order_from_cart_reply() -> AgentActData:
    """쇼핑이 아니라고 답했지만 주문하겠다는 말이었을 때의 응답. 장바구니로 보낸다."""
    return AgentActData(
        reply=ORDER_NO_BOOK,
        not_shopping=False,
        actions=[],
        tool_calls=[],
        resolved_reference=None,
        selection=None,
        buttons=[
            Button(label=UI_TARGETS[CART_SCREEN], action="navigate", target=CART_SCREEN)
        ],
    )


def _checkout(plan: Plan | None) -> Checkout | None:
    if plan is None:
        return None
    coupon = plan.coupon.owned_coupon_id if plan.coupon is not None else None
    return Checkout(owned_coupon_id=coupon, use_point=plan.use_point)


def finish(run: AgentRun) -> Finished:
    """그래프 결과를 검사해 응답으로 만든다. 남는 답이 없으면 GroundingFailed."""
    if run.not_shopping:
        # 쇼핑이 아니라는 답을 뒤집는 것은 주문 낱말 한 마디뿐이다. 주문 기능을 켜면 문지기가 "한 권 사고 싶은데
        # 뭐가 좋을까요" 같은 추천 요청도 쇼핑으로 보내는데, 이런 말은 그대로 추천으로 돌려보낸다(#649 리뷰)
        if _cannot_order(run, words_only=True):
            return Finished(_order_from_cart_reply(), [])
        return Finished(AgentActData.not_shopping_reply(), [])

    text = _plain(run.final_text)
    if text.strip() == NOT_SHOPPING:
        text = ""  # 도구를 부른 뒤에 나온 약속어는 사용자에게 보이지 않는다

    facts = _facts(run)
    removed: list[str] = []
    flagged: set[int] = set()
    kept_lines: list[str] = []
    claim_questions: list[str] = []
    told = _told(run.tool_calls)
    picked = _picked(run.tool_calls)
    # 찾은 책으로 고른 조합은 확인 버튼을 못 붙인다(#664). 모델이 "담기 버튼을 누르면"이라고 해도 버튼이 없다
    no_button = (
        picked is not None
        and run.ctx.logged_in
        and not _wrote(run.tool_calls)
        and not _confirmable(run.ctx, picked)
    )
    for line in _units(text):
        kept: list[str] = []
        for sentence in line:
            if no_button and "버튼" in sentence:
                removed.append("NO_PICK_BUTTON")
                continue
            problem = _check(sentence, facts)
            # 약속을 묻는 말로 바꾸는 것은 금액 등 다른 검사를 지난 문장만이다. 바꾼 문장은 "?"로 끝나 검사를
            # 건너뛰므로, 먼저 바꾸면 틀린 금액이 묻는 말에 실려 나갔다(#684 리뷰)
            promised = _promised(sentence, facts) if problem is None else None
            if promised is not None:
                removed.append("ACTION_PROMISED")
                if promised not in told:
                    kept.append(_as_question(sentence, promised))
                continue
            if problem is None:
                kept.append(sentence)
                continue
            reason, flag_at = problem
            removed.append(reason)
            if reason == "ACTION_NOT_DONE" and (
                question := _claim_question(sentence, facts, run.tool_calls)
            ):
                claim_questions.append(question)
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
    if run.ordered_cart:
        outcome_lines.append(_ordered_line(run.ordered_cart))
    if run.ordered_cart or (run.unorderable and run.checkout is None):
        # 장바구니를 바꾸지 않고 주문 화면만 연 턴(앞 턴에 담아 둔 책)이거나, 짚은 책이 모두 살 수 없어 열지 못한 턴이다
        # (#678). 모델은 "주문 화면으로 넘겨드렸습니다. 주문내역에서 확인해 주세요", "장바구니에서 확인하실 수
        # 있습니다"처럼 다른 화면을 가리키는 말을 덧붙이곤 했다(11/11 실제 모델 10번 중 7번). 이 턴에 모델이 더 말할
        # 결과가 없으므로 결과는 코드가 말한다
        kept_lines = []
    for line in run.unorderable:
        eun = _particle(line.title, "은", "는")
        outcome_lines.append(
            f"《{line.title}》{eun} 지금 살 수 없어서 주문 화면에 넣지 않았어요."
        )
    if run.checkout is not None:
        outcome_lines.append(_checkout_line(run.checkout))
    elif run.ctx.checkout_enabled and run.actions and evidence.mentions_coupon(run.ctx):
        # "쿠폰 적용해서 장바구니에 넣어 줘": 장바구니에는 쿠폰이 없다(#631)
        outcome_lines.append(COUPON_LATER)
    elif _cannot_order(run) and any(
        c.result.get("error") == ToolErrorCode.ALREADY_IN_CART.value
        for c in run.tool_calls
    ):
        outcome_lines.append(ORDER_NO_BOOK)
    if no_button:
        kept_lines.append(PICK_SAY_TITLE)
    reply = "\n".join([" ".join(outcome_lines)] * bool(outcome_lines) + kept_lines)
    if not reply:
        # 모델이 막힌 쓰기를 "뺐어요"라고 써서 지웠더니 남는 말이 없다. 코드가 만든 되묻기가 있으면 그것으로 답한다.
        # 422 는 BE 에서 오류 화면이 된다(11/11 dev 시험, #679)
        # 되묻기가 없으면(모델이 도구를 부르지도 않고 "뺐어요"라고 썼으면) 그 문장을 묻는 말로 바꿔 답한다
        asks = _held_asks(run.tool_calls) or claim_questions[:1]
        if asks:
            reply = " ".join(asks)
        elif removed:
            raise GroundingFailed(removed)
        else:
            reply = FALLBACK_REPLY

    calls = [
        c.model_copy(update={"grounded": False}) if i in flagged else c
        for i, c in enumerate(run.tool_calls)
    ]
    data = AgentActData(
        reply=reply,
        not_shopping=False,
        actions=[
            Action(op=a.op, isbn=a.isbn, quantity=a.quantity) for a in run.actions
        ],
        tool_calls=calls,
        resolved_reference=_resolved(run),
        selection=_selection(calls, run.ctx),
        buttons=_buttons(calls, run.ctx, order=run.checkout is not None),
        checkout=_checkout(run.checkout),
    )
    return Finished(data, removed)
