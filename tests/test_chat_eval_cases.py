"""③ 평가용 고정 질문 묶음(evals/chat/cases.json)의 형식 검사(#322). DB·LLM 없이 돈다.

묶음을 돌려 채점하는 것은 LLM 비용 때문에 CI 에서 하지 않는다. 여기서는 묶음 자체가 깨지지 않았는지만 본다.
칸 이름을 잘못 적으면 채점기(evals/chat/grade.py)가 그 검사를 조용히 건너뛰어 통과로 센다.
"""

from collections import Counter

import pytest

from app.chat.schemas import MAX_MESSAGE_CHARS
from app.routers.chat import parse_request
from evals.chat.grade import load_cases
from evals.chat.run import INITIAL_SPEC

ACTIONS = {"recommend", "clarify", "recommend_and_ask", "observe"}
EXPECT_KEYS = {"action", "intent", "cards", "reply", "spec", "semantic", "ungraded"}
CARD_KEYS = {
    "any_isbn",
    "title_all",
    "title_any",
    "author_all",
    "publisher_all",
    "title_none",
    "title_none_exact",
    "author_none",
    "distinct_titles",
    "max_cards",
    "price_max",
    "year_from",
    "not_shown_before",
    "only_shown_before",
    "reason_long_needs_description",
}
# 실제 글이나 기록을 본뜬 질문. 무엇을 본떴는지(evidence) 적어 두어야 나중에 확인할 수 있다.
SOURCES_NEEDING_EVIDENCE = {"community", "chatlog", "study"}
# 채점기가 턴별로 볼 수 있는 spec 의 칸
SPEC_PATHS = {
    "intent",
    "semantic",
    "filters.category",
    "filters.price_min",
    "filters.price_max",
    "filters.pub_year_from",
    "filters.pub_year_to",
}

_DATA = load_cases()
_KINDS = _DATA["kinds"]
_CASES = _DATA["cases"]
_IDS = [f"#{case['no']}" for case in _CASES]


def test_묶음에_버전이_있다() -> None:
    # 질문이나 기대 동작을 고치면 전후 숫자를 견줄 수 없다. 버전을 올려 결과에 함께 적는다.
    assert isinstance(_DATA["version"], int) and _DATA["version"] >= 1


def test_번호는_1부터_빠짐없이_이어진다() -> None:
    # 이슈와 PR 이 질문을 번호로 가리킨다. 번호가 밀리면 옛 기록과 어긋난다.
    assert [case["no"] for case in _CASES] == list(range(1, len(_CASES) + 1))


def test_종류마다_설명이_있고_질문이_둘_이상이다() -> None:
    counts = Counter(case["kind"] for case in _CASES)

    assert set(counts) == set(_KINDS)
    assert min(counts.values()) >= 2
    for kind in _KINDS.values():
        assert kind["about"].strip()


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_질문마다_출처가_있고_본뜬_질문은_무엇을_본떴는지_적는다(case: dict) -> None:
    assert case["source"] in _DATA["sources"]
    if case["source"] in SOURCES_NEEDING_EVIDENCE:
        assert case.get("evidence", "").strip()


def test_출처마다_설명이_있다() -> None:
    for source in _DATA["sources"].values():
        assert source["about"].strip()


def test_되물어야_하는_질문이_넉넉하다() -> None:
    # 되묻기 쪽이 얇으면 "무엇이든 추천하는" 프롬프트가 높은 점수를 받는다.
    clarify = [case for case in _CASES if case["expect"]["action"] == "clarify"]

    assert len(clarify) >= 3


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_질문은_API가_받는_요청이_된다(case: dict) -> None:
    assert case["about"].strip()
    assert case["turns"]
    for message in case["turns"]:
        assert message.strip() and len(message) <= MAX_MESSAGE_CHARS
        request = parse_request(
            {
                "user_id": 1,
                "consented": False,
                "spec": INITIAL_SPEC,
                "message": message,
                "recent_turns": [],
                "exclude_book_ids": [],
            }
        )
        assert not isinstance(request, tuple)


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_기대_동작은_채점기가_아는_칸만_쓴다(case: dict) -> None:
    expect = case["expect"]

    assert set(expect) <= EXPECT_KEYS
    assert expect["action"] in ACTIONS
    assert expect.get("intent") in (None, "exact", "semantic")
    assert set(expect.get("cards", {})) <= CARD_KEYS
    assert set(expect.get("reply", {})) <= {"has", "lacks"}
    assert set(expect.get("semantic", {})) <= {"changes_at", "has", "lacks"}
    for note in expect.get("ungraded", []):
        assert isinstance(note, str) and note.strip()


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_턴을_가리키는_검사는_있는_턴을_가리킨다(case: dict) -> None:
    expect = case["expect"]
    turns = len(case["turns"])

    for rule in expect.get("spec", []):
        assert set(rule) == {"turn", "path", "equals"}
        assert 1 <= rule["turn"] <= turns
        assert rule["path"] in SPEC_PATHS
    changes_at = expect.get("semantic", {}).get("changes_at")
    if changes_at is not None:
        # 앞 턴과 견주므로 둘째 턴부터다.
        assert 2 <= changes_at <= turns
    cards = expect.get("cards", {})
    if cards.get("not_shown_before") or cards.get("only_shown_before"):
        assert turns >= 2


def test_기록만_하는_질문은_검사_대신_무엇을_볼지만_적는다() -> None:
    # 통과율에서 빠지는 질문이다. 검사를 적어 두면 채점한 것처럼 보인다.
    for case in _CASES:
        expect = case["expect"]
        if expect["action"] == "observe":
            assert set(expect) == {"action", "ungraded"}, case["no"]


def test_되물어야_하는_질문에는_카드_검사를_두지_않는다() -> None:
    # 되물으면 카드가 없다. 카드 검사를 적어 두면 늘 통과해 채점한 것처럼 보인다.
    for case in _CASES:
        if case["expect"]["action"] == "clarify":
            assert "cards" not in case["expect"], case["no"]
