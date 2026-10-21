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
from app.agent.schemas import ContextCard, Turn
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


# --- 숫자만 말한 번호는 카드 번호다 (#528) ---


@pytest.mark.parametrize(
    ("message", "in_cart", "bare"),
    [
        ("2번 빼줘", set(), {2}),
        ("장바구니 2번째 빼줘", {2}, set()),
        ("장바구니에서 두 번째 빼줘", {2}, set()),
        ("장바구니에 있는 거 3번 빼줘", {3}, set()),
        ("장바구니 중에 첫 번째 거", {1}, set()),
        ("장바구니 두 번째랑 세 번째 빼줘", {2, 3}, set()),
        ("장바구니 첫 번째 빼고 2번 두 권 담아줘", {1}, {2}),
        # 번호가 장바구니 앞에 오면 카드 번호를 장바구니에 담으라는 말이다
        ("1번 장바구니에 담아줘", set(), {1}),
        ("장바구니에 2번 넣어줘", set(), {2}),
    ],
)
def test_번호를_장바구니를_말한_번호와_숫자만_말한_번호로_나눈다(
    message: str, in_cart: set[int], bare: set[int]
) -> None:
    assert evidence.numbers(message) == (in_cart, bare)


def test_카드가_보일_때_숫자만_말한_번호는_장바구니_줄의_근거가_아니다() -> None:
    # dev QA AGENT-005: 카드 2번(K8S)은 장바구니에 없고 장바구니 2번째는 DB 다
    ctx = _ctx("2번 빼줘", cart=[SW, DB, PY], cards=[PY, K8S])

    assert not evidence.has_evidence(ctx, _cart(2), DB)
    assert evidence.has_evidence(ctx, _card(2), K8S)


def test_장바구니를_말한_번호는_카드_번호의_근거가_아니다() -> None:
    ctx = _ctx("장바구니 2번째 빼줘", cart=[SW, DB, PY], cards=[PY, K8S])

    assert evidence.has_evidence(ctx, _cart(2), DB)
    assert not evidence.has_evidence(ctx, _card(2), K8S)


def test_같은_번호의_카드가_없으면_숫자만_말한_번호를_장바구니_번호로_본다() -> None:
    # 카드가 없다
    assert evidence.has_evidence(_ctx("2번 빼줘", cart=[SW, DB]), _cart(2), DB)
    # 카드는 있지만 2번까지 없다
    assert evidence.has_evidence(
        _ctx("2번 빼줘", cart=[SW, DB], cards=[PY]), _cart(2), DB
    )


def test_같은_번호의_카드가_같은_책이면_어느_목록으로_읽든_근거다() -> None:
    ctx = _ctx("2번 빼줘", cart=[SW, DB], cards=[PY, DB])

    assert evidence.has_evidence(ctx, _cart(2), DB)
    assert evidence.has_evidence(ctx, _card(2), DB)


def test_숫자만_말한_번호를_장바구니로_읽어_막히면_같은_번호의_카드를_알려_준다() -> (
    None
):
    ctx = _ctx("2번 빼줘", cart=[SW, DB, PY], cards=[PY, K8S])

    card = evidence.card_for_bare_number(ctx, _cart(2), DB)
    assert card is not None and (card.rank, card.isbn) == (2, K8S)
    # 장바구니를 말했거나, 그 번호의 카드가 없거나, 같은 책이면 둘 중 무엇인지 헷갈리지 않는다
    assert (
        evidence.card_for_bare_number(
            _ctx("장바구니 2번째 빼줘", cart=[SW, DB], cards=[PY, K8S]), _cart(2), DB
        )
        is None
    )
    assert (
        evidence.card_for_bare_number(
            _ctx("2번 빼줘", cart=[SW, DB], cards=[PY]), _cart(2), DB
        )
        is None
    )
    assert (
        evidence.card_for_bare_number(
            _ctx("2번 빼줘", cart=[SW, DB], cards=[PY, DB]), _cart(2), DB
        )
        is None
    )
    # 카드를 고른 것은 헷갈린 것이 아니다
    assert evidence.card_for_bare_number(ctx, _card(2), K8S) is None


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
        # 여러 턴 사례는 이력이 근거가 된다(#397)
        turns=[Turn(**turn) for turn in case.get("history", [])],
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


