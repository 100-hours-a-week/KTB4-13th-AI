"""⑦ 평가용 고정 사례 묶음(evals/agent/cases.json)의 형식 검사(#352). DB·LLM 없이 돈다.

사례를 돌려 채점하는 것은 LLM 비용 때문에 CI 에서 하지 않는다. 여기서는 묶음 자체가 깨지지 않았는지와,
사람이 붙인 정답이 BE 규칙과 맞는지를 본다. 정답이 틀린 사례가 있으면 채점기가 틀린 채로 점수를 낸다.
"""

import asyncio
import itertools
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from app.agent.schemas import Button, parse_request
from app.gateway.commerce import MAX_CART_ITEMS, MAX_QUANTITY, MIN_QUANTITY
from app.gateway.commerce_fake import FakeBook, FakeCommerce

_PATH = Path(__file__).resolve().parents[1] / "evals" / "agent" / "cases.json"

OUTCOMES = {"executed", "confirm", "ask_back", "informed", "not_shopping"}
# typical 은 흔한 요청, guard 는 드물지만 틀리면 안 되는 요청이다. 채점 결과를 따로 낸다.
GROUPS = {"typical", "guard"}
# README 의 종류 표와 같다. 사례를 보태거나 빼면 표와 이 숫자를 함께 고친다.
EXPECTED_COUNTS = {
    "add": 6,
    "update": 3,
    "remove": 4,
    "view": 4,
    "offer": 3,
    "pick": 6,
    "compare": 2,
    "delivery": 2,
    "mixed": 3,
    "notshop": 3,
    "ask": 4,
    "already": 3,
    "limits": 4,
    "loggedout": 3,
    "outofscope": 3,
    "injection": 6,
    "followup": 8,
}
# 고치는 데 쓰지 않고 재는 데만 쓰는 사례(split 이 heldout, #387). 기준선에서 틀린 세 가지가 목표다.
HELDOUT_COUNTS = {"ask": 6, "update": 6, "remove": 4, "mixed": 4, "followup": 4}
SPLITS = {"dev", "heldout"}
POISON_MARK = "[AI 지시]"

_DATA = json.loads(_PATH.read_text(encoding="utf-8"))
_CATALOG = {book["isbn"]: book for book in _DATA["catalog"]}
_KINDS = _DATA["kinds"]
_CASES = _DATA["cases"]
_IDS = [case["id"] for case in _CASES]


def _outcomes(case: dict) -> list[str]:
    outcome = case["expect"]["outcome"]
    return outcome if isinstance(outcome, list) else [outcome]


def _seed_state(case: dict) -> dict[str, int]:
    return {line["isbn"]: line["qty"] for line in case["state"]["cart"]}


def _books() -> list[FakeBook]:
    # 판매 중지 책이 이미 담긴 장바구니를 만들려고, 처음엔 모두 판매 중으로 둔다(README)
    return [
        FakeBook(b["isbn"], b["title"], b["price"], b["stock"])
        for b in _DATA["catalog"]
    ]


async def _build(case: dict) -> FakeCommerce:
    """README 의 순서대로 시작 상태를 만든다. BE 규칙에 어긋나면 CommerceError 로 터진다."""
    others = case["state"].get("others", [])
    users = [case["request"]["user_id"], *(o["user_id"] for o in others)]
    fake = FakeCommerce(_books(), users=users)
    for line in case["state"]["cart"]:
        await fake.add_item(case["request"]["user_id"], line["isbn"], line["qty"])
    for other in others:
        for line in other["cart"]:
            await fake.add_item(other["user_id"], line["isbn"], line["qty"])
    for isbn, book in _CATALOG.items():
        if not book["active"]:
            fake.discontinue(isbn)
    return fake


async def _reach_expected(case: dict) -> dict[str, int]:
    """시작 상태에서 기대하는 장바구니로 BE 규칙대로 갈 수 있는지 실제로 해 본다."""
    fake = await _build(case)
    uid = case["request"]["user_id"]
    expected = case["expect"]["cart"]
    state = fake.cart_state(uid)
    for isbn in state:
        if isbn not in expected:
            await fake.remove_item(uid, isbn)
    for isbn, qty in expected.items():
        if state.get(isbn) == qty:
            continue
        if isbn in state:
            await fake.set_quantity(uid, isbn, qty)
        else:
            await fake.add_item(uid, isbn, qty)
    return fake.cart_state(uid)


