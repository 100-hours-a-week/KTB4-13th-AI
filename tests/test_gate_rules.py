"""문지기 규칙(app/gate/rules.py)과 쇼핑 칸 변환(app/gate/shopping.py) 시험(#413). LLM 은 부르지 않는다."""

import pytest

from app.gate import rules, shopping

# (말, 카드가 보이나, 기대하는 네 갈래)
KINDS = [
    pytest.param("비 오는 날 읽을 책 추천해줘", False, "recommend", id="추천"),
    pytest.param("파이썬 입문서 있을까?", False, "recommend", id="입문서"),
    pytest.param("1번 담아줘", True, "cart", id="담기"),
    pytest.param("장바구니 보여줘", False, "cart", id="장바구니"),
    pytest.param("이거 얼마야?", True, "cart", id="가격"),
    pytest.param("추천해 주고 담아줘", True, "both", id="둘_다"),
    pytest.param("안녕", False, "neither", id="아님"),
    pytest.param("네", True, "neither", id="짧은_동의"),
    # 카드를 가리키는 말은 카드가 보일 때만 장바구니 신호다. 추천 낱말이 같이 있으면 추천이다(v2, #530)
    pytest.param("2번 같은 책 추천해줘", True, "recommend", id="번호와_추천_카드_있음"),
    pytest.param(
        "2번 같은 책 추천해줘", False, "recommend", id="번호와_추천_카드_없음"
    ),
    pytest.param("2번 같은 거 더", True, "recommend", id="번호와_같은_거"),
    pytest.param("1번 쪽으로", True, "recommend", id="번호와_쪽으로"),
    pytest.param("2번 같은 거 담아줘", True, "both", id="번호와_추천과_담기"),
    pytest.param("2번 줄거리 알려줘", True, "cart", id="번호만"),
]


@pytest.mark.parametrize(("message", "cards_shown", "kind"), KINDS)
def test_말과_카드로_네_갈래를_가른다(message, cards_shown, kind) -> None:
    assert rules.classify(message, cards_shown) == kind


@pytest.mark.parametrize(("message", "cards_shown", "kind"), KINDS)
def test_추천만_추천으로_바로_보내고_나머지는_전부_쇼핑으로_보낸다(
    message, cards_shown, kind
) -> None:
    expected = "recommend" if kind == "recommend" else "shop"

    assert rules.route(message, cards_shown) == expected


def test_앞뒤_공백은_결과를_바꾸지_않는다() -> None:
    assert rules.classify("  1번 담아줘  ", True) == rules.classify("1번 담아줘", True)


def test_규칙_버전은_2다() -> None:
    # 규칙을 바꾸면 버전을 올리고 이유를 PR 에 적는다. 이 시험이 깨지면 그 약속을 떠올리게 하는 자리다.
    # 1 = 라우팅 기준선(#404), 2 = 카드를 가리키는 추천 말·③ 묻기의 답·카드 평가 말을 ③으로(#530)
    assert rules.RULES_VERSION == 2


# 직전 챗봇 말이 ③의 묻기(넓은 첫 요청에 원하는 것을 물은 말)면, 장바구니 낱말이 없는 답은 추천이다(#530)
AFTER_ASK = [
    pytest.param("비전공자인데 처음 배워 보려고", "recommend", id="추천_낱말_없는_답"),
    pytest.param("아무거나", "recommend", id="건너뛰기"),
    pytest.param("요즘 너무 지쳐서", "recommend", id="사정만_말함"),
    pytest.param("그냥 장바구니 보여줘", "cart", id="장바구니는_그대로"),
    pytest.param("얼마 정도 해?", "cart", id="가격은_그대로"),
]


@pytest.mark.parametrize(("message", "kind"), AFTER_ASK)
def test_묻기_뒤의_답은_장바구니_말이_없으면_추천이다(message, kind) -> None:
    assert rules.classify(message, False, after_ask=True) == kind
    # 묻기가 아니었으면 v1 처럼 신호 없는 말은 ⑦로 간다
    if kind == "recommend" and message != "아무거나":
        assert rules.classify(message, False) == "neither"


