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
    if (number := search_number(ctx, isbn)) is not None:
        return f"찾은 책 {number}번"
    return "목록 밖의 책"


def search_number(ctx: TurnContext, isbn: str) -> int | None:
    """찾은 책의 번호(이번 턴의 검색 전체에 이어 붙인 차례). 찾은 책이 아니면 None."""
    for position, book in enumerate(ctx.search_results, start=1):
        if book.isbn == isbn:
            return position
    return None


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

    if call.name == ToolName.BOOK_SEARCH and "books" in result:
        # 번호는 cart_add 가 {"source": "search", "n": 번호}로 가리킬 때 쓴다. 이번 턴의 검색 전체에 이어 붙인
        # 번호라, 두 번째 검색의 책은 앞 검색의 책 다음 번호부터다. 장바구니에 이미 있는 책이어도 찾은 책 번호로 보인다
        return {
            "books": [
                {
                    "book": f"찾은 책 {search_number(ctx, book['isbn'])}번",
                    "title": book["title"],
                    "author": book["author"],
                    "price": book["price"],
                    "in_stock": book["in_stock"],
                }
                for book in result["books"]
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
            # 골라서 담으라고 맡긴 말이면 코드가 이미 담았다(#631). 다시 cart_add 하지 않는다
            **(
                {
                    "added": [
                        {
                            "book": label(ctx, added["isbn"]),
                            **{k: v for k, v in added.items() if k != "isbn"},
                        }
                        for added in result["added"]
                    ]
                }
                if "added" in result
                else {}
            ),
        }

    # 쓰기 결과와 거절(ok, error, 이미 담긴 책의 title·quantity), 비교 요약에는 ISBN 이 없다
    return result