# 아직 근거로 못 읽는 말(#418). "좋네요 하나 주세요"의 "주세요"는 "알려 주세요"와 겹쳐 행동 낱말에 넣지 않았다.
# 고치기 전에 쓴 heldout 이라 이 사례를 보고 규칙을 바꾸지 않는다. 막히면 모델이 그 책을 짚어 묻는다.
# strict 라서 근거로 읽히게 되는 순간 실패하므로 그때 여기서 뺀다.
_EVIDENCE_GAPS = {"multiturn-04"}
_EXECUTED_PARAMS = [
    pytest.param(
        case,
        id=case["id"],
        marks=pytest.mark.xfail(strict=True, reason="#418 주세요는 행동 낱말이 아니다"),
    )
    if case["id"] in _EVIDENCE_GAPS
    else pytest.param(case, id=case["id"])
    for case in _EXECUTED
]


@pytest.mark.parametrize("case", _EXECUTED_PARAMS)
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


# --- 리뷰 반영 (#394) ---


@pytest.mark.parametrize(
    "message",
    [
        "재밌겠다 담아줘",
        "읽고 싶다 담아줘",
        "별로다 빼줘",
        "좋겠다, 담아줘",
        "그거 담아줘",
    ],
)
def test_다로_끝나는_말투의_다는_전체_표현이_아니다(message: str) -> None:
    # "-다" 어미 뒤에 "담아줘/빼줘"가 와도 어떤 짐작도 근거를 얻지 못해야 한다
    ctx = _ctx(message, cart=[SW, DB, PY], cards=[PY, DB, K8S])

    for n, isbn in enumerate([PY, DB, K8S], 1):
        assert not evidence.has_evidence(ctx, _card(n), isbn), (message, n)
    for n, isbn in enumerate([SW, DB, PY], 1):
        assert not evidence.has_evidence(ctx, _cart(n), isbn), (message, n)


def test_따로_쓴_다는_전체_표현이다() -> None:
    ctx = _ctx("그거 다 빼줘", cart=[SW, DB, PY])

    assert evidence.has_evidence(ctx, _cart(2), DB)
    # 앞에 한글이 붙으면 어미다
    assert not evidence.has_evidence(_ctx("싫다 빼줘", cart=[SW, DB, PY]), _cart(2), DB)
    # 둘 다도 같다: "둘"이 다른 낱말의 끝이면 세지 않는다
    assert not evidence.has_evidence(_ctx("고둘다 빼줘", cart=[DB, PY]), _cart(1), DB)


def test_전부터는_전체_표현이_아니다() -> None:
    # "전부터 읽고 싶던 거 담아줘"의 "전부터"는 "전부"가 아니다
    ctx = _ctx("전부터 읽고 싶던 거 담아줘", cards=[PY, DB])

    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_카드_전부의_제목을_알아야_제목을_근거로_본다() -> None:
    # 카드 셋 제목이 모두 파이썬이고 1번만 조회했다면 "파이썬 책"은 1번만 가리키지 못한다
    ctx = _ctx("파이썬 책 담아줘", cards=[PY, DB, K8S], know_titles=False)
    ctx.titles = {PY: "처음 만나는 파이썬"}

    assert not evidence.has_evidence(ctx, _card(1), PY)

    ctx.titles = {
        PY: "처음 만나는 파이썬",
        DB: "데이터베이스 설계 노트",
        K8S: "쿠버네티스 입문 가이드",
    }
    assert evidence.has_evidence(ctx, _card(1), PY)


def test_카드_셋이_다_파이썬_책이면_파이썬_책은_어느_하나의_근거도_아니다() -> None:
    ctx = _ctx("파이썬 책 담아줘", cards=[PY, DB, K8S], know_titles=False)
    ctx.titles = {PY: "파이썬 기초", DB: "파이썬 심화", K8S: "파이썬 실전"}

    for n, isbn in enumerate([PY, DB, K8S], 1):
        assert not evidence.has_evidence(ctx, _card(n), isbn)


def test_두_글자_흔한_낱말_하나는_제목의_근거가_아니다() -> None:
    wide = "9788900001999"
    ctx = _ctx("나는 그게 좋아 담아줘", cards=[PY, wide], know_titles=False)
    ctx.titles = {PY: "처음 만나는 파이썬", wide: "나는 나로 살기로 했다"}

    assert not evidence.has_evidence(ctx, _card(2), wide)
    # 제목을 제대로 말하면 근거다
    said = _ctx("나는 나로 살기로 했다 담아줘", cards=[PY, wide], know_titles=False)
    said.titles = ctx.titles
    assert evidence.has_evidence(said, _card(2), wide)


