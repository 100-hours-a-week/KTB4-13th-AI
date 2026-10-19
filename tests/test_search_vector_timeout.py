"""벡터 질의 설정 테스트(시간 제한, 탐색 폭). DB 없이 가짜 연결로 넘기는 값만 본다."""

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
        self.executed: list[object] = []

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    async def execute(self, *args: object) -> None:
        self.executed.extend(args)

    async def fetch(self, *args: object, **kwargs: Any) -> list[dict[str, int]]:
        self.fetch_kwargs = kwargs
        return [{"book_id": 1}]


def test_벡터_질의는_연결_기본값보다_짧은_시간_제한을_건다() -> None:
    conn = _FakeConn()

    ids = asyncio.run(vector.search_ids(conn, [0.0] * 384, SearchFilters()))

    assert ids == [1]
    assert conn.fetch_kwargs == {"timeout": vector.QUERY_TIMEOUT_SECONDS}


def test_검색은_홈_추천과_따로_둔_탐색_폭을_건다() -> None:
    conn = _FakeConn()

    asyncio.run(vector.search_ids(conn, [0.0] * 384, SearchFilters()))

    assert f"SET LOCAL hnsw.ef_search = {vector.SEARCH_EF_SEARCH}" in conn.executed
    # 검색은 400 으로 줄였고(#516), 홈 추천 더 보기는 1000 그대로다
    assert (vector.SEARCH_EF_SEARCH, vector.EF_SEARCH) == (400, 1000)
