"""⑦ 책 확정(app/agent/reference.py) 테스트(#354). LLM 도 BE 도 쓰지 않는다.

LLM 은 책을 "카드 몇 번, 장바구니 몇 번째, 지금 보는 책"으로만 가리키고 ISBN 은 코드가 채운다.
가리킬 수 없으면 실행하지 않는다 — 여기서 어떤 말이 어느 책이 되는지, 무엇이 거절되는지를 고정한다.
"""

import pytest
from pydantic import ValidationError

from app.agent.errors import RefError, ToolErrorCode
from app.agent.reference import BookRef, TurnContext, resolve
from app.agent.schemas import ContextCard, ToolName, parse_request
from app.gateway.commerce import CartLine

A, B, C, D = (f"97889{n:08d}" for n in (1, 2, 3, 4))


def _card(isbn: str, rank: int) -> ContextCard:
    return ContextCard(isbn=isbn, rank=rank, match_score=80)


def _line(isbn: str) -> CartLine:
    return CartLine(isbn=isbn, title="책", price=1000, quantity=1, available=True)


def _ctx(**overrides) -> TurnContext:
    values = {
        "user_id": 1,
        "logged_in": True,
        "cards": [_card(A, 1), _card(B, 2), _card(C, 3)],
        "focused_isbn": None,
        "allow_tools": None,
    }
    values.update(overrides)
    return TurnContext(**values)


def _code(ctx: TurnContext, ref: dict) -> ToolErrorCode:
    with pytest.raises(RefError) as exc_info:
        resolve(ctx, BookRef.model_validate(ref))
    return exc_info.value.code


# --- BookRef: LLM 이 주는 인자의 모양 ---


@pytest.mark.parametrize(
    "ref",
    [
        pytest.param({"source": "card", "n": 1}, id="카드"),
        pytest.param({"source": "cart", "n": 12}, id="장바구니"),
        pytest.param({"source": "focused"}, id="지금_보는_책"),
    ],
)
def test_BookRef는_세_가지_모양을_받는다(ref) -> None:
    assert BookRef.model_validate(ref)


@pytest.mark.parametrize(
    "ref",
    [
        pytest.param({"source": "card"}, id="카드인데_번호_없음"),
        pytest.param({"source": "cart"}, id="장바구니인데_번호_없음"),
        pytest.param({"source": "focused", "n": 1}, id="지금_보는_책에_번호"),
        pytest.param({"source": "card", "n": 0}, id="번호_0"),
        pytest.param({"source": "card", "n": -1}, id="번호_음수"),
        pytest.param({"source": "card", "n": "1"}, id="번호_문자열"),
        pytest.param({"source": "card", "n": 1.0}, id="번호_실수"),
        pytest.param({"source": "card", "n": True}, id="번호_불리언"),
        pytest.param({"source": "shelf", "n": 1}, id="모르는_목록"),
        pytest.param({"n": 1}, id="목록_없음"),
        # LLM 이 지어낸 ISBN 이나 사용자 번호를 끼워 넣어도 받지 않는다
        pytest.param(
            {"source": "card", "n": 1, "isbn": "9788900000001"}, id="isbn_끼움"
        ),
        pytest.param({"source": "card", "n": 1, "user_id": 2}, id="user_id_끼움"),
        pytest.param({"isbn": "9788900000001"}, id="isbn만"),
    ],
)
def test_BookRef는_ISBN을_끼워_넣거나_모양이_틀리면_받지_않는다(ref) -> None:
    with pytest.raises(ValidationError):
        BookRef.model_validate(ref)


# --- 카드 ---


def test_카드는_목록_위치가_아니라_사용자에게_보인_번호로_찾는다() -> None:
    # 카드가 rank 순서로 오지 않아도 "1번"은 rank 1이다
    ctx = _ctx(cards=[_card(C, 3), _card(A, 1), _card(B, 2)])

    assert resolve(ctx, BookRef(source="card", n=1)) == A
    assert resolve(ctx, BookRef(source="card", n=3)) == C


def test_없는_카드_번호는_실행하지_않는다() -> None:
    ctx = _ctx(cards=[_card(A, 1), _card(B, 2)])

    assert _code(ctx, {"source": "card", "n": 3}) == ToolErrorCode.BOOK_NOT_IN_CARDS