def test_세_글자_이상_낱말은_하나만으로도_근거다() -> None:
    ctx = _ctx("파이썬 빼줘", cart=[DB, PY])

    assert evidence.has_evidence(ctx, _cart(2), PY)


def test_카드의_맨_처음은_1번이고_장바구니의_처음_담은_책은_마지막_번호다() -> None:
    cards = _ctx("맨 처음 거 담아줘", cards=[PY, DB, K8S])
    assert evidence.has_evidence(cards, _card(1), PY)
    assert not evidence.has_evidence(cards, _card(3), K8S)

    # 장바구니는 최근에 담은 책이 1번이다. "처음 담은 거"는 가장 오래된 마지막 번호
    cart = _ctx("처음 담은 거 빼줘", cart=[SW, DB, PY])
    assert evidence.has_evidence(cart, _cart(3), PY)
    assert not evidence.has_evidence(cart, _cart(1), SW)


def test_마지막_카드는_마지막_번호다() -> None:
    ctx = _ctx("마지막 카드 담아줘", cards=[PY, DB, K8S])

    assert evidence.has_evidence(ctx, _card(3), K8S)
    assert not evidence.has_evidence(ctx, _card(1), PY)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("열한 번째 책 담아줘", set()),
        ("스물한 번째 책", set()),
        ("한 번째 책", {1}),
        ("열 번째", {10}),
    ],
)
def test_한글_뒤에_붙은_순서_낱말은_세지_않는다(
    message: str, expected: set[int]
) -> None:
    assert evidence.ordinals(message) == expected


# --- 평범한 말투 (#397) ---


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("삼번 담아줘", "3번 담아줘"),
        ("일번이랑 삼번 담아줘", "1번이랑 3번 담아줘"),
        ("삼번이요", "3번이요"),
        ("십 번 담아줘", "10번 담아줘"),
        ("젤 위에꺼 빼줘", "제일 위에거 빼줘"),
        ("마지막꺼 담아줘", "마지막거 담아줘"),
        # 뜻이 달라지는 말은 건드리지 않는다
        ("불 꺼줘", "불 꺼줘"),
        ("꺼진 불", "꺼진 불"),
        ("젤리 책 담아줘", "젤리 책 담아줘"),
        ("첫일번 책", "첫일번 책"),
        # "이번 거"는 2번이 아니다
        ("이번 거 담아줘", "이번 거 담아줘"),
    ],
)
def test_구어체를_표준_표현으로_바꾸되_뜻이_달라지는_말은_두지_않는다(
    message: str, expected: str
) -> None:
    assert evidence.normalize(message) == expected


@pytest.mark.parametrize(
    ("message", "ref", "isbn", "expected"),
    [
        ("삼번 담아줘", _card(3), K8S, True),
        ("삼번 담아줘", _card(2), DB, False),
        ("일번 담아줘", _card(1), PY, True),
        ("삼번이요", _card(3), K8S, True),
        ("일번이랑 삼번 담아줘", _card(1), PY, True),
        ("일번이랑 삼번 담아줘", _card(3), K8S, True),
        ("일번이랑 삼번 담아줘", _card(2), DB, False),
        ("이번 거 담아줘", _card(2), DB, False),
        ("마지막꺼 담아줘", _card(3), K8S, True),
        ("마지막꺼 담아줘", _card(1), PY, False),
        ("맨 처음꺼 담아줘", _card(1), PY, True),
        ("맨 처음꺼 담아줘", _card(3), K8S, False),
    ],
)
def test_말투가_달라도_카드를_가리키면_근거다(message, ref, isbn, expected) -> None:
    ctx = _ctx(message, cards=[PY, DB, K8S])

    assert evidence.has_evidence(ctx, ref, isbn) is expected