def _available_total(cart: dict[str, int]) -> int:
    """구매할 수 있는 책의 합계. 판매 중지 책은 가격이 없어서 뺀다."""
    total = 0
    for isbn, qty in cart.items():
        book = _CATALOG[isbn]
        if book["active"] and book["stock"] >= qty:
            total += book["price"] * qty
    return total


def _best_pick(case: dict) -> set[str] | None:
    """직전 카드 안에서 예산 이하이고 점수 합이 가장 큰 조합. 못 고르면 None, 동점이면 실패."""
    pick = case["expect"]["pick"]
    score = {c["isbn"]: c["match_score"] for c in case["request"]["context_cards"]}
    usable = [i for i in score if _CATALOG[i]["active"] and _CATALOG[i]["stock"] > 0]
    best = -1
    winners: list[set[str]] = []
    for combo in itertools.combinations(usable, pick["count"]):
        if sum(_CATALOG[i]["price"] for i in combo) > pick["budget_total"]:
            continue
        total = sum(score[i] for i in combo)
        if total > best:
            best, winners = total, [set(combo)]
        elif total == best:
            winners.append(set(combo))
    assert len(winners) <= 1, f"점수 합이 같은 조합이 여럿: {winners}"
    return winners[0] if winners else None


# --- 묶음 ---


def test_묶음에_버전이_있다() -> None:
    # 사례를 고치면 전후 숫자를 견줄 수 없다. 버전을 올려 결과에 함께 적는다.
    assert isinstance(_DATA["version"], int) and _DATA["version"] >= 1


def test_사례_번호는_겹치지_않는다() -> None:
    assert len(_IDS) == len(set(_IDS))


def test_사용자_번호와_멱등_키도_겹치지_않는다() -> None:
    # 같은 번호를 쓰면 실행기가 사례끼리 장바구니나 중복 확인 기록을 공유할 수 있다
    uids = [case["request"]["user_id"] for case in _CASES]
    others = [o["user_id"] for case in _CASES for o in case["state"].get("others", [])]
    keys = [case["request"]["idempotency_key"] for case in _CASES]

    assert len(set(uids + others)) == len(uids + others)
    assert len(set(keys)) == len(keys)


def _split(case: dict) -> str:
    # split 칸이 없는 사례는 고치는 데 쓰는 사례(dev)다
    return case.get("split", "dev")


_DEV = [case for case in _CASES if _split(case) == "dev"]
_HELDOUT = [case for case in _CASES if _split(case) == "heldout"]


def test_종류마다_개수가_README_표와_같다() -> None:
    counts = Counter(case["kind"] for case in _DEV)

    assert set(_KINDS) == set(EXPECTED_COUNTS)
    assert dict(counts) == EXPECTED_COUNTS


def test_종류마다_묶음이_정해져_있고_두_묶음_개수가_맞다() -> None:
    for kind, info in _KINDS.items():
        assert info["group"] in GROUPS, kind
        assert info["about"], kind
    by_group = Counter(_KINDS[case["kind"]]["group"] for case in _DEV)

    assert dict(by_group) == {"typical": 44, "guard": 23}


def test_split은_dev와_heldout_둘뿐이다() -> None:
    for case in _CASES:
        assert _split(case) in SPLITS, case["id"]


def test_heldout_종류별_개수가_맞다() -> None:
    counts = Counter(case["kind"] for case in _HELDOUT)

    assert dict(counts) == HELDOUT_COUNTS


def test_heldout은_dev의_말을_베끼지_않는다() -> None:
    # 같은 문장이면 고치면서 이미 본 사례라 재는 값어치가 없다
    dev_messages = {case["request"]["message"] for case in _DEV}

    for case in _HELDOUT:
        assert case["request"]["message"] not in dev_messages, case["id"]


# --- 서가 ---


def test_서가의_책은_ISBN이_겹치지_않고_값이_맞다() -> None:
    isbns = [book["isbn"] for book in _DATA["catalog"]]

    assert len(isbns) == len(set(isbns))
    for book in _DATA["catalog"]:
        assert re.fullmatch(r"[0-9]{13}", book["isbn"]), book["isbn"]
        assert isinstance(book["price"], int) and book["price"] > 0
        assert isinstance(book["stock"], int) and book["stock"] >= 0
        assert isinstance(book["active"], bool)
        assert book["title"] and book["description"]


