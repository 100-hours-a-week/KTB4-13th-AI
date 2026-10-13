"""⑦ 답변 검사와 응답 조립(app/agent/reply.py) 테스트(#358). LLM 도 BE 도 쓰지 않는다.

6단계에서 실제 모델로 재 봤을 때 나온 두 사고(하지 않은 일을 했다고 답함, 합계를 지어냄)를 그대로 재현해서
막는지 본다. 맞는 말(완료 표현, 합계, 부정문)은 지워지지 않아야 한다.
"""

import json
from typing import Any

import pytest

from app.agent.graph import AgentRun
from app.agent.prompts import NOT_SHOPPING
from app.agent.reference import TurnContext
from app.agent.reply import FALLBACK_REPLY, LOGIN_REPLY, GroundingFailed, finish
from app.agent.schemas import AgentActData, ContextCard, ToolCall, ToolName
from app.gateway.commerce import CartLine

A, B, C = (f"97889{n:08d}" for n in (1, 2, 3))


def _ctx() -> TurnContext:
    return TurnContext(
        user_id=1,
        logged_in=True,
        cards=[
            ContextCard(isbn=A, rank=1, match_score=90),
            ContextCard(isbn=B, rank=2, match_score=80),
        ],
        focused_isbn=None,
        allow_tools=None,
        cart_snapshot=[
            CartLine(isbn=C, title="책3", price=1, quantity=1, available=True)
        ],
    )


def _call(
    name: ToolName, result: dict[str, Any], arguments: dict[str, Any] | None = None
) -> ToolCall:
    return ToolCall(name=name, arguments=arguments or {}, result=result, grounded=True)


def _view(total: int = 37000, items: list[tuple[int, int]] | None = None) -> ToolCall:
    lines = items or [(15000, 1), (22000, 1)]
    return _call(
        ToolName.CART_VIEW,
        {
            "items": [
                {
                    "isbn": A,
                    "title": "책",
                    "price": price,
                    "quantity": qty,
                    "available": True,
                }
                for price, qty in lines
            ],
            "total": total,
        },
    )


def _add(ok: bool = True, isbn: str = A) -> ToolCall:
    result = (
        {"ok": True} if ok else {"ok": False, "error": "INSUFFICIENT_PRODUCT_STOCK"}
    )
    return _call(ToolName.CART_ADD, result, {"isbn": isbn, "quantity": 1})


def _remove(ok: bool = True) -> ToolCall:
    result = {"ok": True} if ok else {"ok": False, "error": "CART_ITEM_NOT_FOUND"}
    return _call(ToolName.CART_REMOVE, result, {"isbn": C})


def _update(ok: bool = True) -> ToolCall:
    result = (
        {"ok": True} if ok else {"ok": False, "error": "INVALID_CART_ITEM_QUANTITY"}
    )
    return _call(ToolName.CART_UPDATE, result, {"isbn": C, "quantity": 2})


def _offer(*prices: int) -> ToolCall:
    isbns = [A, B, C][: len(prices)]
    return _call(
        ToolName.BOOK_OFFER,
        {
            "offers": [
                {"isbn": i, "title": "책", "price": p, "in_stock": True}
                for i, p in zip(isbns, prices)
            ]
        },
        {"isbns": isbns},
    )


def _run(
    text: str, calls: list[ToolCall] | None = None, *, not_shopping: bool = False
) -> AgentRun:
    return AgentRun(
        final_text=text,
        not_shopping=not_shopping,
        tool_calls=list(calls or []),
        iterations=len(calls or []),
        llm_calls=1,
        hit_cap=False,
        timings=[],
        ctx=_ctx(),
    )


# --- 쓰기의 결과: LLM 이 아니라 코드가 기록으로 말한다 ---


def test_도구를_안_불렀는데_담았다고_하면_지우고_남는_게_없어_422다() -> None:
    # 6단계 탐침의 실제 사고: 도구를 한 번도 안 부르고 "바꿨고 담아드렸습니다"라고 답함
    with pytest.raises(GroundingFailed) as exc_info:
        finish(
            _run("장바구니 1번째 책 수량을 2권으로 바꿨고, 카드 2번도 담아드렸습니다.")
        )

    assert exc_info.value.removed == ["ACTION_NOT_DONE"]