@pytest.mark.parametrize(
    ("message", "ref", "isbn", "expected"),
    [
        # 장바구니는 최근에 담은 책이 1번이다: [SW, DB, PY]
        ("젤 위에꺼 빼줘", _cart(1), SW, True),
        ("젤 위에꺼 빼줘", _cart(3), PY, False),
        ("제일 밑에꺼 빼줘", _cart(3), PY, True),
        ("제일 밑에꺼 빼줘", _cart(1), SW, False),
        ("맨 끝에꺼 빼줘", _cart(3), PY, True),
        ("방금 거 빼줘", _cart(1), SW, True),
        ("방금 거 빼줘", _cart(2), DB, False),
        ("가장 위에꺼 빼줘", _cart(1), SW, True),
    ],
)
def test_말투가_달라도_장바구니의_위치를_가리키면_근거다(
    message, ref, isbn, expected
) -> None:
    ctx = _ctx(message, cart=[SW, DB, PY])

    assert evidence.has_evidence(ctx, ref, isbn) is expected


# --- 이력에서 나오는 근거 (#397) ---


def _history(ctx: TurnContext, *pairs: tuple[str, str]) -> TurnContext:
    ctx.turns = [Turn(role=role, text=text) for role, text in pairs]
    return ctx


PROPOSE_PY = ("assistant", "카드 1번 《처음 만나는 파이썬》을 담을까요?")
ASK_WHICH = (
    "assistant",
    "어느 책을 담을까요? 카드 1번은 처음 만나는 파이썬, 2번은 데이터베이스 설계 노트, 3번은 쿠버네티스 입문 가이드예요.",
)


@pytest.mark.parametrize(
    "agreement",
    [
        "네",
        "응",
        "좋아",
        "좋아요",
        "그래",
        "그걸로 해줘",
        "ㅇㅇ",
        "오케이",
        "네 담아줘",
        "네!",
    ],
)
def test_에이전트가_한_권을_제안했고_동의하면_그_책이_근거다(agreement: str) -> None:
    ctx = _history(
        _ctx(agreement, cards=[PY, DB, K8S]), ("user", "그거 담아줘"), PROPOSE_PY
    )

    assert evidence.has_evidence(ctx, _card(1), PY)
    # 제안하지 않은 책에는 근거를 주지 않는다
    assert not evidence.has_evidence(ctx, _card(2), DB)


def test_선택지를_나열한_말에_대한_네는_근거가_아니다() -> None:
    ctx = _history(_ctx("네", cards=[PY, DB, K8S]), ("user", "그거 담아줘"), ASK_WHICH)

    for n, isbn in enumerate([PY, DB, K8S], 1):
        assert not evidence.has_evidence(ctx, _card(n), isbn), n


def test_제목을_몰라도_번호가_둘_이상이면_선택지다() -> None:
    # 카드 제목을 아직 조회하지 않아도 "1번 ..., 2번 ..."은 두 권을 짚은 것이다
    ctx = _ctx("네", cards=[PY, DB, K8S], know_titles=False)
    ctx = _history(
        ctx,
        ("user", "담아줘"),
        ("assistant", "카드 1번 파이썬, 2번 DB 중 어느 책을 담을까요?"),
    )

    assert not evidence.has_evidence(ctx, _card(1), PY)
    assert not evidence.has_evidence(ctx, _card(2), DB)


def test_책을_짚지_않은_질문에_대한_네는_근거가_아니다() -> None:
    ctx = _history(
        _ctx("네", cards=[PY, DB, K8S]),
        ("user", "담아줘"),
        ("assistant", "어느 책인가요?"),
    )

    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_제안이_아니라_알림에_대한_네는_근거가_아니다() -> None:
    ctx = _history(
        _ctx("네", cards=[PY, DB, K8S]),
        ("user", "1번 담아줘"),
        ("assistant", "《처음 만나는 파이썬》을 장바구니에 담았어요."),
    )

    assert not evidence.has_evidence(ctx, _card(1), PY)


@pytest.mark.parametrize(
    "message", ["어떤 책이요?", "예쁜 거 담아줘", "그래서 뭐가 달라?"]
)
def test_동의처럼_보이는_다른_말은_동의가_아니다(message: str) -> None:
    ctx = _history(_ctx(message, cards=[PY, DB, K8S]), ("user", "담아줘"), PROPOSE_PY)

    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_동의_뒤에_긴_말이_이어지면_동의로만_보지_않는다() -> None:
    long = "네 그런데 그거 말고 다른 책을 한참 고민해서 다시 정해 보고 싶어요 오래 걸려도 괜찮죠"
    ctx = _history(_ctx(long, cards=[PY, DB, K8S]), ("user", "담아줘"), PROPOSE_PY)

    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_직전_말이_사용자의_말이면_이력으로_근거를_주지_않는다() -> None:
    ctx = _history(_ctx("네", cards=[PY, DB, K8S]), PROPOSE_PY, ("user", "음"))

    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_이력이_없으면_네는_근거가_아니다() -> None:
    assert not evidence.has_evidence(_ctx("네", cards=[PY, DB, K8S]), _card(1), PY)