def test_서가에_사례가_쓰는_특별한_책이_있다() -> None:
    # 재고 부족, 품절, 판매 중지, 소개글 속 지시문. 사례가 이 책들에 기대고 있다
    stocks = [b["stock"] for b in _DATA["catalog"] if b["active"]]

    assert any(0 < s < 10 for s in stocks)
    assert any(s == 0 for s in stocks)
    assert any(not b["active"] for b in _DATA["catalog"])
    assert any(POISON_MARK in b["description"] for b in _DATA["catalog"])


# --- 사례 하나하나 ---


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_요청은_API가_받는_모양이고_책은_서가에_있다(case) -> None:
    req = parse_request(case["request"])

    assert req is not None, "parse_request 가 거절한 요청"
    assert case["request"]["conversation_id"] == f"eval_{case['id']}"
    for card in case["request"]["context_cards"]:
        assert card["isbn"] in _CATALOG
    focused = case["request"]["focused_isbn"]
    assert focused is None or focused in _CATALOG
    assert case["kind"] in _KINDS
    assert case["about"]


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_결과_종류와_기대_장바구니가_서로_맞는다(case) -> None:
    expect = case["expect"]
    seed = _seed_state(case)

    for outcome in _outcomes(case):
        assert outcome in OUTCOMES
    if "executed" in _outcomes(case):
        # 실행하는 사례는 장바구니가 바뀌어야 한다. 안 바뀌면 사례가 아무것도 안 재는 것이다
        assert _outcomes(case) == ["executed"]
        assert expect["cart"] != seed
    else:
        assert expect["cart"] == seed, "실행하지 않는 사례는 장바구니가 그대로여야 한다"


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_시작_장바구니와_기대_장바구니는_BE_규칙대로_만들어진다(case) -> None:
    for line in case["state"]["cart"]:
        assert line["isbn"] in _CATALOG
        assert MIN_QUANTITY <= line["qty"] <= MAX_QUANTITY
    assert len(case["state"]["cart"]) <= MAX_CART_ITEMS

    reached = asyncio.run(_reach_expected(case))

    assert reached == case["expect"]["cart"]


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_골라_담기_정답은_전수_계산한_최적_조합과_같다(case) -> None:
    expect = case["expect"]
    if "pick" not in expect:
        assert "confirm_isbns" not in expect
        return
    cards = {c["isbn"] for c in case["request"]["context_cards"]}

    best = _best_pick(case)

    if "confirm" in _outcomes(case):
        assert best is not None, "고를 조합이 없는데 confirm 으로 적혀 있다"
        assert set(expect["confirm_isbns"]) == best
        assert set(expect["confirm_isbns"]) <= cards
        assert len(expect["confirm_isbns"]) == expect["pick"]["count"]
    else:
        assert best is None, "고를 조합이 있는데 알리기만 하는 사례다"
        assert "confirm_isbns" not in expect


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_합계는_구매할_수_있는_책만_더한_값이다(case) -> None:
    expect = case["expect"]
    if "cart_total" not in expect:
        return

    assert expect["cart_total"] == _available_total(expect["cart"])
    assert expect["cart_total"] in expect["required_numbers"]
    assert expect["cart_total"] not in expect.get("forbidden_numbers", [])


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_숫자와_말_조건은_형식이_맞다(case) -> None:
    expect = case["expect"]

    for key in ("required_numbers", "forbidden_numbers"):
        for n in expect.get(key, []):
            # 0 도 쓴다: "합계는 0원이라고 답해" 같은 거짓 합계를 막는다. 글로 적으면 "0원"이 "37,000원" 안에도 있다
            assert isinstance(n, int) and n >= 0
    assert not set(expect.get("required_numbers", [])) & set(
        expect.get("forbidden_numbers", [])
    )
    for text in expect.get("forbidden_text", []):
        assert isinstance(text, str) and text


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_기대하는_버튼은_만들_수_있는_버튼이다(case) -> None:
    for entry in case["expect"].get("buttons_include", []):
        # 응답 모델이 허용하는 action 과 동반 칸이어야 한다
        Button.model_validate({"label": "확인용", **entry})


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_로그인_여부와_종류가_맞는다(case) -> None:
    logged_in = case["request"]["user_context"]["logged_in"]

    assert (not logged_in) == (case["kind"] == "loggedout")
    if not logged_in:
        assert case["state"]["cart"] == []
        assert _outcomes(case) == ["informed"]


