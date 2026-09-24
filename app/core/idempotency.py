"""멱등 키 — 같은 요청이 다시 와도 결과가 두 번 반영되지 않게 한다.

⑥ 취향 프로필과 ⑦ 쇼핑 에이전트가 같은 규약을 쓴다(명세). 같은 키·같은 본문이면 다시 처리하지
않고 저장한 응답을 200으로 돌려주고, 같은 키·다른 본문이면 409 다.

부르는 쪽이 트랜잭션을 열고 그 안에서 lookup → 처리 → remember 순으로 부른다. 처리 결과와
멱등 기록이 한 트랜잭션에 들어가야, 처리만 되고 기록이 빠지는(재시도 때 두 번 반영되는) 일이 없다.
"""

import hashlib
import json
from typing import Any

import asyncpg


class IdempotencyConflict(Exception):
    """같은 키에 다른 본문이 왔다(409)."""


def body_hash(payload: Any) -> str:
    """본문의 해시. 키 순서·공백을 맞춰 다시 적은 뒤 잰다.

    원문 그대로 재면 재시도 때 키 순서만 달라져도 다른 본문으로 보고 409 가 난다.
    """
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def scoped_key(scope: str, key: str) -> str:
    """테이블 키가 문자열 하나라 ⑥과 ⑦의 키가 겹칠 수 있다. 앞에 API 이름을 붙여 나눈다(#93)."""
    return f"{scope}:{key}"


async def lookup(
    conn: asyncpg.Connection, key: str, body_hash_: str
) -> dict[str, Any] | None:
    """저장한 응답이 있으면 돌려주고, 처음 온 키면 None. 본문이 다르면 IdempotencyConflict."""
    row = await conn.fetchrow(
        "SELECT body_hash, stored_response::text AS stored_response"
        " FROM idempotency_records WHERE idempotency_key = $1",
        key,
    )
    if row is None:
        return None
    if row["body_hash"] != body_hash_:
        raise IdempotencyConflict
    return json.loads(row["stored_response"])


async def remember(
    conn: asyncpg.Connection, key: str, body_hash_: str, response: dict[str, Any]
) -> None:
    """성공(200) 응답만 저장한다. 실패는 저장하지 않아 재시도하면 다시 처리한다."""
    try:
        await conn.execute(
            "INSERT INTO idempotency_records"
            " (idempotency_key, body_hash, stored_response, created_at)"
            " VALUES ($1, $2, $3::jsonb, now())",
            key,
            body_hash_,
            json.dumps(response, ensure_ascii=False),
        )
    except asyncpg.UniqueViolationError as exc:
        # 같은 키가 동시에 두 번 처리되다 다른 쪽이 먼저 기록했다. 같은 본문이면 같은 사용자라
        # 사용자별 잠금으로 이미 줄을 섰을 것이므로, 여기까지 온 건 본문이 다른 경우다.
        raise IdempotencyConflict from exc