@pytest.mark.parametrize(
    "text",
    [
        "담았어요.",
        "담았습니다.",
        "장바구니에 담아 드렸어요.",
        "담아드렸습니다.",
        "장바구니에 추가했어요.",
        "넣었어요.",
        "넣어 드렸습니다.",
    ],
)
def test_담기_성공이_있으면_LLM의_완료_표현은_코드가_만든_문장으로_바뀐다(text) -> None:
    finished = finish(_run(text, [_add()]))

    assert finished.data.reply == "1번 책을 장바구니에 담았어요."
    assert finished.removed == ["ACTION_RESTATED"]


@pytest.mark.parametrize(
    "text",
    [
        "추가되었습니다.",
        "담겼어요.",
        "추가됐어요.",
        "담기 완료했습니다.",
        "넣어 놨어요.",
        "담아 뒀어요.",
        "장바구니에 추가가 완료되었어요.",
    ],
)
def test_말투가_달라도_완료_표현은_코드의_문장으로_바뀐다(text) -> None:
    # 리뷰에서 "추가되었습니다" 같은 말이 목록에 없어 그대로 나간 것을 확인했다
    finished = finish(_run(text, [_add()]))

    assert finished.data.reply == "1번 책을 장바구니에 담았어요."


@pytest.mark.parametrize(
    "text",
    [
        "추가되었습니다.",
        "담겼어요.",
        "추가됐어요.",
        "담기 완료했습니다.",
        "넣어 놨어요.",
        "담아 뒀어요.",
    ],
)
def test_하지_않은_담기를_이런_말투로_해도_거짓_완료로_잡는다(text) -> None:
    with pytest.raises(GroundingFailed) as exc_info:
        finish(_run(text, []))

    assert exc_info.value.removed == ["ACTION_NOT_DONE"]


@pytest.mark.parametrize(
    "text",
    ["삭제되었습니다.", "빠졌어요.", "제거 완료했어요.", "지워 드렸어요."],
)
def test_하지_않은_빼기를_이런_말투로_해도_거짓_완료로_잡는다(text) -> None:
    with pytest.raises(GroundingFailed):
        finish(_run(text, []))


@pytest.mark.parametrize(
    "text",
    ["변경되었습니다.", "수정했어요.", "바뀌었어요.", "수량을 늘렸어요."],
)
def test_하지_않은_수량_변경을_이런_말투로_해도_거짓_완료로_잡는다(text) -> None:
    with pytest.raises(GroundingFailed):
        finish(_run(text, []))


def test_담기가_실패했는데_담았다고_하면_지우고_실패를_코드가_말한다() -> None:
    finished = finish(_run("담았어요.", [_add(ok=False)]))

    # 담았다는 거짓말이 지워져도 422 가 아니다. 무슨 일이 있었는지 코드가 알린다
    assert finished.data.reply == "1번 책은 재고가 부족해서 담지 못했어요."
    assert finished.removed == ["ACTION_NOT_DONE"]


def test_담기_실패의_이유를_LLM이_함께_말하면_그_문장은_남는다() -> None:
    finished = finish(
        _run(
            "담았어요. 다만 재고가 부족해서 실제로는 담기지 않았어요.", [_add(ok=False)]
        )
    )

    assert finished.data.reply == (
        "1번 책은 재고가 부족해서 담지 못했어요.\n"
        "다만 재고가 부족해서 실제로는 담기지 않았어요."
    )
    assert finished.removed == ["ACTION_NOT_DONE"]


def test_거짓_완료로_지운_문장과_관련된_실패한_기록은_grounded가_false다() -> None:
    failed = _add(ok=False)

    finished = finish(_run("담았어요. 재고가 부족해요.", [failed]))

    assert finished.data.tool_calls[0].grounded is False


def test_성공한_기록은_grounded가_그대로_true다() -> None:
    finished = finish(_run("담았어요.", [_add()]))

    assert finished.data.tool_calls[0].grounded is True


def test_빼기는_빼기_성공이_있어야_한다() -> None:
    assert (
        finish(_run("뺐어요.", [_remove()])).data.reply
        == "《책3》을 장바구니에서 뺐어요."
    )
    # 담기가 성공했다고 빼기가 되지는 않는다. 안 한 빼기를 했다는 말은 지우고 담은 것만 말한다
    finished = finish(_run("뺐어요.", [_add()]))
    assert finished.data.reply == "1번 책을 장바구니에 담았어요."
    assert finished.removed == ["ACTION_NOT_DONE"]


