"""③ 2단계(get_candidates) — ①(app.search.service)과 잇는 부분 테스트.

라우터 계약 테스트(tests/test_chat_router.py)는 get_candidates를 통째로 가짜로
바꿔 끼운다. 여기서는 그 안쪽 — spec에서 검색어를 어떻게 만들고, ①의 결과를
어떻게 후보로 다듬는지를 본다. ①(service.search)과 설명 조회(_fetch_descriptions)는
가짜로 바꿔 끼운다. DB·모델 없이 돈다.
"""

import asyncio

import pytest

from app.chat.schemas import Spec, SpecExact
from app.routers import chat
from app.search.schemas import SearchFilters, SearchRequest
from app.search.service import SearchOutcome


def _spec(**overrides) -> Spec:
    base = {
        "intent": "semantic",
        "exact": SpecExact(title=None, author=None, publisher=None),
        "filters": SearchFilters(),
        "semantic": None,
        "anchor_book": None,
        "exclude": [],
    }
    base.update(overrides)
    return Spec(**base)


def _book(book_id: int, **overrides) -> dict:
    book = {
        "book_id": book_id,
        "title": f"책{book_id}",
        "author": "작가",
        "publisher": "출판사",
        "price": 10000,
        "in_stock": True,
        "cover_url": f"https://example.com/{book_id}.jpg",
    }
    book.update(overrides)
    return book


def test_semantic_intent이면_semantic_문장을_검색어로_쓴다() -> None:
    spec = _spec(intent="semantic", semantic="비 오는 날 읽을 잔잔한 책")
    assert chat._query_text(spec) == "비 오는 날 읽을 잔잔한 책"


def test_exact_intent이면_제목_저자를_합친다() -> None:
    spec = _spec(
        intent="exact",
        exact=SpecExact(title="달러구트 꿈 백화점", author="이미예", publisher=None),
    )
    assert chat._query_text(spec) == "달러구트 꿈 백화점 이미예"


def test_exact_intent이어도_출판사는_검색어에서_뺀다() -> None:
    # ①은 출판사를 안 보고 제목·저자·소개글에서만 낱말을 찾는다. 검색어에
    # 섞으면 평균 커버리지만 깎여 정확한 책이 오히려 탈락한다(리뷰 지적).
    spec = _spec(
        intent="exact",
        exact=SpecExact(title="마음", author=None, publisher="현암사"),
    )
    assert chat._query_text(spec) == "마음"


def test_exact_intent인데_exact가_비어있으면_semantic으로_대체한다() -> None:
    spec = _spec(intent="exact", semantic="그래도 이건 있음")
    assert chat._query_text(spec) == "그래도 이건 있음"


def test_아무것도_없으면_빈_문자열() -> None:
    assert chat._query_text(_spec()) == ""


def test_검색어가_없으면_service_search를_안_부르고_빈_후보(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(req: SearchRequest):
        raise AssertionError("검색어 없으면 service.search를 부르면 안 됨")

    monkeypatch.setattr(chat.service, "search", _boom)

    result = asyncio.run(chat.get_candidates(_spec(), []))

    assert result == []


def test_semantic이면_소개글을_붙이고_소개글_없는_책은_뺀다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _search(req: SearchRequest) -> SearchOutcome:
        assert req.query == "비 오는 날 읽을 책"
        assert req.filters.category == "에세이"
        return SearchOutcome(results=[_book(1088), _book(2000)], degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        assert book_ids == [1088, 2000]
        return {1088: "잠든 사이 꿈을 사고파는 상점 이야기."}  # 2000은 설명 없음

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="semantic",
        semantic="비 오는 날 읽을 책",
        filters=SearchFilters(category="에세이"),
    )
    result = asyncio.run(chat.get_candidates(spec, []))

    # 2000은 소개글이 없어 후보에서 빠진다 — 3단계가 소개글만 근거로 카드를 쓴다.
    assert [c["book_id"] for c in result] == [1088]
    assert result[0]["description"] == "잠든 사이 꿈을 사고파는 상점 이야기."
    assert result[0]["price"] == 10000
    assert result[0]["cover_url"] == "https://example.com/1088.jpg"


def test_exact이면_소개글_없는_책도_후보에_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # exact는 분위기를 안 보므로, ①이 키워드로 찾아준 소개글 없는 책도 그대로 쓴다
    # (리뷰: "exact에서는 소개글 없는 책도 후보에 남기고, semantic일 때만 거르자").
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=[_book(1088), _book(2000)], degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {1088: "잠든 사이 꿈을 사고파는 상점 이야기."}  # 2000은 설명 없음

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="exact", exact=SpecExact(title="아무 제목", author=None, publisher=None)
    )
    result = asyncio.run(chat.get_candidates(spec, []))

    assert [c["book_id"] for c in result] == [1088, 2000]
    assert result[1]["description"] == ""


def test_소개글_없는_책은_CANDIDATE_LIMIT_자리를_안_먹는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """맨 앞 책이 소개글이 없어도, 그 뒤 소개글 있는 책으로 CANDIDATE_LIMIT까지 채워야 한다.

    자르기 전에 거르지 않으면(카드 생성 단계에서만 거르면) 이 자리가 그냥
    버려진다 — 실제로 재현된 문제(리뷰): 후보가 전부 소개글 없는 책이면
    카드 0장에 "골라봤어요"가 나감.
    """
    books = [_book(i) for i in range(1, chat.CANDIDATE_LIMIT + 2)]

    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=books, degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {
            book_id: "설명" for book_id in book_ids if book_id != 1
        }  # 1만 소개글 없음

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, []))

    assert len(result) == chat.CANDIDATE_LIMIT
    book_ids = [c["book_id"] for c in result]
    assert 1 not in book_ids
    assert chat.CANDIDATE_LIMIT + 1 in book_ids  # 밀려난 책이 빈 자리를 채운다


def test_exclude_book_ids와_spec_exclude를_합쳐서_거른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(
            results=[_book(1), _book(2), _book(3), _book(4)], degraded=None
        )

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(intent="semantic", semantic="아무거나", exclude=[2])
    result = asyncio.run(chat.get_candidates(spec, [3]))

    assert [c["book_id"] for c in result] == [1, 4]


def test_후보가_CANDIDATE_LIMIT보다_많으면_앞에서부터_자른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    books = [_book(i) for i in range(1, chat.CANDIDATE_LIMIT + 5)]

    async def _search(req: SearchRequest) -> SearchOutcome:
        assert req.size == chat.SEARCH_SIZE
        return SearchOutcome(results=books, degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, []))

    assert len(result) == chat.CANDIDATE_LIMIT
    assert [c["book_id"] for c in result] == list(range(1, chat.CANDIDATE_LIMIT + 1))
