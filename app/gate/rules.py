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
"""

import re
from typing import Literal

RULES_VERSION = 1

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
)


def classify(message: str, cards_shown: bool) -> Kind:
    """말 하나를 네 갈래 중 하나로 가른다.

    1. 장바구니 낱말이나(카드가 보이면) 카드를 가리키는 말이 있으면 장바구니 신호
    2. 추천 낱말이 있으면 추천 신호
    3. 둘 다 있으면 ``both``, 하나만 있으면 그 갈래, 없으면 ``neither``
    """
    text = message.strip()
    cart = bool(_CART_WORDS.search(text)) or (
        cards_shown and bool(_POINTER.search(text))
    )
    recommend = bool(_REC_WORDS.search(text))
    if cart and recommend:
        return "both"
    if cart:
        return "cart"
    if recommend:
        return "recommend"
    return "neither"


def route(message: str, cards_shown: bool) -> Lane:
    """③으로 바로 보낼지(``recommend``), ⑦로 보낼지(``shop``)."""
    return "recommend" if classify(message, cards_shown) == "recommend" else "shop"