@pytest.mark.parametrize(
    ("message", "after_action", "kind"),
    [
        ("1번 책이 흥미로워 보여요!", False, "recommend"),
        ("2번이 마음에 들어", False, "recommend"),
        ("1번 책이 정말 따뜻해 보여요!", False, "recommend"),
        ("1번은 그림책이라서 좀 다를 것 같아.", False, "recommend"),
        ("1번 보여줘", False, "cart"),
        # ⑦이 "담을까요?"라고 물은 뒤의 관심은 그 답이다
        ("1번 책이 좋을 것 같아!", True, "cart"),
        # 관심 낱말이 없거나 장바구니 낱말이 있으면 그대로 ⑦
        ("1번 얼마야", False, "cart"),
        ("1번 좋아 보여 담아줘", False, "cart"),
    ],
)
def test_카드가_보일_때_책을_평가하는_말은_추천이다(
    message, after_action, kind
) -> None:
    assert rules.classify(message, True, after_action=after_action) == kind


@pytest.mark.parametrize(
    ("message", "lane", "sure"),
    [
        # 번호로 카드를 가리켜 평가하면 새로 찾지 않고 그 책 소개로 답한다(대답 턴). 찾으면 카드가 갈아 끼워진다
        ("1번 책이 흥미로워 보여요!", "card", True),
        ("2번이 마음에 들어", "card", True),
        ("1번 책이 정말 따뜻해 보여요!", "card", True),
        ("1번은 그림책이라서 좀 다를 것 같아.", "card", True),
        # 번호 없이 제목으로 말했거나 다른 책과 견주는 말은, 보인 책에 대한 말인지 조건을 바꾸는 말인지
        # 낱말로는 모른다. 애매하다 — 판정 호출이 가르고, 판정이 실패하면 ⑦로 간다
        ("『번 아웃 탈출기』가 정말 좋을 것 같아!", "shop", False),
        ("1번보다 쉬워 보이는 걸로", "shop", False),
        ("좀 더 밝아 보이는 걸로", "shop", False),
        # 평가에 새 책을 찾는 말이 같이 있으면 찾는다
        ("1번 같은 책이 좋아 보여요 더 찾아줘", "recommend", True),
    ],
)
def test_카드_평가_말은_대답_턴으로_보내고_낱말로_못_가르면_애매하다(
    message, lane, sure
) -> None:
    decision = rules.decide(message, True)

    assert (decision.lane, decision.sure) == (lane, sure)


# 카드가 보일 때 사겠다는 말은 책을 칭찬하는 낱말이 같이 있어도 ⑦이다. ③이 새로 찾으면 카드가 갈아 끼워져
# 담을 수 없다. 장바구니 낱말에 없던 말이라 칭찬 낱말에 먼저 걸렸다(#530 리뷰)
BUY_WITH_PRAISE = [
    "1번 담으면 될 것 같아",
    "2번 사면 될 것 같아",
    "1번 구매할게요 마음에 들어서",
    "2번으로 주세요 좋아 보여요",
    "이걸로 살게요 흥미로워 보여서",
    "1번 사고 싶어요 끌리네요",
    "이거 주세요 따뜻해 보여요",
]


@pytest.mark.parametrize("message", BUY_WITH_PRAISE)
def test_사겠다는_말은_책을_칭찬하는_낱말이_같이_있어도_쇼핑이다(message) -> None:
    decision = rules.decide(message, True)

    assert (decision.kind, decision.sure, decision.lane) == ("cart", True, "shop")
    assert rules.route(message, True) == "shop"


def test_사겠다는_낱말도_새_책을_찾는_말이_같이_있으면_찾는다() -> None:
    decision = rules.decide("2번 사면 될 것 같은데 비슷한 책도 추천해줘", True)

    assert (decision.lane, decision.sure) == ("recommend", True)


def test_카드가_없으면_사겠다는_낱말이_추천을_가로채지_않는다() -> None:
    assert rules.classify("구매할 책 추천해줘", False) == "recommend"


def test_알려_달라는_주세요는_사겠다는_말이_아니다() -> None:
    # "알려주세요"의 "주세요"를 사겠다는 말로 읽으면 카드 질문이 ⑦로 간다
    decision = rules.decide("1번 줄거리 알려주세요 좋아 보여요", True)

    assert decision.lane == "card"