def test_수량_변경은_수량_변경_성공이_있어야_한다() -> None:
    ok = finish(_run("수량을 바꿨어요.", [_update()]))
    assert ok.data.reply == "《책3》의 수량을 2권으로 바꿨어요."

    failed = finish(_run("수량을 바꿨어요.", [_update(ok=False)]))
    assert failed.data.reply == "수량이 올바르지 않아 《책3》의 수량을 바꾸지 못했어요."
    assert failed.removed == ["ACTION_NOT_DONE"]


def test_한_문장에_두_가지를_했다고_하면_둘_다_성공해야_한다() -> None:
    both = finish(_run("1번을 담고 장바구니의 책은 뺐어요.", [_add(), _remove()]))
    assert both.removed == ["ACTION_RESTATED"]
    assert both.data.reply == (
        "1번 책을 장바구니에 담았어요. 《책3》을 장바구니에서 뺐어요."
    )

    one = finish(_run("1번을 담고 장바구니의 책은 뺐어요.", [_add()]))
    assert one.removed == ["ACTION_NOT_DONE"]
    assert one.data.reply == "1번 책을 장바구니에 담았어요."


@pytest.mark.parametrize(
    "text",
    [
        "재고가 부족해서 담지 못했어요.",
        "이미 장바구니에 담겨 있어요.",
        "지금은 담을 수 없어요.",
        "담을까요?",
        "담았나요?",
        "못 담았어요.",
        "안 담았어요.",
        "재고가 없어서 못 담아 드렸어요.",
        "못 뺐어요.",
        "아직 수량을 못 바꿨어요.",
        "추가하지 못했어요.",
        "담지 않았어요.",
        "로그인하셔야 담을 수 있어요.",
        "담아 드릴게요.",
        "장바구니에 담긴 책은 2권이에요.",
        "추가 배송비는 없어요.",
        "추가로 궁금한 점이 있으세요?",
        # 실제 모델이 조합을 고른 뒤 한 안내(#381 측정). 눌러 주면 담아 주겠다는 말이지 담았다는 말이 아니다
        "아래 담기 버튼을 누르시면 장바구니에 담아 드립니다.",
        "원하시면 바로 담아 드립니다.",
        "확인하시면 장바구니에 넣어 드립니다.",
        "버튼을 누르시면 수량을 바꿔 드립니다.",
        "버튼을 누르시면 장바구니에서 빼 드립니다.",
    ],
)
def test_완료_표현이_아닌_말은_도구가_없어도_지우지_않는다(text) -> None:
    finished = finish(_run(text, []))

    assert finished.data.reply == text
    assert finished.removed == []


def test_행동_검사는_문장_단위로_지운다() -> None:
    finished = finish(_run("뺐어요. 장바구니에는 책이 한 권 남아 있어요.", [_add()]))

    assert finished.data.reply == (
        "1번 책을 장바구니에 담았어요.\n장바구니에는 책이 한 권 남아 있어요."
    )
    assert finished.removed == ["ACTION_NOT_DONE"]


def test_일부만_성공하면_어느_책이_됐고_안_됐는지_코드가_말한다() -> None:
    # 리뷰에서 한 권만 담겼는데 "두 권 모두 담았어요"가 통과한 것을 확인했다
    already = _call(
        ToolName.CART_ADD,
        {"ok": False, "error": "ALREADY_IN_CART", "title": "나무", "quantity": 1},
        {"isbn": A, "quantity": 1},
    )

    finished = finish(_run("두 권 모두 장바구니에 담았어요.", [already, _add(isbn=B)]))

    assert finished.data.reply == (
        "《나무》는 이미 장바구니에 1권 담겨 있어서 그대로 뒀어요. "
        "2번 책을 장바구니에 담았어요."
    )
    assert "두 권 모두" not in finished.data.reply


def test_쓰기가_성공한_턴은_LLM이_합계만_말해도_422가_아니다() -> None:
    # 리뷰에서 담기가 성공했는데 답이 "합계는 99,000원입니다." 한 문장이라 지워져 422 가 난 것을 확인했다
    finished = finish(_run("합계는 99,000원입니다.", [_view(total=37000), _add()]))

    assert finished.data.reply == "《책》을 장바구니에 담았어요."
    assert finished.removed == ["TOTAL_MISMATCH"]


