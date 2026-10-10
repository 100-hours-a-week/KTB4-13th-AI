"""③ 2단계(get_candidates) — ①(app.search.service)과 잇는 부분 테스트.

라우터 계약 테스트(tests/test_chat_router.py)는 get_candidates를 통째로 가짜로
바꿔 끼운다. 여기서는 그 안쪽 — spec에서 검색어를 어떻게 만들고, ①의 결과를
어떻게 후보로 다듬는지를 본다. ①(service.search)과 설명 조회(_fetch_descriptions)는
가짜로 바꿔 끼운다. DB·모델 없이 돈다.
"""

import asyncio
import contextlib

import pytest

from app.chat.schemas import Spec, SpecExact
from app.core import history
from app.routers import chat
from app.search.schemas import SearchFilters, SearchRequest
from app.search.service import SearchOutcome


@pytest.fixture(autouse=True)
def empty_history(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본값: 구매·저평점 이력 없음. 그 이력을 보는 테스트는 개별적으로 override한다."""

    async def _fake(user_id: int) -> history.History:
        return history.History()

    monkeypatch.setattr(chat, "_fetch_history", _fake)


@pytest.fixture(autouse=True)
def no_match_scores(monkeypatch: pytest.MonkeyPatch) -> None:
    """match_score 조회(_attach_match_scores)는 DB가 필요하다(tests/test_chat_scoring.py에서 본다).

    get_candidates()가 db.get_pool().acquire()로 연결을 연 뒤 그 conn을
    _attach_match_scores에 넘기므로, get_pool 자체도 가짜로 바꿔야 이
    테스트 파일의 "DB 없이 돈다"가 유지된다(app/feed/service.py 테스트와
    같은 방식 — _FakePool).
    """

    class _NoPool:
        def acquire(self):
            return contextlib.nullcontext()

    async def _noop(conn, candidates: list[dict], user_id: int, spec: Spec) -> None:
        pass

    monkeypatch.setattr(chat.db, "get_pool", lambda: _NoPool())
    monkeypatch.setattr(chat, "_attach_match_scores", _noop)


@pytest.fixture(autouse=True)
def no_shown_editions(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본값: 이미 보여 준 책의 판본 키 없음(DB 조회 안 함). 보는 테스트는 개별적으로 override한다."""

    async def _fake(book_ids: list[int]) -> set[tuple[str, str]]:
        return set()

    monkeypatch.setattr(chat, "_fetch_edition_keys", _fake)


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


def test_출판사만_있으면_출판사를_검색어로_쓴다() -> None:
    # 제목·저자·semantic이 다 비면, 출판사라도 검색어로 써야 service.search()까지
    # 가서 get_candidates()의 publisher 후처리 필터가 걸릴 기회가 생긴다(이슈 #137).
    spec = _spec(
        intent="exact", exact=SpecExact(title=None, author=None, publisher="현암사")
    )
    assert chat._query_text(spec) == "현암사"


def test_검색어가_없으면_service_search를_안_부르고_빈_후보(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(req: SearchRequest):
        raise AssertionError("검색어 없으면 service.search를 부르면 안 됨")

    monkeypatch.setattr(chat.service, "search", _boom)

    result = asyncio.run(chat.get_candidates(_spec(), [], 1))

    assert result == []


def test_semantic이면_소개글을_붙이고_소개글_없는_책은_뺀다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _search(req: SearchRequest) -> SearchOutcome:
        assert req.query == "비 오는 날 읽을 책"
        # 온보딩 대응표(#110)에 없는 값(이미 카탈로그 분류명)이면 그대로 ①에 넘긴다
        # — 장르 정규화 자체는 tests/test_chat_category.py가 따로 본다.
        assert req.filters.category == "한국문학"
        return SearchOutcome(results=[_book(1088), _book(2000)], degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        assert book_ids == [1088, 2000]
        return {1088: "잠든 사이 꿈을 사고파는 상점 이야기."}  # 2000은 설명 없음

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="semantic",
        semantic="비 오는 날 읽을 책",
        filters=SearchFilters(category="한국문학"),
    )
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    # 2000은 소개글이 없어 후보에서 빠진다 — 3단계가 소개글만 근거로 카드를 쓴다.
    assert [c["book_id"] for c in result] == [1088]
    assert result[0]["description"] == "잠든 사이 꿈을 사고파는 상점 이야기."
    assert result[0]["price"] == 10000
    assert result[0]["cover_url"] == "https://example.com/1088.jpg"


def test_온보딩_장르는_카탈로그_분류로_바꿔_거른다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#203 — filters.category에 온보딩 값("소설")이 그대로 오면 ①의 정확 일치

    필터는 카탈로그 분류명("한국문학" 등)과 안 맞아 0건이 된다. 대응표(#110)에
    있는 값이면 ①에는 category를 안 보내고(정확 일치를 걸면 처음부터 0건이라
    넓게 받아야 한다) 카탈로그 분류로 직접 거른다.
    """

    async def _search(req: SearchRequest) -> SearchOutcome:
        # "소설"은 ①에 안 넘어가야 한다 — 그대로 넘기면 정확 일치라 0건이 된다.
        assert req.filters.category is None
        return SearchOutcome(results=[_book(1088), _book(2000)], degraded=None)

    async def _categories(book_ids: list[int]) -> dict[int, str | None]:
        assert set(book_ids) == {1088, 2000}
        return {1088: "한국소설", 2000: "경제학"}  # 1088만 "소설" 대응표에 걸림

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {bid: "설명" for bid in book_ids}

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_categories", _categories)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="exact",
        exact=SpecExact(title="아무 제목", author=None, publisher=None),
        filters=SearchFilters(category="소설"),
    )
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1088]


def test_대응표에_없는_카테고리는_예전처럼_정확_일치로_넘긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """대응표에 없는 값(이미 카탈로그 분류명이거나 모르는 값)은 그대로 ①에 보낸다

    (categories.py: "여기 없는 값은 쓰는 쪽에서 건너뛴다") — 카탈로그 조회를
    새로 안 하고 예전 동작을 그대로 유지한다.
    """

    async def _search(req: SearchRequest) -> SearchOutcome:
        assert req.filters.category == "한국문학"
        return SearchOutcome(results=[_book(1088)], degraded=None)

    async def _must_not_run(book_ids: list[int]) -> dict[int, str | None]:
        raise AssertionError("대응표에 없는 값인데 카탈로그 조회를 했다")

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {1088: "설명"}

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_categories", _must_not_run)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="exact",
        exact=SpecExact(title="아무 제목", author=None, publisher=None),
        filters=SearchFilters(category="한국문학"),
    )
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1088]


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
    result = asyncio.run(chat.get_candidates(spec, [], 1))

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
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert len(result) == chat.CANDIDATE_LIMIT
    book_ids = [c["book_id"] for c in result]
    assert 1 not in book_ids
    assert chat.CANDIDATE_LIMIT + 1 in book_ids  # 밀려난 책이 빈 자리를 채운다


def test_publisher가_지정되면_다른_출판사_책은_후보에서_빠진다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(
            results=[
                _book(1, publisher="현암사"),
                _book(2, publisher="다른출판사"),
                _book(3, publisher="현암사"),
            ],
            degraded=None,
        )

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="exact",
        exact=SpecExact(title="마음", author=None, publisher="현암사"),
    )
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1, 3]


def test_publisher_비교는_공백을_무시한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=[_book(1, publisher=" 현암사 ")], degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="exact",
        exact=SpecExact(title="마음", author=None, publisher="현암사"),
    )
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1]


def test_publisher_비교는_주식회사_표기_차이를_무시한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 리뷰에서 지적된 실제 표기 차이 — "(주)현암사"는 6,649권이 쓰는 "(주)"/주식회사
    # 표기의 예, "현암주니어 :현암사"는 "브랜드 :출판사" 형태의 예다.
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(
            results=[
                _book(1, publisher="(주)현암사"),
                _book(2, publisher="현암주니어 :현암사"),
                _book(3, publisher="다른출판사"),
            ],
            degraded=None,
        )

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="exact",
        exact=SpecExact(title="마음", author=None, publisher="현암사"),
    )
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1, 2]


def test_publisher_없는_책은_출판사_지정_시_후보에서_빠진다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # books.py 주석대로 publisher가 null인 책이 있다 — .get()이 없으면 AttributeError.
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=[_book(1, publisher=None)], degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)

    spec = _spec(
        intent="exact",
        exact=SpecExact(title="마음", author=None, publisher="현암사"),
    )
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert result == []


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
    result = asyncio.run(chat.get_candidates(spec, [3], 1))

    assert [c["book_id"] for c in result] == [1, 4]


def test_이미_구매한_책은_후보에서_빠진다(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=[_book(1), _book(2)], degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    async def _fetch_history(user_id: int) -> history.History:
        return history.History(weights={1: history.PURCHASE})

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)
    monkeypatch.setattr(chat, "_fetch_history", _fetch_history)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [2]


def test_저평점_준_책은_후보에서_빠진다(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=[_book(1), _book(2)], degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    async def _fetch_history(user_id: int) -> history.History:
        return history.History(disliked_book_ids={1})

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)
    monkeypatch.setattr(chat, "_fetch_history", _fetch_history)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [2]


def test_라이브러리에_담았거나_긍정_리뷰만_준_책은_안_빠진다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """구매(PURCHASE)만 제외 대상이다 — 담기·긍정 리뷰는 추천에서 뺄 이유가 아니다."""

    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=[_book(1), _book(2)], degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    async def _fetch_history(user_id: int) -> history.History:
        return history.History(weights={1: history.LIBRARY, 2: history.LIKED_REVIEW})

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)
    monkeypatch.setattr(chat, "_fetch_history", _fetch_history)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1, 2]


def test_구매한_책_제외도_CANDIDATE_LIMIT_자리를_안_먹는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    books = [_book(i) for i in range(1, chat.CANDIDATE_LIMIT + 2)]

    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=books, degraded=None)

    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids}

    async def _fetch_history(user_id: int) -> history.History:
        return history.History(weights={1: history.PURCHASE})  # 1만 이미 구매함

    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)
    monkeypatch.setattr(chat, "_fetch_history", _fetch_history)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert len(result) == chat.CANDIDATE_LIMIT
    book_ids = [c["book_id"] for c in result]
    assert 1 not in book_ids
    assert chat.CANDIDATE_LIMIT + 1 in book_ids  # 밀려난 책이 빈 자리를 채운다


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
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert len(result) == chat.CANDIDATE_LIMIT
    assert [c["book_id"] for c in result] == list(range(1, chat.CANDIDATE_LIMIT + 1))


def _fake_descriptions(
    monkeypatch: pytest.MonkeyPatch, missing: set[int] = frozenset()
):
    async def _descriptions(book_ids: list[int]) -> dict[int, str]:
        return {book_id: "설명" for book_id in book_ids if book_id not in missing}

    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)


def _fake_search(monkeypatch: pytest.MonkeyPatch, books: list[dict]) -> None:
    async def _search(req: SearchRequest) -> SearchOutcome:
        return SearchOutcome(results=books, degraded=None)

    monkeypatch.setattr(chat.service, "search", _search)


def test_같은_제목_저자의_다른_판본은_앞의_하나만_후보에_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_search(
        monkeypatch,
        [
            _book(1, title="인류최강 남사친", author="지은이: 건드리고고"),
            _book(2, title="인류최강 남사친", author="지은이: 건드리고고"),
            _book(3, title="다른 책", author="지은이: 건드리고고"),
        ],
    )
    _fake_descriptions(monkeypatch)

    spec = _spec(intent="semantic", semantic="판타지")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1, 3]


def test_저자_표기가_달라도_같은_저자의_같은_제목이면_하나로_본다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_search(
        monkeypatch,
        [
            _book(1, title="해리포터", author="J.K. 롤링 지음 ;강동혁 옮김"),
            _book(
                2,
                title="해리 포터",
                author="지은이: J.K. 롤링,존 티퍼니 ;옮긴이: 박아람",
            ),
        ],
    )
    _fake_descriptions(monkeypatch)

    spec = _spec(intent="semantic", semantic="마법")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1]


def test_제목이_같아도_저자가_다르면_다른_책이라_둘_다_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_search(
        monkeypatch,
        [_book(1, title="나비", author="김가"), _book(2, title="나비", author="이나")],
    )
    _fake_descriptions(monkeypatch)

    spec = _spec(intent="semantic", semantic="나비")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1, 2]


def test_저자가_비어_있으면_같은_제목이어도_묶지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_search(
        monkeypatch,
        [_book(1, title="나비", author=None), _book(2, title="나비", author="")],
    )
    _fake_descriptions(monkeypatch)

    spec = _spec(intent="semantic", semantic="나비")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [1, 2]


def test_앞_판본에_소개글이_없으면_소개글_있는_판본으로_바꿔_남긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # exact는 소개글 없는 책도 남기는데, 같은 책이면 소개글이 있는 판본이 카드 이유를 쓰기 낫다.
    _fake_search(
        monkeypatch,
        [
            _book(1, title="채식주의자", author="한강"),
            _book(2, title="채식주의자", author="지은이: 한강"),
            _book(3, title="다른 책", author="작가"),
        ],
    )
    _fake_descriptions(monkeypatch, missing={1})

    spec = _spec(
        intent="exact", exact=SpecExact(title="채식주의자", author=None, publisher=None)
    )
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert [c["book_id"] for c in result] == [2, 3]
    assert result[0]["description"] == "설명"


def test_이미_보여_준_책의_다른_판본은_후보에서_뺀다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_search(
        monkeypatch,
        [
            _book(10, title="전자레인지 레시피", author="김수림"),
            _book(11, title="다른 요리책", author="박셰프"),
        ],
    )
    _fake_descriptions(monkeypatch)

    async def _shown(book_ids: list[int]) -> set[tuple[str, str]]:
        assert book_ids == [9]
        return {chat._edition_key("전자레인지 레시피", "김수림")}

    monkeypatch.setattr(chat, "_fetch_edition_keys", _shown)

    spec = _spec(intent="semantic", semantic="요리")
    result = asyncio.run(chat.get_candidates(spec, [9], 1))

    assert [c["book_id"] for c in result] == [11]


def test_중복_판본은_CANDIDATE_LIMIT_자리를_안_먹는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 같은 책 두 판본씩 CANDIDATE_LIMIT * 2권이 오면 중복을 걷은 뒤 CANDIDATE_LIMIT권이 남아야 한다.
    books = []
    for i in range(chat.CANDIDATE_LIMIT):
        books.append(_book(i * 2 + 1, title=f"제목{i}", author="작가"))
        books.append(_book(i * 2 + 2, title=f"제목{i}", author="작가"))
    _fake_search(monkeypatch, books)
    _fake_descriptions(monkeypatch)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, [], 1))

    assert len(result) == chat.CANDIDATE_LIMIT
    assert len({c["title"] for c in result}) == chat.CANDIDATE_LIMIT


def test_판본_키는_역할_말을_이름_속_글자와_헷갈리지_않는다() -> None:
    # "한글"의 "글", "홍길동저"처럼 붙어 있는 접미 글자는 이름의 일부라 잘라내면 안 된다.
    assert chat._edition_key("책", "한글") == ("책", "한글")
    assert chat._edition_key("책", "홍 길동 지음") == ("책", "홍길동")
    assert chat._edition_key("책", "글쓴이: 최작가") == ("책", "최작가")
    assert chat._edition_key("책", "") is None
    assert chat._edition_key("", "작가") is None


# ---------------------------------------------------------------------------
# 요청으로 온 가진 책(산 책·나의 도서관 책)으로 빼기(#319)
# ---------------------------------------------------------------------------


def _history_must_not_be_read(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(user_id: int) -> history.History:
        raise AssertionError("가진 책이 요청으로 왔으면 복제 표의 이력을 읽으면 안 됨")

    monkeypatch.setattr(chat, "_fetch_history", _boom)


def test_요청으로_온_가진_책은_후보에서_빠지고_복제_표는_읽지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_search(monkeypatch, [_book(1), _book(2), _book(3)])
    _fake_descriptions(monkeypatch)
    _history_must_not_be_read(monkeypatch)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, [], 1, owned_book_ids=[1, 3]))

    assert [c["book_id"] for c in result] == [2]


def test_가진_책이_빈_목록이면_아무것도_빼지_않고_복제_표도_읽지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 빈 목록은 "가진 책이 없다"는 뜻이다. 안 온 것(None)과 달라서 복제 표로 돌아가지 않는다.
    _fake_search(monkeypatch, [_book(1), _book(2)])
    _fake_descriptions(monkeypatch)
    _history_must_not_be_read(monkeypatch)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, [], 1, owned_book_ids=[]))

    assert [c["book_id"] for c in result] == [1, 2]


def test_가진_책이_안_왔으면_지금처럼_복제_표의_이력으로_뺀다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_search(monkeypatch, [_book(1), _book(2)])
    _fake_descriptions(monkeypatch)

    async def _fetch_history(user_id: int) -> history.History:
        return history.History(weights={1: history.PURCHASE})

    monkeypatch.setattr(chat, "_fetch_history", _fetch_history)

    spec = _spec(intent="semantic", semantic="아무거나")
    result = asyncio.run(chat.get_candidates(spec, [], 1, owned_book_ids=None))

    assert [c["book_id"] for c in result] == [2]
