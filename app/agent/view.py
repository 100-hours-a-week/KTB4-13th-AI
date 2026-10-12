"""⑦ 도구 결과를 LLM 에게 보여 줄 모양으로 바꾼다(#356).

기록(ToolCall.result)에는 ISBN 이 있지만 LLM 에게는 보여 주지 않는다. LLM 이 ISBN 을 모르면 지어낼 수도
없고, 책은 "카드 1번", "장바구니 2번째" 같은 번호로만 가리키게 된다. 제목·가격·수량처럼 답에 쓸 값은 그대로 준다.
"""

from typing import Any

from app.agent.reference import TurnContext
from app.agent.schemas import ToolCall, ToolName


def label(ctx: TurnContext, isbn: str) -> str:
    """책을 LLM 이 다시 가리킬 수 있는 말로. ISBN 을 말하지 않는다."""
    for card in ctx.cards:
        if card.isbn == isbn:
            return f"카드 {card.rank}번"
    for position, line in enumerate(ctx.cart_snapshot or [], start=1):
        if line.isbn == isbn:
            return f"장바구니 {position}번째"
    if ctx.focused_isbn == isbn:
        return "지금 보는 책"
    return "목록 밖의 책"


def llm_view(ctx: TurnContext, call: ToolCall) -> dict[str, Any]:
    result = call.result

    if call.name == ToolName.CART_VIEW and "items" in result:
        return {
            "items": [
                {
                    "n": position,
                    "title": item["title"],
                    "price": item["price"],
                    "quantity": item["quantity"],
                    "available": item["available"],
                }
                for position, item in enumerate(result["items"], start=1)
            ],
            "total": result["total"],
        }

    if call.name == ToolName.BOOK_OFFER and "offers" in result:
        return {
            "offers": [
                {
                    "book": label(ctx, offer["isbn"]),
                    "title": offer["title"],
                    "price": offer["price"],
                    "in_stock": offer["in_stock"],
                }
                for offer in result["offers"]
            ]
        }

    if call.name == ToolName.SELECTION_PICK and result.get("ok") is True:
        return {
            "ok": True,
            "chosen": [
                {
                    "book": label(ctx, book["isbn"]),
                    "title": book["title"],
                    "price": book["price"],
                }
                for book in result["chosen"]
            ],
            "total_price": result["total_price"],
            "alternatives_considered": result["alternatives_considered"],
        }

    # 쓰기 결과와 거절(ok, error, 이미 담긴 책의 title·quantity), 비교 요약에는 ISBN 이 없다
    return result
