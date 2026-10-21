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

**v2(#530).** ③이 대화로 원하는 책을 좁혀 가게 되면서 세 가지를 바꿨다. v1 의 측정값(갈래 80.3%, 경계 사례
51.7%)은 v1 의 숫자다.
1. 카드를 가리키는 말에 추천 낱말이 같이 있고 장바구니 낱말이 없으면("2번 같은 거") 추천이다. v1 은 ``both`` 로
   ⑦에 보냈고, ⑦이 쇼핑이 아니라고 돌려보내 결과는 같았지만 LLM 호출이 한 번 더 들었다.
2. 직전 챗봇 말이 ③의 묻기(넓은 첫 요청에 원하는 것을 물은 말, app/chat/asking.py)면, 장바구니 낱말이 없는
   답("비전공자인데 처음 배워 보려고", "아무거나")은 추천이다. 이런 답엔 추천 낱말이 없어 v1 은 ⑦에 들렀다가
   되돌아왔다. 최근 대화에서 직전 챗봇 말만 본다.
3. 카드가 보일 때 책을 평가하는 말("1번 책이 흥미로워 보여요", "『번 아웃 탈출기』가 좋을 것 같아", "1번은 좀
   다를 것 같아")은 추천이다. 번호 없이 제목으로 말해도 같다. ⑦로 가면 "담을까요?"만 되풀이했다(#530 대화 평가:
   10개 중 8개 대화). 직전 챗봇 말이 담기·빼기를 물었으면("카드 1번을 담을까요?") 그 답이라 ⑦로 보낸다.

**확실한 가름과 애매한 가름(#530).** ``decide`` 는 갈래와 함께 그 갈래가 낱말 근거로 정해졌는지(sure)를 준다.
낱말 근거 없이 기본값으로 정한 경우 — 아무 신호도 없는 말, 카드 번호만 있고 행동·추천 낱말이 없는 말 — 는
애매하다. 문지기는 애매한 말만 작은 판정 호출(app/gate/judge.py)에 맡긴다. 머지 전 점검 45턴에서 갈래가 틀린 7턴이
모두 애매한 쪽이었고("1만5천원 이하로", "다른 거 보여줘", "2번은 어떤 내용이야?" 등, ⑦이 예산 골라 담기나 카드
되읊기로 받음), 확실한 쪽 34턴은 하나도 틀리지 않았다. 갈래를 가르는 낱말 규칙 자체는 v2 그대로다.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
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
# 책을 평가하는 말("좋아 보여", "따뜻해 보여요", "괜찮을 것 같아", "마음에 들어"). "1번 보여줘"는 아니다
_INTEREST = re.compile(
    r"(?:아|어|해|워)\s*보(?:여|이|인다)(?!\s*줘|\s*주)|것\s*같|마음에\s*들|흥미로|끌리"
)
# ⑦이 담기·빼기·수량 변경을 물은 문장("카드 1번을 담을까요?")
_ACTION_QUESTION = re.compile(r"(?:담|넣|뺄|빼|바꿀)[^.!?\n]*(?:\?|까요)")


class _Turn(Protocol):
    role: str
    text: str


def asked_action(recent_turns: Sequence[_Turn]) -> bool:
    """직전 챗봇 말이 담기·빼기·수량 변경을 물었나."""
    for turn in reversed(recent_turns):
        if turn.role == "assistant":
            return bool(_ACTION_QUESTION.search(turn.text))
        if turn.role == "user":
            return False
    return False


@dataclass(frozen=True)
class Decision:
    """규칙이 가른 갈래와, 그것이 낱말 근거로 정해졌는지(sure). basis 는 로그에 남길 근거다."""

    kind: Kind
    sure: bool
    basis: str


def decide(
    message: str,
    cards_shown: bool,
    after_ask: bool = False,
    after_action: bool = False,
) -> Decision:
    """말 하나를 네 갈래 중 하나로 가르고, 그 갈래가 확실한지 함께 준다.

    1. 장바구니 낱말이 있으면 장바구니 신호. 추천 낱말도 있으면 ``both``
    2. 직전 챗봇 말이 ③의 묻기면(after_ask) 나머지는 추천(v2)
    3. 카드가 보일 때 책을 평가하는 말은 추천이다. 직전에 담기를 물었으면 그 답이라 아니다(v2)
    4. 카드가 보일 때 카드를 가리키는 말은 장바구니 신호. 추천 낱말도 있으면 추천(v2). 행동 낱말도 추천 낱말도
       없으면 애매하다. 직전에 ⑦이 담기를 물었으면 그 답이라 확실하다
    5. 추천 낱말만 있으면 추천. 아무 신호도 없으면 ``neither`` 이고 애매하다. 직전에 ⑦이 담기를 물었으면 그 답이라
       확실하다
    """
    text = message.strip()
    cart_words = bool(_CART_WORDS.search(text))
    pointer = cards_shown and bool(_POINTER.search(text))
    recommend = bool(_REC_WORDS.search(text))
    if cart_words:
        return Decision("both" if recommend else "cart", True, "장바구니 낱말")
    if after_ask:
        return Decision("recommend", True, "묻기의 답")
    if cards_shown and not after_action and _INTEREST.search(text):
        return Decision("recommend", True, "카드 평가 말")
    if pointer:
        if recommend:
            return Decision("recommend", True, "카드와 추천 낱말")
        return Decision(
            "cart", after_action, "⑦ 질문의 답" if after_action else "카드 번호만"
        )
    if recommend:
        return Decision("recommend", True, "추천 낱말")
    return Decision(
        "neither", after_action, "⑦ 질문의 답" if after_action else "낱말 근거 없음"
    )


def classify(
    message: str,
    cards_shown: bool,
    after_ask: bool = False,
    after_action: bool = False,
) -> Kind:
    """말 하나를 네 갈래 중 하나로 가른다(``decide`` 의 갈래만)."""
    return decide(message, cards_shown, after_ask, after_action).kind


def route(
    message: str,
    cards_shown: bool,
    after_ask: bool = False,
    after_action: bool = False,
) -> Lane:
    """③으로 바로 보낼지(``recommend``), ⑦로 보낼지(``shop``)."""
    kind = classify(message, cards_shown, after_ask, after_action)
    return "recommend" if kind == "recommend" else "shop"
