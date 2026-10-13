"""⑦ 사용자의 말이 LLM 이 고른 책의 근거인지 보는 규칙(app/agent/evidence.py) 테스트(#387). LLM 도 BE 도 쓰지 않는다.

여기서 고정하는 것은 두 가지다. 정확히 말한 요청이 잘못 막히지 않는 것(번호, 제목, 전체, 보고 있는 책)과,
누구인지 알 수 없는 말은 어떤 짐작으로도 근거를 얻지 못하는 것. 평가 사례 79개의 정답 행동으로도 확인한다.
"""

import json
from pathlib import Path

import pytest

from app.agent import evidence
from app.agent.errors import RefError, ToolErrorCode
from app.agent.reference import BookRef, TurnContext
from app.agent.schemas import ContextCard
from app.gateway.commerce import CartLine

PY, DB, K8S, ALGO, SW = (
    "9788900001001",
    "9788900001018",
    "9788900001025",
    "9788900001032",
    "9788900001049",
)
TITLES = {
    PY: "처음 만나는 파이썬",
    DB: "데이터베이스 설계 노트",
    K8S: "쿠버네티스 입문 가이드",
    ALGO: "알고리즘 문제 해결 전략",
    SW: "소프트웨어 설계의 기본기",
}


def _line(isbn: str) -> CartLine:
    return CartLine(
        isbn=isbn, title=TITLES[isbn], price=1000, quantity=1, available=True
    )


def _ctx(
    message: str | None,
    *,
    cart: list[str] = (),
    cards: list[str] = (),
    know_titles: bool = True,
    focused: str | None = None,
) -> TurnContext:
    """cart 는 BE 조회 순서(최근에 담은 책이 1번)로 준다. cards 는 rank 1부터."""
    return TurnContext(
        user_id=1,
        logged_in=True,
        cards=[
            ContextCard(isbn=i, rank=r, match_score=80) for r, i in enumerate(cards, 1)
        ],
        focused_isbn=focused,
        allow_tools=None,
        cart_snapshot=[_line(i) for i in cart],
        message=message,
        titles={i: TITLES[i] for i in cards} if know_titles else {},
    )


def _card(n: int) -> BookRef:
    return BookRef(source="card", n=n)


def _cart(n: int) -> BookRef:
    return BookRef(source="cart", n=n)


# --- 말에서 순서 읽기 ---


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("1번 담아줘", {1}),
        ("3번 두 권 담아줘", {3}),
        ("두 번째 책 넣어줘", {2}),
        ("첫 번째 거 빼줘", {1}),
        ("첫째 담아줘", {1}),
        ("2번째 거", {2}),
        ("세 번째랑 5번 빼줘", {3, 5}),
        ("장바구니 두 번째 걸 1권으로 줄여줘", {2}),
        ("열 번째 책", {10}),
        # 수량과 횟수는 순서가 아니다
        ("3권 담아줘", set()),
        ("두 권 담아줘", set()),
        ("두 번 담아줘", set()),
        ("5권으로 해줘", set()),
        ("그거 담아줘", set()),
    ],
)
def test_말에_나온_순서를_읽는다(message: str, expected: set[int]) -> None:
    assert evidence.ordinals(message) == expected


def test_제목_낱말은_한_글자와_위치를_말하는_낱말을_뺀다() -> None:
    assert evidence.title_tokens("처음 만나는 파이썬") == {"만나는", "파이썬"}
    assert evidence.title_tokens("면접 대비 CS 요약") == {"면접", "대비", "cs", "요약"}
    assert evidence.title_tokens("개발자의 글쓰기") == {"개발자의", "글쓰기"}


# --- 근거가 있는 말 ---


def test_검사하지_않는_경우() -> None:
    ref = _card(1)
    # 말을 모르면(테스트나 옛 호출) 검사하지 않는다
    assert evidence.has_evidence(_ctx(None, cards=[PY, DB]), ref, PY)
    # 지금 보고 있는 책은 화면이 지목한 것이다
    assert evidence.has_evidence(
        _ctx("그거 빼줘", cards=[PY, DB], focused=SW), BookRef(source="focused"), SW
    )


def test_가리킬_책이_하나뿐이면_짐작이_아니다() -> None:
    assert evidence.has_evidence(_ctx("그거 담아줘", cards=[PY]), _card(1), PY)
    assert evidence.has_evidence(_ctx("그거 빼줘", cart=[PY]), _cart(1), PY)
    # 같은 책이 카드에 두 번 있어도 한 권이다
    ctx = _ctx("그거 담아줘", cards=[PY, PY])
    assert evidence.has_evidence(ctx, _card(1), PY)


def test_번호가_그_책의_번호와_같아야_한다() -> None:
    ctx = _ctx("2번 담아줘", cards=[PY, DB, K8S])

    assert evidence.has_evidence(ctx, _card(2), DB)
    # 2번이라고 했는데 1번을 골랐다
    assert not evidence.has_evidence(ctx, _card(1), PY)
    assert not evidence.has_evidence(ctx, _card(3), K8S)


