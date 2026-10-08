"""벡터 질의 시간 제한 테스트. DB 없이 가짜 연결로 넘기는 값만 본다."""

import asyncio
from typing import Any

from app.search import vector
from app.search.schemas import SearchFilters


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.fetch_kwargs: dict[str, Any] = {}

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, *args: object) -> None:
        return None

    async def fetch(self, *args: object, **kwargs: Any) -> list[dict[str, int]]:
        self.fetch_kwargs = kwargs
        return [{"book_id": 1}]


def test_벡터_질의는_연결_기본값보다_짧은_시간_제한을_건다() -> None:
    conn = _FakeConn()

    ids = asyncio.run(vector.search_ids(conn, [0.0] * 384, SearchFilters()))

    assert ids == [1]
    assert conn.fetch_kwargs == {"timeout": vector.QUERY_TIMEOUT_SECONDS}