def test_쓰기가_성공한_턴은_LLM이_빈_글이어도_안내_문장이_아니라_결과를_말한다() -> (
    None
):
    finished = finish(_run("", [_add()]))

    assert finished.data.reply == "1번 책을 장바구니에 담았어요."
    assert finished.data.reply != FALLBACK_REPLY


def test_수량이_2권_이상이면_권수를_말한다() -> None:
    call = _call(ToolName.CART_ADD, {"ok": True}, {"isbn": A, "quantity": 3})

    assert finish(_run("", [call])).data.reply == "1번 책 3권을 장바구니에 담았어요."


def test_같은_책을_여러_번_시도하면_마지막_시도만_말한다() -> None:
    finished = finish(_run("", [_add(ok=False), _add()]))

    assert finished.data.reply == "1번 책을 장바구니에 담았어요."


def test_로그인하지_않아_막힌_쓰기는_로그인을_안내한다() -> None:
    blocked = _call(
        ToolName.CART_ADD,
        {"ok": False, "error": "LOGIN_REQUIRED"},
        {"book": {"source": "card", "n": 1}},
    )

    finished = finish(_run("", [blocked]))

    assert finished.data.reply == LOGIN_REPLY


def test_책을_풀기_전에_막힌_호출은_결과_문장을_만들지_않는다() -> None:
    # 이 호출의 arguments 는 LLM 이 준 값 그대로다. 거기 든 isbn 을 믿고 말하면 안 된다
    blocked = _call(
        ToolName.CART_ADD,
        {"ok": False, "error": "BOOK_NOT_IN_CARDS"},
        {"book": {"source": "card", "n": 9}, "isbn": "9780000000000"},
    )

    finished = finish(_run("어느 책 말씀이신가요?", [blocked]))

    assert finished.data.reply == "어느 책 말씀이신가요?"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("나무", "《나무》를 장바구니에 담았어요."),
        ("책", "《책》을 장바구니에 담았어요."),
        ("2024", "《2024》를 장바구니에 담았어요."),  # 이천이십사 -> 사
        ("Python", "《Python》을(를) 장바구니에 담았어요."),
    ],
)
def test_조사는_제목의_받침에_따라_고른다(title, expected) -> None:
    offer = _call(
        ToolName.BOOK_OFFER,
        {"offers": [{"isbn": A, "title": title, "price": 1, "in_stock": True}]},
        {"isbns": [A]},
    )

    assert finish(_run("", [offer, _add()])).data.reply == expected


# --- 책 제목 속의 글자는 검사에 걸리지 않는다 ---


@pytest.mark.parametrize(
    "title",
    [
        "총, 균, 쇠",
        "권총의 역사",
        "총액의 비밀",
        "5000원으로 시작하는 주식",
        "담았어요",
    ],
)
def test_제목에_합계나_금액이나_완료_표현이_든_책의_가격을_말해도_지우지_않는다(
    title,
) -> None:
    # 리뷰에서 《총, 균, 쇠》는 22,000원이 합계 주장으로 읽혀 지워지고 422 가 난 것을 확인했다
    offer = _call(
        ToolName.BOOK_OFFER,
        {"offers": [{"isbn": A, "title": title, "price": 22000, "in_stock": True}]},
        {"isbns": [A]},
    )
    text = f"《{title}》는 22,000원이고 재고가 있어요."

    finished = finish(_run(text, [offer]))

    assert finished.data.reply == text  # 사용자에게는 가리지 않은 글이 간다
    assert finished.removed == []


def test_따옴표가_없어도_도구가_알려_준_제목은_가린다() -> None:
    offer = _call(
        ToolName.BOOK_OFFER,
        {
            "offers": [
                {"isbn": A, "title": "권총의 역사", "price": 15000, "in_stock": True}
            ]
        },
        {"isbns": [A]},
    )

    finished = finish(_run("권총의 역사는 15,000원이에요.", [offer]))

    assert finished.removed == []


def test_제목을_가려도_진짜_합계_거짓은_잡는다() -> None:
    offer = _call(
        ToolName.BOOK_OFFER,
        {
            "offers": [
                {"isbn": A, "title": "총, 균, 쇠", "price": 22000, "in_stock": True}
            ]
        },
        {"isbns": [A]},
    )

    with pytest.raises(GroundingFailed):
        finish(
            _run("《총, 균, 쇠》 합계는 99,000원이에요.", [_view(total=37000), offer])
        )


