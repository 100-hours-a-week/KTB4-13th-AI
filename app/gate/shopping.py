"""챗봇 요청에 실린 쇼핑 칸을 ⑦ 요청으로 바꾼다(#413).

``POST /recommendations/chat`` 은 ③ 의 요청에 쇼핑 칸을 선택으로 더 받는다. ``idempotency_key`` 가 있으면
문지기를 켠다. 없으면 이 변경 전과 똑같이 ③만 하고 응답 모양도 그대로다(BE 가 쇼핑 칸을 보내기 전에 AI 가 먼저
배포되어도 안전하다).

쇼핑 칸은 갈래가 ``shop`` 일 때만 검사한다. ③으로 가는 요청이 쇼핑 칸이 잘못됐다는 이유로 실패하지 않게 한다.
"""

from typing import Any

from app.agent.schemas import AgentActRequest, parse_request

# ⑦ 요청의 칸. ③ 요청과 겹치는 user_id, message, recent_turns 는 같은 값을 쓴다
SHOP_KEYS = (
    "user_id",
    "conversation_id",
    "message",
    "context_cards",
    "focused_isbn",
    "recent_turns",
    "user_context",
    "allow_tools",
    "idempotency_key",
)


def enabled(payload: dict[str, Any]) -> bool:
    """쇼핑 칸을 보낸 요청인가. 문지기를 켤지 정한다."""
    return payload.get("idempotency_key") is not None


def cards_shown(payload: dict[str, Any]) -> bool:
    """직전에 카드가 보였나. 카드를 가리키는 말("1번")을 장바구니 신호로 볼지 정한다."""
    cards = payload.get("context_cards")
    return isinstance(cards, list) and len(cards) > 0


def agent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """챗봇 요청에서 ⑦ 요청의 칸만 뽑는다. 멱등 키의 본문 비교도 이 값으로 한다."""
    return {key: payload[key] for key in SHOP_KEYS if key in payload}


def to_agent_request(payload: dict[str, Any]) -> AgentActRequest | None:
    """⑦ 요청 검사를 그대로 거친다. 계약에 안 맞으면 None."""
    return parse_request(agent_payload(payload))
