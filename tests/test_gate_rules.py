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
    # 1 = 라우팅 기준선(#404), 2 = 카드를 가리키는 추천 말·갈래 질문의 답을 ③으로(#530)
    assert rules.RULES_VERSION == 2


# 직전 챗봇 말이 갈래 질문이면, 장바구니 낱말이 없는 답은 추천이다(#530)
AFTER_BRANCH = [
    pytest.param("2번", "recommend", id="번호만"),
    pytest.param("두 번째", "recommend", id="몇_번째"),
    pytest.param("담담한 거", "recommend", id="신호_없는_말"),
    pytest.param("2번 담아줘", "cart", id="담기는_그대로"),
    pytest.param("2번이 좋은데 얼마야?", "cart", id="가격은_그대로"),
]


@pytest.mark.parametrize(("message", "kind"), AFTER_BRANCH)
def test_갈래_질문_뒤의_답은_장바구니_말이_없으면_추천이다(message, kind) -> None:
    assert rules.classify(message, True, after_branch=True) == kind


class _T:
    def __init__(self, role: str, text: str) -> None:
        self.role, self.text = role, text


@pytest.mark.parametrize(
    ("turns", "asked"),
    [
        (
            [
                _T("user", "위로되는 책"),
                _T("assistant", "1번처럼 시, 2번처럼 에세이 중 어느 쪽이 끌리세요?"),
            ],
            True,
        ),
        ([_T("assistant", "어떤 쪽이 좋으세요?")], True),
        (
            [
                _T("assistant", "어느 쪽이 끌리세요?"),
                _T("user", "2번"),
                _T("assistant", "에세이로 골랐어요."),
            ],
            False,
        ),
        ([_T("assistant", "어느 쪽이 끌리세요?"), _T("user", "2번")], False),
        ([], False),
    ],
)
def test_직전_챗봇_말이_갈래_질문인지_본다(turns, asked) -> None:
    assert rules.asked_branch(turns) is asked


@pytest.mark.parametrize(
    ("message", "after_action", "kind"),
    [
        ("1번 책이 흥미로워 보여요!", False, "recommend"),
        ("2번이 마음에 들어", False, "recommend"),
        ("1번 책이 정말 따뜻해 보여요!", False, "recommend"),
        ("1번은 그림책이라서 좀 다를 것 같아.", False, "recommend"),
        # 번호 없이 제목으로 말해도 같다
        ("『번 아웃 탈출기』가 정말 좋을 것 같아!", False, "recommend"),
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