def test_장바구니_번호도_같은_규칙이다() -> None:
    ctx = _ctx("장바구니 두 번째 빼줘", cart=[SW, DB, PY])

    assert evidence.has_evidence(ctx, _cart(2), DB)
    assert not evidence.has_evidence(ctx, _cart(1), SW)


def test_맨_위는_1번_맨_아래는_마지막_번호다() -> None:
    cart = [SW, DB, PY]  # 최근에 담은 SW 가 맨 위

    top = _ctx("장바구니 맨 위 거 빼줘", cart=cart)
    assert evidence.has_evidence(top, _cart(1), SW)
    assert not evidence.has_evidence(top, _cart(2), DB)

    bottom = _ctx("맨 아래 거 빼줘", cart=cart)
    assert evidence.has_evidence(bottom, _cart(3), PY)
    assert not evidence.has_evidence(bottom, _cart(1), SW)


@pytest.mark.parametrize("message", ["전부 빼줘", "모두 담아줘", "모든 책 빼줘"])
def test_전체를_뜻하는_낱말이면_어느_번호든_근거가_있다(message: str) -> None:
    ctx = _ctx(message, cart=[SW, DB, PY], cards=[PY, DB])

    assert evidence.has_evidence(ctx, _cart(2), DB)
    assert evidence.has_evidence(ctx, _card(1), PY)


def test_수를_말하지_않은_다_빼줘는_전체다() -> None:
    ctx = _ctx("다 빼줘", cart=[SW, DB, PY])

    assert evidence.has_evidence(ctx, _cart(2), DB)


def test_둘_다는_목록이_두_권일_때만_전체다() -> None:
    two = _ctx("둘 다 빼줘", cart=[DB, PY])
    three = _ctx("둘 다 빼줘", cart=[SW, DB, PY])

    assert evidence.has_evidence(two, _cart(1), DB)
    # 셋 중 둘이 누구인지는 모른다
    assert not evidence.has_evidence(three, _cart(1), SW)


def test_수를_말했는데_목록보다_적으면_일부라서_전체가_아니다() -> None:
    ctx = _ctx("그 두 권 다 빼줘", cart=[SW, DB, PY])

    for n, isbn in enumerate([SW, DB, PY], 1):
        assert not evidence.has_evidence(ctx, _cart(n), isbn)
    # 목록이 정확히 두 권이면 전체다
    assert evidence.has_evidence(_ctx("두 권 다 빼줘", cart=[DB, PY]), _cart(1), DB)
    assert evidence.has_evidence(_ctx("2권 다 빼줘", cart=[DB, PY]), _cart(2), PY)


def test_제목_낱말이_그_책만_가리키면_근거다() -> None:
    ctx = _ctx("파이썬 책 빼줘", cart=[DB, PY, K8S])

    assert evidence.has_evidence(ctx, _cart(2), PY)
    # 파이썬이라고 했는데 다른 책을 골랐다
    assert not evidence.has_evidence(ctx, _cart(1), DB)
    assert not evidence.has_evidence(ctx, _cart(3), K8S)


def test_띄어쓰기와_대소문자가_달라도_제목을_알아본다() -> None:
    cart = [DB, PY]
    assert evidence.has_evidence(_ctx("처음만나는파이썬 빼줘", cart=cart), _cart(2), PY)
    assert evidence.has_evidence(
        _ctx("데이터베이스설계노트 빼줘", cart=cart), _cart(1), DB
    )


def test_카드_책은_제목을_알아야_제목으로_말한_근거가_된다() -> None:
    message = "쿠버네티스 입문 가이드 담아줘"

    known = _ctx(message, cards=[PY, DB, K8S])
    assert evidence.has_evidence(known, _card(3), K8S)
    assert not evidence.has_evidence(known, _card(1), PY)

    # book.offer 로 제목을 확인하기 전에는 카드 책이 누구인지 말과 맞춰 볼 수 없다
    unknown = _ctx(message, cards=[PY, DB, K8S], know_titles=False)
    assert not evidence.has_evidence(unknown, _card(3), K8S)


def test_두_제목에_다_있는_낱말은_한_권을_가리키지_못한다() -> None:
    # "설계"는 데이터베이스 설계 노트와 소프트웨어 설계의 기본기에 다 들어 있다
    ctx = _ctx("설계 책 빼줘", cart=[SW, DB, PY])

    assert not evidence.has_evidence(ctx, _cart(1), SW)
    assert not evidence.has_evidence(ctx, _cart(2), DB)


def test_한_번에_여러_책을_제목으로_말해도_각각_근거가_된다() -> None:
    ctx = _ctx("알고리즘이랑 파이썬 책 빼줘", cart=[ALGO, DB, PY])

    assert evidence.has_evidence(ctx, _cart(1), ALGO)
    assert evidence.has_evidence(ctx, _cart(3), PY)
    assert not evidence.has_evidence(ctx, _cart(2), DB)


# --- 근거가 없는 말 ---