# --- 숫자 검사: 합계는 마지막 조회의 total 이어야 한다 ---


def test_합계를_지어내면_지운다() -> None:
    # 6단계 탐침의 실제 사고: 합계를 15,000원이라고 답함(실제 37,000원). 15,000은 가격 조회 결과에 있어서
    # "도구 결과에 있는 숫자인가"만 보면 못 잡는다
    finished_calls = [_view(total=37000), _offer(15000)]

    finished = finish(
        _run("합계는 15,000원입니다. 장바구니를 확인해 보세요.", finished_calls)
    )

    assert finished.data.reply == "장바구니를 확인해 보세요."
    assert finished.removed == ["TOTAL_MISMATCH"]
    assert finished.data.tool_calls[0].grounded is False  # 합계를 뒷받침하지 못한 조회


def test_맞는_합계는_남는다() -> None:
    finished = finish(_run("장바구니 합계는 37,000원입니다.", [_view(total=37000)]))

    assert finished.data.reply == "장바구니 합계는 37,000원입니다."
    assert finished.data.tool_calls[0].grounded is True


@pytest.mark.parametrize("word", ["합계", "총액", "총합", "총 금액", "전체 금액", "총"])
def test_합계를_뜻하는_여러_말을_같게_본다(word) -> None:
    with pytest.raises(GroundingFailed) as exc_info:
        finish(_run(f"{word} 99,000원이에요.", [_view(total=37000)]))
    assert exc_info.value.removed == ["TOTAL_MISMATCH"]
    assert finish(_run(f"{word} 37,000원이에요.", [_view(total=37000)])).removed == []


def test_조회하지_않고_합계를_말하면_지운다() -> None:
    with pytest.raises(GroundingFailed):
        finish(_run("합계는 37,000원입니다.", []))


def test_조회_뒤에_쓰기가_성공했으면_그_합계는_인정하지_않는다() -> None:
    # 담고 나면 합계가 바뀐다. 다시 조회하지 않고 말한 합계는 낡은 값이다. 담았다는 말은 맞아서 남는다
    finished = finish(
        _run("담았어요. 합계는 37,000원입니다.", [_view(total=37000), _add()])
    )

    assert finished.data.reply == "《책》을 장바구니에 담았어요."
    assert finished.removed == ["ACTION_RESTATED", "TOTAL_MISMATCH"]


def test_쓰기가_실패했으면_조회한_합계는_여전히_맞다() -> None:
    finished = finish(
        _run(
            "담지 못했어요. 합계는 37,000원 그대로예요.",
            [_view(total=37000), _add(ok=False)],
        )
    )

    assert finished.removed == []


def test_쓰기_뒤에_다시_조회했으면_새_합계를_인정한다() -> None:
    calls = [_view(total=22000, items=[(22000, 1)]), _add(), _view(total=37000)]

    finished = finish(_run("담았어요. 합계는 37,000원입니다.", calls))

    assert finished.removed == ["ACTION_RESTATED"]
    assert finished.data.reply == (
        "《책》을 장바구니에 담았어요.\n합계는 37,000원입니다."
    )


def test_빈_장바구니의_합계_0원은_맞는_말이다() -> None:
    finished = finish(
        _run("장바구니가 비어 있어요. 합계는 0원이에요.", [_view(total=0, items=[])])
    )

    assert finished.removed == []


def test_합계가_0원이_아니라는_말도_근거가_없으면_지운다() -> None:
    # 거짓 합계를 말하라는 요청에 대한 6단계 탐침 답. 숫자를 언급하는 문장은 기록과 대조한다
    with pytest.raises(GroundingFailed):
        finish(_run("합계는 0원이 아닙니다.", [_view(total=37000)]))


# --- 숫자 검사: 그 밖의 금액은 도구 결과에 있는 값이어야 한다 ---


def test_가격_조회_결과의_가격은_말할_수_있다() -> None:
    finished = finish(
        _run("1번은 15,000원, 2번은 22,000원이에요.", [_offer(15000, 22000)])
    )

    assert finished.removed == []


