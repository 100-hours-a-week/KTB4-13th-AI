"""③ "1번 작가 다른 책"을 그 카드의 저자로 찾는다(#694).

11/11 dev 새 질문 QA에서 카드 1번이 김금희 『너무 한낮의 연애』일 때 "1번 작가 다른 책도 보여줘"가 3번 모두 김금희 책
1권 + 다른 작가 책 1권이었다. 1번을 기준 책으로 둔 함께 읽은 책 찾기가 되고, 같은 저자 한 장 규칙이 걸렸다.
LLM 과 DB 는 부르지 않는다.
"""

import pytest

from app.chat import lanes
from app.chat.schemas import Spec, SpecExact
from app.chat.shown import ShownBook
from app.core import editions
from app.routers import chat
from app.search.schemas import SearchFilters

_SHOWN = [
    ShownBook(1, 101, "너무 한낮의 연애", "김금희 지음", "소개"),
    ShownBook(2, 102, "바깥은 여름", "김애란", "소개"),
    ShownBook(3, 103, "쇼코의 미소", "최은영", "소개"),
]
_RANKS = [b.rank for b in _SHOWN]
_TITLES = {b.rank: b.title for b in _SHOWN}


def _spec(**overrides) -> Spec:
    base = {
        "intent": "semantic",
        "exact": SpecExact(title=None, author=None, publisher=None),
        "filters": SearchFilters(price_max=20000),
        "semantic": "잔잔한 한국 소설",
        "anchor_book": None,
        "exclude": [],
    }
    base.update(overrides)
    return Spec(**base)


@pytest.mark.parametrize(
    ("message", "rank"),
    [
        ("1번 작가 다른 책도 보여줘", 1),
        ("2번 저자 책 또 있어?", 2),
        ("세 번째 책 작가의 다른 작품", 3),
        ("너무 한낮의 연애 작가 책 더 보여줘", 1),
    ],
)
def test_카드를_짚고_그_저자의_다른_책을_찾는_말을_알아본다(
    message: str, rank: int
) -> None:
    assert lanes.asked_card_author(message, lanes.EMPTY_PLAN, _RANKS, _TITLES) == rank


def test_카드가_한_장이면_지시어로_짚어도_받는다() -> None:
    assert (
        lanes.asked_card_author("이 작가 책 더 있어?", lanes.EMPTY_PLAN, [1], _TITLES)
        == 1
    )


def test_카드가_여럿인데_어느_책인지_말하지_않으면_받지_않는다() -> None:
    assert (
        lanes.asked_card_author(
            "이 작가 책 더 있어?", lanes.EMPTY_PLAN, _RANKS, _TITLES
        )
        is None
    )


def test_메시지에_비춰_본_기준_책이_카드면_그_번호다() -> None:
    plan = lanes.Plan(anchor=2, relation="similar")

    assert lanes.asked_card_author("그 작가 책 더", plan, _RANKS, _TITLES) == 2


@pytest.mark.parametrize(
    "message",
    [
        "1번 작가 말고 다른 작가 책",
        "1번이랑 비슷한 책",
        "1번 읽은 사람들은 또 뭐 읽어?",
        "1번 작가가 누구야?",
    ],
)
def test_저자의_다른_책을_찾는_말이_아니면_받지_않는다(message: str) -> None:
    assert lanes.asked_card_author(message, lanes.EMPTY_PLAN, _RANKS, _TITLES) is None


def test_카드_저자로_찾는_spec_과_빈_계획이_된다() -> None:
    plan = lanes.Plan(anchor=1, relation="co")

    spec, filled = chat._author_of_card(
        _spec(), plan, "1번 작가 다른 책도 보여줘", _SHOWN
    )

    assert filled == lanes.EMPTY_PLAN
    assert spec.intent == "exact"
    assert spec.exact.author == "김금희"
    assert spec.exact.title is None
    assert spec.filters.price_max == 20000  # 앞 조건은 그대로다
    assert chat._query_text(spec) == "김금희"
    assert not chat._uses_lanes(spec, filled)  # 저자를 콕 집은 요청과 같은 길이다


def test_저자를_모르는_카드면_그대로_둔다() -> None:
    books = [ShownBook(1, 101, "작자 미상 책", None, "소개")]
    plan = lanes.Plan(anchor=1)

    spec, filled = chat._author_of_card(_spec(), plan, "1번 작가 다른 책", books)

    assert filled == plan
    assert spec.intent == "semantic"


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ("김금희 지음", "김금희"),
        ("지은이: 한강", "한강"),
        ("J.K. 롤링 지음 ;강동혁 옮김", "J.K. 롤링"),
        ("프랭크 허버트, 김승욱", "프랭크 허버트"),
        (None, ""),
    ],
)
def test_첫_저자_이름은_역할_말만_떼고_표기는_둔다(
    author: str | None, expected: str
) -> None:
    assert editions.first_author(author) == expected
