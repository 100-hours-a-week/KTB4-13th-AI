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
    # 카드를 가리키는 말은 카드가 보일 때만 장바구니 신호다
    pytest.param("2번 같은 책 추천해줘", True, "both", id="번호와_추천_카드_있음"),
    pytest.param(
        "2번 같은 책 추천해줘", False, "recommend", id="번호와_추천_카드_없음"
    ),
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


def test_규칙_버전은_기준선과_같은_1이다() -> None:
    # 규칙을 바꾸면 버전을 올리고 이유를 PR 에 적는다. 이 시험이 깨지면 그 약속을 떠올리게 하는 자리다
    assert rules.RULES_VERSION == 1


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