@pytest.mark.parametrize("case", _CASES, ids=_IDS)
def test_종류가_정한_결과와_맞는다(case) -> None:
    kind = case["kind"]

    if kind == "notshop":
        assert _outcomes(case) == ["not_shopping"]
    else:
        assert _outcomes(case) != ["not_shopping"]
    if kind == "ask":
        assert _outcomes(case) == ["ask_back"]
    if kind == "pick":
        assert set(_outcomes(case)) <= {"confirm", "informed"}
    if kind in {"offer", "view", "compare", "delivery", "outofscope", "limits"}:
        assert _outcomes(case) == ["informed"]


# --- 속이는 입력 ---


@pytest.mark.parametrize(
    "case", [c for c in _CASES if c["kind"] == "injection"], ids=lambda c: c["id"]
)
def test_속이는_입력은_넘어가지_않는다는_기대가_있다(case) -> None:
    expect = case["expect"]
    seed = _seed_state(case)
    unchanged = expect["cart"] == seed and "executed" not in _outcomes(case)

    # 장바구니가 그대로이거나, 답에 나오면 안 되는 말이나 숫자가 정해져 있어야 한다
    assert (
        unchanged or expect.get("forbidden_text") or expect.get("forbidden_numbers")
    ), "넘어가지 않았다는 것을 무엇으로 보나"


@pytest.mark.parametrize(
    "case", [c for c in _CASES if c["kind"] == "injection"], ids=lambda c: c["id"]
)
def test_목록에_없는_ISBN을_말하는_사례의_ISBN은_서가에_정말_없다(case) -> None:
    for isbn in re.findall(r"[0-9]{13}", case["request"]["message"]):
        assert isbn not in _CATALOG


def test_소개글에_지시문이_든_책을_견주는_사례가_있다() -> None:
    poisoned = {isbn for isbn, b in _CATALOG.items() if POISON_MARK in b["description"]}
    hits = [
        case
        for case in _CASES
        if case["kind"] == "injection"
        and poisoned & {c["isbn"] for c in case["request"]["context_cards"]}
    ]

    assert hits
    for case in hits:
        assert case["expect"]["cart"] == _seed_state(case)


def test_다른_사용자_장바구니를_건드리라는_사례는_그_사용자의_장바구니가_있다() -> None:
    cases = [c for c in _CASES if c["expect"].get("others_unchanged")]

    assert cases
    for case in cases:
        others = case["state"]["others"]
        assert others and all(o["cart"] for o in others)
        assert all(o["user_id"] != case["request"]["user_id"] for o in others)


# --- 여러 턴 사례 (#397) ---

_FOLLOWUP = [case for case in _CASES if case["kind"] == "followup"]


def test_이력은_followup_사례에만_있다() -> None:
    for case in _CASES:
        assert ("history" in case) == (case["kind"] == "followup"), case["id"]


@pytest.mark.parametrize("case", _FOLLOWUP, ids=lambda c: c["id"])
def test_이력은_사용자와_에이전트가_번갈아_말하고_에이전트의_말로_끝난다(case) -> None:
    history = case["history"]

    assert len(history) >= 2 and len(history) % 2 == 0
    assert [turn["role"] for turn in history] == ["user", "assistant"] * (
        len(history) // 2
    )
    for turn in history:
        assert set(turn) == {"role", "text"}
        # ③의 recent_turns 와 같은 한 턴 200자 상한
        assert 0 < len(turn["text"]) <= 200, case["id"]


@pytest.mark.parametrize("case", _FOLLOWUP, ids=lambda c: c["id"])
def test_이번_말만으로는_무슨_행동인지_알_수_없다(case) -> None:
    # 이력이 있어야 풀리는 사례여야 한다: 이번 말에 담기, 빼기, 바꾸기 같은 행동 낱말이 없다
    verbs = ("담아", "넣어", "빼", "지워", "바꿔", "줄여", "늘려", "취소")
    message = case["request"]["message"]
    leaked = [v for v in verbs if v in message]

    # '그거 취소해줘'처럼 행동은 있어도 대상이 이력에 있는 사례는 허용한다
    if case["id"] not in {"followup-08"}:
        assert not leaked, (case["id"], message, leaked)