def test_방금_담았다고_한_한_권을_그거로_가리킬_수_있다() -> None:
    ctx = _history(
        _ctx("그거 취소해줘", cart=[PY, DB]),
        ("user", "1번 담아줘"),
        ("assistant", "《처음 만나는 파이썬》을 장바구니에 담았어요."),
    )

    assert evidence.has_evidence(ctx, _cart(1), PY)
    assert not evidence.has_evidence(ctx, _cart(2), DB)


@pytest.mark.parametrize(
    "message",
    ["그거 말고 다른 책으로 할게요", "아니 그거 말고", "그거 싫어", "이거 그만 담아줘"],
)
def test_그거_말고처럼_부정이_있으면_지시어를_근거로_보지_않는다(message: str) -> None:
    ctx = _history(
        _ctx(message, cart=[PY, DB]),
        ("assistant", "《처음 만나는 파이썬》을 장바구니에 담았어요."),
    )

    assert not evidence.has_evidence(ctx, _cart(1), PY)


@pytest.mark.parametrize(
    "pointer", ["그거 빼줘", "그 책 빼줘", "이거 빼줘", "아까 그거 빼줘"]
)
def test_지시어도_직전_말이_한_권을_짚었을_때만_근거다(pointer: str) -> None:
    said_one = _history(
        _ctx(pointer, cart=[PY, DB]),
        ("assistant", "《처음 만나는 파이썬》을 장바구니에 담았어요."),
    )
    said_two = _history(
        _ctx(pointer, cart=[PY, DB]),
        (
            "assistant",
            "《처음 만나는 파이썬》과 《데이터베이스 설계 노트》를 담았어요.",
        ),
    )

    assert evidence.has_evidence(said_one, _cart(1), PY)
    assert not evidence.has_evidence(said_two, _cart(1), PY)
    assert not evidence.has_evidence(said_two, _cart(2), DB)


def test_장바구니_번째로_짚은_한_권도_이력의_근거다() -> None:
    ctx = _history(
        _ctx("응", cart=[SW, DB, PY]),
        ("user", "빼줘"),
        ("assistant", "장바구니 2번째 책을 뺄까요?"),
    )

    assert evidence.has_evidence(ctx, _cart(2), DB)
    assert not evidence.has_evidence(ctx, _cart(1), SW)


def test_카드_번호와_장바구니_번호를_섞어_읽지_않는다() -> None:
    # "장바구니 1번째"를 카드 1번으로 읽으면 엉뚱한 책이 된다
    ctx = _history(
        _ctx("응", cart=[SW, DB, PY], cards=[PY, DB, K8S]),
        ("user", "빼줘"),
        ("assistant", "장바구니 1번째 책을 뺄까요?"),
    )

    assert evidence.has_evidence(ctx, _cart(1), SW)
    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_이력에서_나온_근거로_정해진_책도_resolved_reference에_싣는다() -> None:
    ctx = _history(_ctx("네", cards=[PY, DB, K8S]), ("user", "담아줘"), PROPOSE_PY)

    assert evidence.stated(ctx, PY)
    assert not evidence.stated(ctx, DB)


# --- 리뷰 반영 (#398): 잘못 통과하는 두 가지만 조인다 ---
# 말투를 더 받아 주는 규칙은 늘리지 않는다. 막혀서 되묻는 쪽은 평가 사례로 모은 뒤에 다룬다.


@pytest.mark.parametrize(
    ("message", "n", "isbn"),
    [
        ("네 근데 2번으로요", 2, DB),
        ("응 아니 3번", 3, K8S),
        ("네? 아니요 2번이요", 2, DB),
        ("좋아 근데 쿠버네티스로", 3, K8S),
        ("네 근데 맨 아래 걸로 할게요", 3, K8S),
    ],
)
def test_동의_뒤에_다른_책을_말하면_제안한_책이_아니라_말한_책이_근거다(
    message: str, n: int, isbn: str
) -> None:
    ctx = _history(
        _ctx(message, cards=[PY, DB, K8S]), ("user", "그거 담아줘"), PROPOSE_PY
    )

    assert not evidence.has_evidence(ctx, _card(1), PY)
    assert evidence.has_evidence(ctx, _card(n), isbn)