def test_가격_곱하기_수량도_말할_수_있다() -> None:
    finished = finish(
        _run(
            "15,000원짜리 2권이라 30,000원이에요.",
            [_view(total=30000, items=[(15000, 2)])],
        )
    )

    assert finished.removed == []


def test_결과에_없는_금액은_지운다() -> None:
    with pytest.raises(GroundingFailed):
        finish(_run("1번은 99,000원이에요.", [_offer(15000, 22000)]))


def test_금액을_말하는데_도구를_안_불렀으면_지운다() -> None:
    with pytest.raises(GroundingFailed):
        finish(_run("그 책은 15,000원이에요.", []))


def test_차이를_말하는_문장은_두_가격의_차이를_인정한다() -> None:
    ok = finish(
        _run("가격은 700원 차이예요(13,500원, 12,800원).", [_offer(13500, 12800)])
    )
    assert ok.removed == []

    # 차이를 말하는 문장이 아니면 700원은 결과에 없는 숫자다
    with pytest.raises(GroundingFailed):
        finish(_run("그 책은 700원이에요.", [_offer(13500, 12800)]))


def test_합계_말_뒤에_숫자가_끼면_합계_주장이_아니라_일반_금액으로_본다() -> None:
    # "총 2권, 52,000원"은 합계 주장이 아니다. 52,000원이 결과에 있으니 남는다
    finished = finish(
        _run(
            "총 2권, 52,000원이에요.",
            [_view(total=52000, items=[(30000, 1), (22000, 1)])],
        )
    )

    assert finished.removed == []


def test_원이_붙지_않은_숫자는_검사하지_않는다() -> None:
    finished = finish(_run("장바구니에 2권이 있고 3번 카드는 81점이에요.", []))

    assert finished.removed == []


def test_사용자에게_묻는_문장의_금액은_주장이_아니다() -> None:
    finished = finish(_run("5,000원 더 담으면 무료배송인데 담을까요?", []))

    assert finished.removed == []


# --- 응답 조립 ---


def test_쇼핑_아님이면_아무것도_안_한_응답이다() -> None:
    finished = finish(_run(NOT_SHOPPING, [], not_shopping=True))

    assert finished.data == AgentActData.not_shopping_reply()
    assert finished.data.reply == ""


def test_도구를_부른_뒤에_나온_약속어는_사용자에게_보이지_않는다() -> None:
    finished = finish(_run(NOT_SHOPPING, [_view()]))

    assert finished.data.reply == FALLBACK_REPLY
    assert NOT_SHOPPING not in finished.data.reply
    assert finished.data.not_shopping is False


def test_LLM이_빈_글로_끝나면_안내_문장을_쓴다() -> None:
    finished = finish(_run("", [_view()]))

    assert finished.data.reply == FALLBACK_REPLY
    assert finished.removed == []


def test_마크다운_강조는_벗긴다() -> None:
    finished = finish(
        _run(
            "장바구니에 **「처음 만나는 파이썬」** 이 있어요. `cart_view` 결과예요.", []
        )
    )

    assert (
        finished.data.reply
        == "장바구니에 「처음 만나는 파이썬」 이 있어요. cart_view 결과예요."
    )


def test_줄_구조를_지키고_지운_문장의_줄만_사라진다() -> None:
    text = "장바구니 목록이에요.\n1. 책1 15,000원\n2. 책2 22,000원\n담았어요."

    finished = finish(_run(text, [_view(total=37000)]))

    assert (
        finished.data.reply == "장바구니 목록이에요.\n1. 책1 15,000원\n2. 책2 22,000원"
    )


def test_응답_데이터는_JSON으로_내보낼_수_있고_모양이_맞다() -> None:
    finished = finish(_run("담았어요.", [_view(), _add()]))

    dumped = finished.data.model_dump(mode="json")

    json.dumps(dumped, ensure_ascii=False)
    assert AgentActData.model_validate(dumped) == finished.data
    assert dumped["selection"] is None
    assert dumped["not_shopping"] is False


# --- 풀어 낸 책 ---


def test_담은_책은_카드_번호로_적는다() -> None:
    finished = finish(_run("담았어요.", [_add(isbn=B)]))

    reference = finished.data.resolved_reference
    assert reference is not None
    assert reference.isbns == [B]
    assert reference.ref == "카드 2번"


