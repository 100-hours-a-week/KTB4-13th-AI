"""⑦ 같은 idempotency_key 의 재시도에는 처음 응답을 돌려준다(#376).

BE 가 응답을 못 받고 같은 요청을 다시 보내면, 다시 처리하지 않고 저장한 응답을 200 으로 돌려준다. 다시 처리하면
LLM 을 또 부르고, 이미 담긴 책에는 "이미 담겨 있어요"라고 답해서 사용자가 처음 응답("담았어요")을 보지 못한다.
같은 키에 다른 본문이 오면 409 다(명세 ⑦). 공통 규약과 표는 app.core.idempotency 가 맡는다.

**기록소(DB)가 고장 나도 요청을 막지 않는다.** 조회나 저장이 실패하면 로그만 남기고 그대로 처리한다. BE 의
담기·수량 변경은 "값으로 바꾸기"라서 한 번 더 처리되어도 장바구니는 같고, 막는 쪽이 더 큰 장애다.
"""

import logging
from typing import Any

from app.core import db, idempotency
from app.core.idempotency import IdempotencyConflict

logger = logging.getLogger(__name__)

# ⑥ 과 같은 표를 쓰므로 키가 겹치지 않게 앞에 이름을 붙인다
SCOPE = "agent"

__all__ = ["IdempotencyConflict", "find", "key_of", "save"]


def key_of(idempotency_key: str) -> str:
    return idempotency.scoped_key(SCOPE, idempotency_key)


async def find(key: str, digest: str) -> dict[str, Any] | None:
    """저장한 응답. 처음 온 키이거나 기록소를 못 읽으면 None. 본문이 다르면 IdempotencyConflict."""
    try:
        async with db.get_pool().acquire() as conn:
            return await idempotency.lookup(conn, key, digest)
    except IdempotencyConflict:
        raise
    except Exception as e:  # noqa: BLE001 - 기록소 장애로 요청을 막지 않는다. 종류만 남긴다
        logger.warning(
            "⑦ 멱등 기록을 읽지 못했다(%s). 그대로 처리한다", type(e).__name__
        )
        return None


async def save(
    key: str, digest: str, response: dict[str, Any]
) -> dict[str, Any] | None:
    """응답을 저장한다. 같은 키가 먼저 저장되어 있으면(같은 요청이 동시에 처리됨) 그 응답을 돌려주고,
    본문이 다르면 IdempotencyConflict. 저장했거나 기록소를 못 쓰면 None."""
    try:
        async with db.get_pool().acquire() as conn:
            try:
                await idempotency.remember(conn, key, digest, response)
                return None
            except IdempotencyConflict:
                # 먼저 저장한 쪽이 있다. 본문이 같으면 그 응답이 정답이고, 다르면 lookup 이 충돌을 올린다
                return await idempotency.lookup(conn, key, digest)
    except IdempotencyConflict:
        raise
    except Exception as e:  # noqa: BLE001 - 응답은 이미 만들었다. 저장을 못 해도 돌려준다
        logger.warning(
            "⑦ 멱등 기록을 저장하지 못했다(%s). 응답은 그대로 보낸다", type(e).__name__
        )
        return None