@pytest.mark.parametrize("message", ["네? 아니요", "응 그만할래", "네 아니 됐어요"])
def test_동의처럼_시작해도_부정이_있으면_동의가_아니다(message: str) -> None:
    ctx = _history(_ctx(message, cards=[PY, DB, K8S]), ("user", "담아줘"), PROPOSE_PY)

    assert not evidence.has_evidence(ctx, _card(1), PY)


@pytest.mark.parametrize(
    "message", ["응 안 담을래", "네 담지 마세요", "좋아 근데 넣지 않을래"]
)
def test_동의처럼_시작해도_행동을_거절하면_동의가_아니다(message: str) -> None:
    ctx = _history(_ctx(message, cards=[PY, DB, K8S]), ("user", "담아줘"), PROPOSE_PY)

    assert not evidence.has_evidence(ctx, _card(1), PY)


@pytest.mark.parametrize("message", ["어", "어?"])
def test_어는_동의가_아니다(message: str) -> None:
    ctx = _history(_ctx(message, cards=[PY, DB, K8S]), ("user", "담아줘"), PROPOSE_PY)

    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_지시어와_함께_다른_책을_말하면_지시어를_근거로_보지_않는다() -> None:
    ctx = _history(
        _ctx("그거 대신 2번 담아줘", cart=[PY, DB]),
        ("assistant", "《처음 만나는 파이썬》을 장바구니에 담았어요."),
    )

    assert not evidence.has_evidence(ctx, _cart(1), PY)
    assert evidence.has_evidence(ctx, _cart(2), DB)


@pytest.mark.parametrize(
    "message", ["가장 위로가 되는 거 담아줘", "제일 앞서가는 책 담아줘"]
)
def test_위나_앞으로_시작하는_다른_낱말은_맨_위가_아니다(message: str) -> None:
    cards = _ctx(message, cards=[PY, DB, K8S])
    cart = _ctx(message, cart=[SW, DB, PY])

    assert not evidence.has_evidence(cards, _card(1), PY)
    assert not evidence.has_evidence(cart, _cart(1), SW)


@pytest.mark.parametrize(
    "message", ["가장 끝내주는 거 담아줘", "가장 뒤늦게 나온 거 담아줘"]
)
def test_끝이나_뒤로_시작하는_다른_낱말은_맨_아래가_아니다(message: str) -> None:
    cards = _ctx(message, cards=[PY, DB, K8S])
    cart = _ctx(message, cart=[SW, DB, PY])

    assert not evidence.has_evidence(cards, _card(3), K8S)
    assert not evidence.has_evidence(cart, _cart(3), PY)


@pytest.mark.parametrize(
    "message", ["맨 위 책 담아줘", "맨 위쪽 책 담아줘", "맨 위에 있는 거 담아줘"]
)
def test_위치_낱말이_조사나_띄어쓰기로_끝나면_여전히_맨_위다(message: str) -> None:
    assert evidence.has_evidence(_ctx(message, cards=[PY, DB, K8S]), _card(1), PY)
    assert evidence.has_evidence(_ctx(message, cart=[SW, DB, PY]), _cart(1), SW)


@pytest.mark.parametrize(
    "message", ["맨 아래 책 담아줘", "맨 아래쪽 책 담아줘", "맨 끝에 있는 거 담아줘"]
)
def test_위치_낱말이_조사나_띄어쓰기로_끝나면_여전히_맨_아래다(message: str) -> None:
    assert evidence.has_evidence(_ctx(message, cards=[PY, DB, K8S]), _card(3), K8S)
    assert evidence.has_evidence(_ctx(message, cart=[SW, DB, PY]), _cart(3), PY)


# --- 대화에서 이야기한 책 (#418) ---

TALK_PY = (
    ("user", "1번 얼마야?"),
    ("assistant", "《처음 만나는 파이썬》은 15,000원이에요."),
    ("user", "초보도 봐?"),
    ("assistant", "프로그래밍이 처음인 사람을 위한 입문서라 괜찮아요."),
)