class _T:
    def __init__(self, role: str, text: str) -> None:
        self.role, self.text = role, text


def test_직전_챗봇_말이_담기를_물었는지_본다() -> None:
    assert rules.asked_action([_T("assistant", "카드 1번 《제목》을 담을까요?")])
    # 담았다는 알림 뒤의 다른 물음은 담기를 묻지 않았다
    assert not rules.asked_action(
        [_T("assistant", "담았어요. 다른 도와드릴 일이 있으신가요?")]
    )


# --- 쇼핑 칸 ---


def test_멱등_키가_있는_요청만_문지기를_켠다() -> None:
    assert shopping.enabled({"idempotency_key": "k"}) is True
    assert shopping.enabled({}) is False
    assert shopping.enabled({"idempotency_key": None}) is False


@pytest.mark.parametrize(
    ("cards", "shown"),
    [
        ([{"isbn": "9788900000011", "rank": 1, "match_score": 90}], True),
        ([], False),
        (None, False),
        ("카드", False),
    ],
)
def test_카드가_한_장이라도_있어야_보인_것으로_센다(cards, shown) -> None:
    assert shopping.cards_shown({"context_cards": cards}) is shown


def test_챗봇_요청에서_쇼핑_에이전트의_칸만_뽑는다() -> None:
    payload = {
        "user_id": 1,
        "consented": True,
        "spec": {"intent": "semantic"},
        "message": "1번 담아줘",
        "idempotency_key": "k",
        "user_context": {"logged_in": True},
        "exclude_book_ids": [3],
    }

    assert shopping.agent_payload(payload) == {
        "user_id": 1,
        "message": "1번 담아줘",
        "idempotency_key": "k",
        "user_context": {"logged_in": True},
    }


def test_쇼핑_에이전트_요청_검사를_그대로_거친다() -> None:
    ok = {
        "user_id": 1,
        "message": "1번 담아줘",
        "idempotency_key": "k",
        "user_context": {"logged_in": True},
    }

    assert shopping.to_agent_request(ok) is not None
    assert shopping.to_agent_request({**ok, "user_context": None}) is None
    assert (
        shopping.to_agent_request({k: v for k, v in ok.items() if k != "user_context"})
        is None
    )


# --- 확실한 가름과 애매한 가름(#530) ---


@pytest.mark.parametrize(
    ("message", "cards_shown", "flags", "kind", "sure"),
    [
        pytest.param("2번 담아줘", True, {}, "cart", True, id="장바구니_낱말"),
        pytest.param(
            "파이썬 책 추천해줘", False, {}, "recommend", True, id="추천_낱말"
        ),
        pytest.param("2번 같은 거", True, {}, "recommend", True, id="카드와_추천_낱말"),
        pytest.param(
            "1번이 흥미로워 보여", True, {}, "recommend", True, id="카드_평가"
        ),
        pytest.param(
            "아무거나", False, {"after_ask": True}, "recommend", True, id="묻기의_답"
        ),
        pytest.param(
            "2번", True, {"after_action": True}, "cart", True, id="담기_질문_뒤_번호"
        ),
        pytest.param(
            "응", True, {"after_action": True}, "neither", True, id="담기_질문의_답"
        ),
        # 낱말 근거 없이 기본값으로 정한 말은 애매하다. 머지 전 점검에서 틀린 말들이다
        pytest.param("1만5천원 이하로", True, {}, "neither", False, id="조건만"),
        pytest.param("다른 거 보여줘", True, {}, "neither", False, id="다른_거"),
        pytest.param("2번은 어떤 내용이야?", True, {}, "cart", False, id="카드_번호만"),
        pytest.param("2번", True, {}, "cart", False, id="번호만"),
        pytest.param("안녕", False, {}, "neither", False, id="인사"),
    ],
)
def test_낱말_근거가_있으면_확실하고_기본값으로_정했으면_애매하다(
    message, cards_shown, flags, kind, sure
) -> None:
    decision = rules.decide(message, cards_shown, **flags)

    assert (decision.kind, decision.sure) == (kind, sure)
    # 갈래는 classify 와 같다. 낱말 규칙은 v2 그대로다
    assert rules.classify(message, cards_shown, **flags) == kind