def test_카드가_하나도_없으면_실행하지_않는다() -> None:
    assert (
        _code(_ctx(cards=[]), {"source": "card", "n": 1})
        == ToolErrorCode.BOOK_NOT_IN_CARDS
    )


def test_같은_번호에_다른_책이_둘이면_애매해서_실행하지_않는다() -> None:
    ctx = _ctx(cards=[_card(A, 1), _card(B, 1)])

    assert _code(ctx, {"source": "card", "n": 1}) == ToolErrorCode.AMBIGUOUS_BOOK


def test_같은_번호에_같은_책이_둘이면_하나로_본다() -> None:
    ctx = _ctx(cards=[_card(A, 1), _card(A, 1)])

    assert resolve(ctx, BookRef(source="card", n=1)) == A


# --- 장바구니 ---


def test_장바구니_번호는_마지막으로_조회한_목록_기준이다() -> None:
    ctx = _ctx(cart_snapshot=[_line(C), _line(B), _line(A)])

    assert resolve(ctx, BookRef(source="cart", n=1)) == C
    assert resolve(ctx, BookRef(source="cart", n=3)) == A


def test_조회하기_전에는_장바구니_번호를_풀_수_없다() -> None:
    # 목록을 모르면 몇 번째인지 알 수 없다. 짐작으로 실행하지 않는다
    assert _code(_ctx(), {"source": "cart", "n": 1}) == ToolErrorCode.CART_NOT_LOADED


def test_빈_장바구니를_조회한_뒤에는_없는_번호다() -> None:
    ctx = _ctx(cart_snapshot=[])

    assert _code(ctx, {"source": "cart", "n": 1}) == ToolErrorCode.BOOK_NOT_IN_CART


def test_목록보다_큰_장바구니_번호는_실행하지_않는다() -> None:
    ctx = _ctx(cart_snapshot=[_line(A), _line(B)])

    assert _code(ctx, {"source": "cart", "n": 3}) == ToolErrorCode.BOOK_NOT_IN_CART


# --- 지금 보는 책 ---


def test_지금_보는_책은_요청의_focused_isbn이다() -> None:
    # 카드에 없어도 된다. 사용자가 지목한 책이다
    ctx = _ctx(focused_isbn=D)

    assert resolve(ctx, BookRef(source="focused")) == D


def test_지목한_책이_없으면_실행하지_않는다() -> None:
    assert _code(_ctx(), {"source": "focused"}) == ToolErrorCode.NO_FOCUSED_BOOK


# --- 요청에서 만들기 ---


def test_요청에서_만든_상태는_요청의_값_그대로다() -> None:
    req = parse_request(
        {
            "user_id": 123,
            "message": "이거 담아줘",
            "context_cards": [{"isbn": A, "rank": 1, "match_score": 90}],
            "focused_isbn": B,
            "user_context": {"logged_in": False},
            "allow_tools": ["cart.view", "cart.add"],
            "idempotency_key": "k",
        }
    )
    assert req is not None

    ctx = TurnContext.from_request(req)

    assert ctx.user_id == 123
    assert ctx.logged_in is False
    assert [c.isbn for c in ctx.cards] == [A]
    assert ctx.focused_isbn == B
    assert ctx.allow_tools == [ToolName.CART_VIEW, ToolName.CART_ADD]
    assert ctx.cart_snapshot is None
    assert ctx.pending_cart is None


def test_요청에서_만든_상태를_바꿔도_요청은_그대로다() -> None:
    req = parse_request(
        {
            "user_id": 1,
            "message": "담아줘",
            "context_cards": [{"isbn": A, "rank": 1, "match_score": 90}],
            "user_context": {"logged_in": True},
            "allow_tools": ["cart.view"],
            "idempotency_key": "k",
        }
    )
    assert req is not None

    ctx = TurnContext.from_request(req)
    ctx.cards.clear()
    ctx.allow_tools.append(ToolName.CART_ADD)

    assert len(req.context_cards) == 1
    assert req.allow_tools == [ToolName.CART_VIEW]