@pytest.mark.parametrize(
    "message",
    [
        "그거 담아줘",
        "저거 두 권 담아줘",
        "그 책 장바구니에 넣어줘",
        "방금 거 담아줘",
        "저 책 담아줘",
    ],
)
def test_누구인지_모르는_말은_어떤_카드도_근거가_없다(message: str) -> None:
    ctx = _ctx(message, cards=[PY, DB, K8S])

    for n, isbn in enumerate([PY, DB, K8S], 1):
        assert not evidence.has_evidence(ctx, _card(n), isbn), (message, n)


@pytest.mark.parametrize(
    "message",
    ["아까 그 책 빼줘", "그 두 권 다 빼줘", "아까 본 책 수량 늘려줘", "그거 빼줘"],
)
def test_누구인지_모르는_말은_어떤_장바구니_책도_근거가_없다(message: str) -> None:
    ctx = _ctx(message, cart=[SW, DB, PY])

    for n, isbn in enumerate([SW, DB, PY], 1):
        assert not evidence.has_evidence(ctx, _cart(n), isbn), (message, n)


def test_require는_근거가_없으면_RefError를_던진다() -> None:
    ctx = _ctx("그거 담아줘", cards=[PY, DB])

    with pytest.raises(RefError) as exc_info:
        evidence.require(ctx, _card(1), PY)
    assert exc_info.value.code == ToolErrorCode.REFERENCE_NOT_STATED

    evidence.require(_ctx("1번 담아줘", cards=[PY, DB]), _card(1), PY)


# --- 평가 사례 79개의 정답 행동으로 확인 ---

_DATA = json.loads(
    (Path(__file__).resolve().parents[1] / "evals" / "agent" / "cases.json").read_text(
        encoding="utf-8"
    )
)
_CATALOG = {book["isbn"]: book["title"] for book in _DATA["catalog"]}
_CASES = {case["id"]: case for case in _DATA["cases"]}


def _case_ctx(case: dict) -> TurnContext:
    request = case["request"]
    seed = [line["isbn"] for line in case["state"]["cart"]]  # 담은 순서
    return TurnContext(
        user_id=1,
        logged_in=request["user_context"]["logged_in"],
        cards=[ContextCard(**card) for card in request["context_cards"]],
        focused_isbn=request["focused_isbn"],
        allow_tools=None,
        # BE 조회 순서는 최근에 담은 책이 1번이다
        cart_snapshot=[
            CartLine(isbn=i, title=_CATALOG[i], price=1, quantity=1, available=True)
            for i in reversed(seed)
        ],
        message=request["message"],
        # LLM 이 book.offer 로 카드의 제목을 확인한 뒤라고 본다
        titles={c["isbn"]: _CATALOG[c["isbn"]] for c in request["context_cards"]},
    )


def _natural_refs(ctx: TurnContext, isbn: str) -> list[BookRef]:
    """LLM 이 이 책을 가리킬 때 쓸 수 있는 말."""
    refs = [BookRef(source="card", n=c.rank) for c in ctx.cards if c.isbn == isbn]
    refs += [
        BookRef(source="cart", n=position)
        for position, line in enumerate(ctx.cart_snapshot or [], 1)
        if line.isbn == isbn
    ]
    if ctx.focused_isbn == isbn:
        refs.append(BookRef(source="focused"))
    return refs


def _written_isbns(case: dict) -> set[str]:
    seed = {line["isbn"]: line["qty"] for line in case["state"]["cart"]}
    want = case["expect"]["cart"]
    return {i for i in set(seed) | set(want) if seed.get(i) != want.get(i)}


_EXECUTED = [c for c in _CASES.values() if c["expect"]["outcome"] == "executed"]
_ASK = [c for c in _CASES.values() if c["expect"]["outcome"] == "ask_back"]


@pytest.mark.parametrize("case", _EXECUTED, ids=[c["id"] for c in _EXECUTED])
def test_정확히_말한_사례의_정답_행동은_근거가_있어_막히지_않는다(case: dict) -> None:
    ctx = _case_ctx(case)

    for isbn in _written_isbns(case):
        refs = _natural_refs(ctx, isbn)
        assert refs, (case["id"], isbn)
        assert any(evidence.has_evidence(ctx, ref, isbn) for ref in refs), (
            case["id"],
            case["request"]["message"],
            isbn,
        )


@pytest.mark.parametrize("case", _ASK, ids=[c["id"] for c in _ASK])
def test_되물어야_하는_사례는_어떤_짐작도_근거를_얻지_못한다(case: dict) -> None:
    ctx = _case_ctx(case)
    guesses = [(BookRef(source="card", n=c.rank), c.isbn) for c in ctx.cards] + [
        (BookRef(source="cart", n=position), line.isbn)
        for position, line in enumerate(ctx.cart_snapshot or [], 1)
    ]

    for ref, isbn in guesses:
        assert not evidence.has_evidence(ctx, ref, isbn), (
            case["id"],
            case["request"]["message"],
            ref,
        )
