"""검색 흐름 테스트 — 언제 합치고, 언제 키워드만으로 줄여 응답하는가.

DB·모델 없이 돈다. 키워드·벡터·임베딩을 전부 가짜로 바꿔 끼운다.
"""

import asyncio
from contextlib import asynccontextmanager

import asyncpg
import pytest

from app.search import service
from app.search.schemas import SearchRequest


class _FakePool:
    @asynccontextmanager
    async def acquire(self):
        yield object()


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> dict:
    """기본 상태: 키워드 [1, 2], 벡터 [2, 3], 임베딩 정상, 책 벡터 있음."""
    state = {"keyword": [1, 2], "vector": [2, 3], "has_any": True}

    async def _embed(texts, purpose):
        assert purpose == "query"
        return [[0.0] * 384], 384, "test-model"

    async def _keyword(conn, query, filters):
        return state["keyword"]

    async def _vector(conn, query_vector, filters):
        if isinstance(state["vector"], Exception):
            raise state["vector"]
        return state["vector"]

    async def _has_any(conn):
        return state["has_any"]

    async def _fetch(conn, ids):
        return [{"book_id": i} for i in ids]

    monkeypatch.setattr(service.db, "get_pool", lambda: _FakePool())
    monkeypatch.setattr(service.embedding, "embed", _embed)
    monkeypatch.setattr(service.keyword, "search_ids", _keyword)
    monkeypatch.setattr(service.vector, "search_ids", _vector)
    monkeypatch.setattr(service.vector, "has_any", _has_any)

    async def _sort(conn, ids, sort):
        state["sorted"] = (list(ids), sort)
        return sorted(ids, reverse=True)

    monkeypatch.setattr(service.books, "fetch", _fetch)
    monkeypatch.setattr(service.sorting, "sort_ids", _sort)
    return state


def _search(**kwargs) -> service.SearchOutcome:
    return asyncio.run(service.search(SearchRequest(query="책", **kwargs)))


def _ids(outcome: service.SearchOutcome) -> list[int]:
    return [r["book_id"] for r in outcome.results]


def test_둘_다_되면_합치고_헤더가_없다(fakes: dict) -> None:
    outcome = _search()

    # 2 는 두 목록에 다 있어 맨 위, 1 은 키워드 1등(비중 3), 3 은 벡터에만 있다.
    assert _ids(outcome) == [2, 1, 3]
    assert outcome.degraded is None


def test_size_만큼만_돌려준다(fakes: dict) -> None:
    assert _ids(_search(size=2)) == [2, 1]


def test_임베딩이_실패하면_키워드_결과만_주고_알린다(
    fakes: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fail(texts, purpose):
        raise RuntimeError("모델 없음")

    monkeypatch.setattr(service.embedding, "embed", _fail)

    outcome = _search()

    assert _ids(outcome) == [1, 2]
    assert outcome.degraded == "keyword-only"


@pytest.mark.parametrize(
    "error",
    [
        asyncpg.PostgresError("색인 이상"),
        # 아래 둘은 PostgresError 하위가 아니다. 좁게 잡으면 키워드 결과가 멀쩡한데도 500이 된다.
        asyncpg.InterfaceError("연결이 끊김"),
        TimeoutError("command_timeout 초과"),
    ],
)
def test_벡터_검색이_어떤_이유로_실패해도_키워드_결과는_준다(
    fakes: dict, error: Exception
) -> None:
    fakes["vector"] = error

    outcome = _search()

    assert _ids(outcome) == [1, 2]
    assert outcome.degraded == "keyword-only"


def test_책_벡터가_아직_한_권도_없으면_줄여_응답한_것이다(fakes: dict) -> None:
    fakes["vector"], fakes["has_any"] = [], False

    assert _search().degraded == "keyword-only"


def test_필터_때문에_벡터_결과가_0건인_것은_정상_응답이다(fakes: dict) -> None:
    fakes["vector"], fakes["has_any"] = [], True

    outcome = _search()

    assert _ids(outcome) == [1, 2]
    assert outcome.degraded is None


def test_키워드_검색이_실패하면_예외가_올라간다(fakes: dict, monkeypatch) -> None:
    async def _fail(conn, query, filters):
        raise asyncpg.PostgresError("DB 끊김")

    monkeypatch.setattr(service.keyword, "search_ids", _fail)

    with pytest.raises(asyncpg.PostgresError):
        _search()


def test_관련도순이면_정렬을_부르지_않는다(fakes: dict) -> None:
    _search()

    assert "sorted" not in fakes


def test_다른_정렬이면_합친_후보를_넘겨_다시_줄_세운다(fakes: dict) -> None:
    outcome = _search(sort="price_asc")

    assert fakes["sorted"] == ([2, 1, 3], "price_asc")
    assert _ids(outcome) == [3, 2, 1]


def test_다른_정렬이면_벡터_쪽은_앞의_20권만_후보에_넣는다(fakes: dict) -> None:
    fakes["keyword"], fakes["vector"] = [], list(range(100, 150))

    _search(sort="newest", size=50)

    assert fakes["sorted"][0] == list(range(100, 120))


def test_키워드만으로_줄여_응답할_때는_정렬_후보를_자르지_않는다(fakes: dict) -> None:
    # 20권 제한은 관련이 약한 벡터 결과를 막으려는 것이라, 키워드 결과에는 걸지 않는다.
    fakes["keyword"] = list(range(100, 150))
    fakes["vector"] = asyncpg.InterfaceError("연결이 끊김")

    outcome = _search(sort="price_asc", size=50)

    assert fakes["sorted"] == (list(range(100, 150)), "price_asc")
    assert outcome.degraded == "keyword-only"