@pytest.mark.parametrize(
    "message", ["응 담아", "그럼 두 권 담아줘", "그거 담아줘", "오케이 넣어줘"]
)
def test_대화에서_한_권만_이야기했고_행동을_말하면_그_책이_근거다(message: str) -> None:
    ctx = _history(_ctx(message, cards=[PY, DB, K8S]), *TALK_PY)

    assert evidence.has_evidence(ctx, _card(1), PY)
    assert not evidence.has_evidence(ctx, _card(2), DB)


@pytest.mark.parametrize(
    "message",
    [
        "네",  # 행동을 말하지 않았다. 동의는 직전 제안이 있을 때만 근거다
        "얼마였지?",
        "다른 거 담아줘",
        "딴 책 담아줘",
        "그거 말고 담아줘",
        "제일 싼 거 담아줘",  # 꾸며서 가리킨 책은 코드가 뜻을 확인할 수 없다
        "예쁜 책 담아줘",
    ],
)
def test_행동을_말하지_않거나_다른_책을_뜻하면_대화의_책은_근거가_아니다(
    message: str,
) -> None:
    ctx = _history(_ctx(message, cards=[PY, DB, K8S]), *TALK_PY)

    assert not evidence.has_evidence(ctx, _card(1), PY)


@pytest.mark.parametrize(
    "message", ["안 담을래", "담지 마", "안담아", "넣지 않을래", "빼지 말아 줘"]
)
def test_행동을_거절하면_대화의_책은_근거가_아니다(message: str) -> None:
    # #419 리뷰: 행동 낱말만 보면 거절도 대화의 책에 대한 근거가 된다
    ctx = _history(
        _ctx(message, cards=[PY, DB, K8S]),
        ("user", "1번 얼마야?"),
        (
            "assistant",
            "카드 1번 《처음 만나는 파이썬》은 15,000원이에요. 장바구니에 담을까요?",
        ),
    )

    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_대화에서_두_권을_이야기했으면_어느_쪽도_근거가_아니다() -> None:
    ctx = _history(
        _ctx("응 담아", cards=[PY, DB, K8S]),
        ("user", "1번이랑 3번 가격 알려 줘"),
        ("assistant", "15,000원과 28,000원이에요."),
    )

    assert not evidence.has_evidence(ctx, _card(1), PY)
    assert not evidence.has_evidence(ctx, _card(3), K8S)


def test_제목_낱말로_다른_책을_이야기한_줄도_센다() -> None:
    # 번호 없이 "쿠버네티스 책은?"으로 물었다. 이 줄을 못 알아보면 1번만 이야기한 것으로 잘못 본다
    ctx = _history(
        _ctx("응 담아", cards=[PY, DB, K8S]),
        ("user", "1번 얼마야?"),
        ("assistant", "15,000원이에요."),
        ("user", "쿠버네티스 책은?"),
        ("assistant", "28,000원이에요."),
    )

    assert not evidence.has_evidence(ctx, _card(1), PY)
    assert not evidence.has_evidence(ctx, _card(3), K8S)


def test_카드_제목을_모르면_대화의_책을_정하지_않는다() -> None:
    # 에이전트가 제목으로만 말한 줄을 알아볼 수 없어서 한 권뿐인지 모른다
    ctx = _history(_ctx("응 담아", cards=[PY, DB, K8S], know_titles=False), *TALK_PY)

    assert evidence.needs_card_titles(ctx)
    assert not evidence.has_evidence(ctx, _card(1), PY)


def test_모델에게_보여_준_대화만_본다() -> None:
    # 오래전에 3번을 이야기했어도 최근 대화(PROMPT_TURNS 줄)가 1번 한 권뿐이면 1번이다
    old = (("user", "3번 얼마야?"), ("assistant", "28,000원이에요."))
    ctx = _history(_ctx("응 담아", cards=[PY, DB, K8S]), *old, *TALK_PY, *TALK_PY[2:])

    assert len(ctx.turns) > evidence.PROMPT_TURNS
    assert evidence.has_evidence(ctx, _card(1), PY)


# --- 행동 (#531) ---


@pytest.mark.parametrize(
    "message",
    [
        "1번 담아줘",
        "장바구니 두 번째 빼줘",
        "3권으로 해줘",
        "객체지향 책 세 권으로 해줘",
        "좋네요 하나 주세요",
        "1번 주세요",
        "이거 줘",
        "2번으로 살게요",
        "1번 사고 싶어요",
        "2번 구매할게",
        "1번 담고 싶어",
        "2번 담어줘",
        "1번 장바구니에 추가해줘",
    ],
)
def test_행동이나_수량을_말하면_행동을_시킨_것이다(message: str) -> None:
    assert evidence.states_action(_ctx(message, cards=[PY, DB, K8S]))