def test_여러_책이면_차례로_적고_중복은_한_번만이다() -> None:
    finished = finish(_run("확인했어요.", [_offer(1, 2), _add(isbn=A)]))

    reference = finished.data.resolved_reference
    assert reference is not None
    assert reference.isbns == [A, B]
    assert reference.ref == "카드 1번, 카드 2번"


def test_책을_푼_도구가_없으면_null이다() -> None:
    assert finish(_run("확인했어요.", [_view()])).data.resolved_reference is None


def test_책을_풀기_전에_막힌_호출의_isbn은_믿지_않는다() -> None:
    # LLM 이 인자에 끼워 넣은 isbn 이 기록에 raw 로 남아 있다. 요청 안의 책이 아니면 resolved_reference 에 안 넣는다
    injected = _call(
        ToolName.CART_ADD,
        {"ok": False, "error": "INVALID_ARGUMENTS"},
        {"book": {"source": "card", "n": 1}, "isbn": "9780000000000"},
    )

    assert finish(_run("못 담았어요.", [injected])).data.resolved_reference is None


def test_요청_밖의_isbn이_결과에_있어도_resolved_reference에_넣지_않는다() -> None:
    outsider = _call(
        ToolName.CART_REMOVE,
        {"ok": False, "error": "LOGIN_REQUIRED"},
        {"isbn": "9789999999999"},
    )

    assert finish(_run("못 뺐어요.", [outsider])).data.resolved_reference is None


# --- 버튼 ---


def _targets(finished) -> list[tuple[str, str]]:
    return [(b.label, b.target) for b in finished.data.buttons]


def test_담기에_성공하면_장바구니_보기_버튼을_붙인다() -> None:
    assert _targets(finish(_run("담았어요.", [_add()]))) == [
        ("장바구니 보기", "CART-001")
    ]


def test_이미_담긴_책이라고_알릴_때도_장바구니_보기_버튼을_붙인다() -> None:
    already = _call(
        ToolName.CART_ADD,
        {"ok": False, "error": "ALREADY_IN_CART", "title": "책1", "quantity": 2},
        {"isbn": A, "quantity": 1},
    )

    assert _targets(finish(_run("책1은 이미 2권 담겨 있어요.", [already]))) == [
        ("장바구니 보기", "CART-001")
    ]


def test_장바구니를_조회했을_때도_버튼을_붙인다() -> None:
    assert _targets(finish(_run("장바구니예요.", [_view()]))) == [
        ("장바구니 보기", "CART-001")
    ]


def test_버튼은_한_번만_붙인다() -> None:
    finished = finish(_run("담았어요.", [_view(), _add(), _remove()]))

    assert _targets(finished) == [("장바구니 보기", "CART-001")]


def test_장바구니를_안_건드렸으면_버튼이_없다() -> None:
    assert finish(_run("가격은 15,000원이에요.", [_offer(15000)])).data.buttons == []
    assert finish(_run("그건 도와드리기 어려워요.", [])).data.buttons == []


def test_실패한_쓰기만_있으면_버튼을_붙이지_않는다() -> None:
    assert finish(_run("재고가 부족해요.", [_add(ok=False)])).data.buttons == []


def test_화면_안내는_이동_버튼으로_바뀐다() -> None:
    guide = _call(
        ToolName.UI_GUIDE,
        {"ok": True, "label": "주문내역"},
        {"target": "ORDER-LIST-001"},
    )

    finished = finish(_run("주문내역에서 확인하실 수 있어요.", [guide]))

    assert _targets(finished) == [("주문내역", "ORDER-LIST-001")]
    assert finished.data.buttons[0].action == "navigate"


def test_거절된_화면_안내는_버튼을_만들지_않는다() -> None:
    bad = _call(
        ToolName.UI_GUIDE,
        {"ok": False, "error": "INVALID_ARGUMENTS"},
        {"target": "HOME-001"},
    )

    assert finish(_run("안내해 드릴게요.", [bad])).data.buttons == []


def test_화면_안내와_장바구니_버튼이_겹치면_한_번만이다() -> None:
    guide = _call(
        ToolName.UI_GUIDE,
        {"ok": True, "label": "장바구니 보기"},
        {"target": "CART-001"},
    )

    finished = finish(_run("담았어요.", [_add(), guide]))

    assert _targets(finished) == [("장바구니 보기", "CART-001")]
