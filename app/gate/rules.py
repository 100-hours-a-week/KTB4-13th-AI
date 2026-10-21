"""문지기 규칙: 말 한 줄과 카드가 보이는지로 ③(추천)과 ⑦(쇼핑)을 가른다. LLM 을 부르지 않는다(#413).

규칙은 라우팅 기준선(#404) v1 과 같다. 사례에 맞춰 다듬지 않은 값이라 측정한 정확도(갈래 80.3%,
경계 사례 51.7%)가 그대로 이 규칙의 숫자다. 규칙을 바꾸면 ``RULES_VERSION`` 을 올리고 이유를 PR 에 적는다.

**잘못 가를 때의 비용이 한쪽으로 쏠리게 만든 것이 이 구조의 핵심이다.**
- 추천 낱말만 있으면 ③으로 바로 보낸다. 장바구니 말이 여기로 새면 ③이 엉뚱한 추천을 답한다(장바구니는
  안 바뀐다). 기준선에서 72개 중 1개(1.4%)였다.
- 그 밖은 전부 ⑦로 보낸다. 추천 말이 ⑦로 가도 ⑦이 ``NOT_SHOPPING`` 으로 거절하면 ③으로 되돌리므로
  결과는 같고 몇 초가 더 들 뿐이다.

서버는 대화를 저장하지 않아서 기준선이 쓰던 "직전 턴의 갈래를 따라가기"는 없다. 낱말 신호가 없는 짧은 말
("네", "다른 거")은 ⑦로 갔다가 되돌려진다.

**v2(#530).** ③이 넓은 요청에 갈래마다 한 권씩 보여 주고 "1번처럼 …, 2번처럼 … 중 어느 쪽이 끌리세요?"라고 묻게
되면서 두 가지를 바꿨다. v1 의 측정값(갈래 80.3%, 경계 사례 51.7%)은 v1 의 숫자다.
1. 카드를 가리키는 말에 추천 낱말이 같이 있고 장바구니 낱말이 없으면("2번 같은 거") 추천이다. v1 은 ``both`` 로
   ⑦에 보냈고, ⑦이 쇼핑이 아니라고 돌려보내 결과는 같았지만 LLM 호출이 한 번 더 들었다.
2. 직전 챗봇 말이 갈래 질문이면, 장바구니 낱말이 없는 답("2번", "두 번째 쪽", "담담한 거")은 추천이다. 이 답이
   ⑦로 가면 "2번"을 담을 책으로 읽을 수 있다. 최근 대화에서 직전 챗봇 말만 본다.
"""

import re
from collections.abc import Sequence
from typing import Literal, Protocol

RULES_VERSION = 2

Lane = Literal["recommend", "shop"]
# 기준선의 네 갈래. 문지기는 ``recommend`` 만 ③으로 보내고 나머지는 ⑦로 보낸다
Kind = Literal["recommend", "cart", "both", "neither"]

# 장바구니 쪽 낱말: 담기, 빼기, 수량, 장바구니, 가격과 재고, 합계, 주문 관련, 비교
_CART_WORDS = re.compile(
    r"담아|담고|담기|담을|넣어|넣고|빼줘|빼 줘|빼 주|빼고|장바구니|수량|권으로"
    r"|재고|품절|가격|얼마|합계|합치|배송|주문|결제|환불|취소|비교"
)
# 카드나 위치를 가리키는 말. 카드가 화면에 있을 때만 장바구니 신호로 본다
_POINTER = re.compile(r"\d+\s*번|[첫두세네]\s?번째|이거|그거|이 책|그 책")
# 새 책을 찾는 말. "보여줘"처럼 장바구니 말에도 쓰이는 낱말은 넣지 않았다
_REC_WORDS = re.compile(
    r"추천|찾아|읽을|읽고 싶|비슷한|같은 책|입문서|필독서|있을까|없을까|책 있|소설|에세이"
    r"|같은 거|같은 걸|같은 느낌|같은 결|쪽으로|쪽이요|쪽이 좋|쪽이 끌|끌려"
)
# ③의 갈래 질문(app/routers/chat.py _QUESTION_NARROW: "… 중 어느 쪽이 끌리세요?")
_BRANCH_QUESTION = re.compile(r"(어느|어떤)\s?쪽")


class _Turn(Protocol):
    role: str
    text: str


def asked_branch(recent_turns: Sequence[_Turn]) -> bool:
    """직전 챗봇 말이 ③의 갈래 질문이었나."""
    for turn in reversed(recent_turns):
        if turn.role == "assistant":
            return bool(_BRANCH_QUESTION.search(turn.text))
        if turn.role == "user":
            return False
    return False


def classify(message: str, cards_shown: bool, after_branch: bool = False) -> Kind:
    """말 하나를 네 갈래 중 하나로 가른다.

    1. 장바구니 낱말이 있으면 장바구니 신호. 추천 낱말도 있으면 ``both``
    2. 직전 챗봇 말이 갈래 질문이면(after_branch) 나머지는 추천(v2)
    3. 카드가 보일 때 카드를 가리키는 말은 장바구니 신호. 추천 낱말도 있으면 추천(v2)
    4. 추천 낱말만 있으면 추천, 아무 신호도 없으면 ``neither``
    """
    text = message.strip()
    cart_words = bool(_CART_WORDS.search(text))
    pointer = cards_shown and bool(_POINTER.search(text))
    recommend = bool(_REC_WORDS.search(text))
    if cart_words:
        return "both" if recommend else "cart"
    if after_branch:
        return "recommend"
    if pointer:
        return "recommend" if recommend else "cart"
    if recommend:
        return "recommend"
    return "neither"


def route(message: str, cards_shown: bool, after_branch: bool = False) -> Lane:
    """③으로 바로 보낼지(``recommend``), ⑦로 보낼지(``shop``)."""
    kind = classify(message, cards_shown, after_branch)
    return "recommend" if kind == "recommend" else "shop"