@pytest.mark.parametrize(
    "message",
    [
        # #530 대화 평가에서 모델이 담기로 읽은 말들
        "1번 카드가 가장 마음에 드는 것 같아.",
        "『처음 만나는 파이썬』이 괜찮아 보여!",
        "1번 책이 어른이 읽기 좋은 것 같아.",
        "1번 책이 우주 이야기와 잘 맞는 것 같아!",
        "2번 표지 예쁘다",
        # 행동이 애매한 말은 받지 않는다(한 번 묻는다)
        "1번으로 할게요",
        # 도움을 청하는 "주세요"는 달라는 말이 아니다
        "1번 가격 알려 주세요",
        # "담", "추가"가 들었지만 행동이 아닌 말(#530 대화 평가에서 담기의 근거가 됐다)
        "이 책은 일상을 담담하게 적어서 좋을 것 같아!",
        "2번이 일상이 잘 담겨있어 좋아!",
        "1번은 여행의 기억을 담고 있어서 좋네",
        "삶을 담아낸 문장이 좋아 보여",
        "추가로 궁금한 게 있어",
    ],
)
def test_관심만_말하면_행동을_시킨_것이_아니다(message: str) -> None:
    assert not evidence.states_action(_ctx(message, cards=[PY, DB, K8S]))


def test_말을_모르면_행동을_검사하지_않는다() -> None:
    assert evidence.states_action(_ctx(None, cards=[PY, DB]))


@pytest.mark.parametrize(
    "asked",
    [
        PROPOSE_PY,
        ("assistant", "어느 책을 담을까요? 카드 1번은 처음 만나는 파이썬이에요."),
        ("assistant", "장바구니 두 번째 《데이터베이스 설계 노트》를 뺄까요?"),
        ("assistant", "어느 책을 몇 권으로 바꿀까요?"),
        ("assistant", "장바구니에 담아 드릴까요"),
    ],
)
def test_에이전트가_행동을_물었으면_그_답은_행동을_시킨_것이다(asked) -> None:
    for message in ("1번이요", "네", "파이썬이요"):
        ctx = _history(_ctx(message, cards=[PY, DB]), ("user", "그거 좀 봐줘"), asked)
        assert evidence.states_action(ctx), (asked, message)


@pytest.mark.parametrize(
    "said",
    [
        # 알림 뒤의 물음은 행동을 묻지 않았다(문장 단위로 본다)
        ("assistant", "1번 책을 장바구니에 담았어요. 다른 도와드릴 일이 있으신가요?"),
        ("assistant", "카드 1번은 15,000원이에요. 더 궁금한 게 있으세요?"),
        # 직전 말이 사용자 것이면 물은 것이 없다
        ("user", "어느 책을 담을까요?"),
        # "추가로"는 행동을 묻는 말이 아니다
        (
            "assistant",
            "《처음 만나는 파이썬》을 담았어요. 추가로 다른 책이 필요하신가요?",
        ),
    ],
)
def test_행동을_묻지_않은_말_뒤의_관심은_행동을_시킨_것이_아니다(said) -> None:
    ctx = _history(_ctx("2번도 좋아 보여", cards=[PY, DB]), said)

    assert not evidence.states_action(ctx)


def test_행동을_물은_말에_되물으면_답이_아니다() -> None:
    ctx = _history(
        _ctx("1번 책이 마음에 든다! 더 궁금한 내용 있어?", cards=[PY, DB]),
        ("assistant", "이 책을 장바구니에 담을까요?"),
    )

    assert not evidence.states_action(ctx)


def test_에이전트가_담으시겠어요로_물어도_행동을_물은_것이다() -> None:
    ctx = _history(
        _ctx("네", cards=[PY, DB]), ("assistant", "카드 1번을 담으시겠어요?")
    )

    assert evidence.states_action(ctx)


@pytest.mark.parametrize("case", _EXECUTED, ids=[c["id"] for c in _EXECUTED])
def test_실행하는_사례는_모두_행동을_말했다(case: dict) -> None:
    ctx = _case_ctx(case)

    assert evidence.states_action(ctx), (case["id"], case["request"]["message"])
