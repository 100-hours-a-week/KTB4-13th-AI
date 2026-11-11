"""③ 분야 이름만 말한 요청을 많이 읽힌 책으로 답하기(app/chat/popular.py, #514).

"소설 추천해줘"에 제목이 『소설』인 책이 아니라 많이 읽힌 소설이 나와야 한다. 내용이 있는 요청은 지금처럼 검색한다.
DB·LLM 은 가짜로 바꿔 끼운다.
"""

import asyncio
import contextlib
import gzip
import json
import os
import random
from pathlib import Path

import asyncpg
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat import popular, shown
from app.chat.popular import Ask, Entry
from app.chat.schemas import Spec, SpecExact
from app.core.config import get_settings
from app.main import app
from app.routers import chat
from app.search.schemas import SearchFilters, SearchRequest
from app.search.service import SearchOutcome

NOVEL = frozenset({"소설"})


# --- 요청 해석 ---


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("소설", Ask(NOVEL)),
        ("그냥 소설 추천", Ask(NOVEL)),
        ("아무 소설이나", Ask(NOVEL)),
        ("재밌는 장편소설", Ask(NOVEL)),
        ("에세이 추천", Ask(frozenset({"수필"}))),
        ("에세이를", Ask(frozenset({"수필"}))),
        ("산문집", Ask(frozenset({"수필"}))),
        ("시집", Ask(frozenset({"시"}))),
        ("자기계발서 추천해줘", Ask(frozenset({"자기계발"}))),
        ("추리소설", Ask(NOVEL, frozenset({"추리"}))),
        ("추리 소설", Ask(NOVEL, frozenset({"추리"}))),
        ("미스터리", Ask(NOVEL, frozenset({"추리"}))),
        ("추리·스릴러", Ask(NOVEL, frozenset({"추리"}))),
        ("SF 소설", Ask(NOVEL, frozenset({"SF"}))),
        ("한국 소설", Ask(NOVEL, origins=frozenset({"한국"}))),
        ("일본 추리소설", Ask(NOVEL, frozenset({"추리"}), frozenset({"일본"}))),
        ("청소년 소설", Ask(NOVEL, teen=True)),
        # 수필은 576권뿐이라 나라로 가르지 않는다
        ("일본 에세이", Ask(frozenset({"수필"}))),
    ],
)
def test_분야_이름과_내용_없는_말뿐이면_그_분야다(text: str, expected: Ask) -> None:
    assert popular.parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # 내용이 있으면 지금처럼 검색한다
        "잔잔한 소설",
        "우울할 때 읽을 소설",
        "반전 있는 추리소설",
        "300쪽 이하의 소설",
        "소설 말고 에세이",
        # 새 책을 원하는 말은 많이 읽힌(몇 년 된) 책으로 답하지 않는다
        "요즘 소설",
        "최근 에세이",
        # 분야 이름이 없다
        "책 추천",
        "한국",
        "",
        None,
    ],
)
def test_내용이_있거나_분야가_없으면_검색한다(text: str | None) -> None:
    assert popular.parse(text) is None


# --- 꼬리표 ---


def _response(
    isbn: str = "9780000000001",
    kdc: str = "813.7",
    addition: str = "03810",
    loans: int = 100,
    keywords: dict[str, float] | None = None,
) -> dict:
    return {
        "book": {
            "isbn13": isbn,
            "class_no": kdc,
            "addition_symbol": addition,
            "loanCnt": str(loans),
        },
        "keywords": [
            {"keyword": {"word": word, "weight": str(weight)}}
            for word, weight in (keywords or {}).items()
        ],
    }


def test_한국_장편소설은_소설_한국으로_매긴다() -> None:
    entry, loans = popular.tag(_response(kdc="813.7", loans=241692))

    assert entry == Entry("9780000000001", "소설", "한국", False, frozenset())
    assert loans == 241692


@pytest.mark.parametrize(
    ("kdc", "kind", "origin"),
    [
        ("833.6", "소설", "일본"),
        ("843.6", "소설", "영미"),
        ("814.62", "수필", "한국"),
        ("811.7", "시", "한국"),
        ("325.211", "자기계발", None),
        ("199.1", "자기계발", None),
    ],
)
def test_KDC로_분야와_나라를_매긴다(kdc: str, kind: str, origin: str | None) -> None:
    entry, _ = popular.tag(_response(kdc=kdc))

    assert (entry.kind, entry.origin) == (kind, origin)


@pytest.mark.parametrize(
    "kdc", ["812.6", "400", "325.04", "", "abc"]
)  # 희곡, 과학, 경영 일반, 비었음
def test_쓰지_않는_분야는_넣지_않는다(kdc: str) -> None:
    assert popular.tag(_response(kdc=kdc)) is None


@pytest.mark.parametrize("addition", ["77810", "63710", "53710"])
def test_아동서와_학습참고서는_넣지_않는다(addition: str) -> None:
    # 어른의 "소설 추천해줘"에 동화가 나오면 안 된다
    assert popular.tag(_response(addition=addition)) is None


def test_부가기호_4는_청소년이다() -> None:
    entry, _ = popular.tag(_response(addition="43810"))

    assert entry.teen


def test_세부_장르는_낱말이_통째로_같을_때만_무게를_더한다() -> None:
    # "용"을 일부로 보면 『용의자 X의 헌신』이 판타지가 됐다
    entry, _ = popular.tag(_response(keywords={"용의자": 15, "살인": 10, "용기": 30}))

    assert entry.subgenres == {"추리"}


def test_세부_장르는_무게_합이_기준보다_낮으면_매기지_않는다() -> None:
    entry, _ = popular.tag(_response(keywords={"살인": 10, "탐정": 9}))

    assert entry.subgenres == frozenset()


def test_세부_장르는_소설에만_매긴다() -> None:
    entry, _ = popular.tag(_response(kdc="814.7", keywords={"사랑": 40}))

    assert entry.subgenres == frozenset()


# --- 파일 ---


def _line(**kwargs) -> str:
    return json.dumps(
        {"isbn13": kwargs.get("isbn"), "resp": {"response": _response(**kwargs)}},
        ensure_ascii=False,
    )


def test_파일은_대출_많은_순이고_대출_건수는_담지_않는다() -> None:
    data = popular.build(
        [
            _line(isbn="9780000000001", loans=10),
            "깨진 줄",
            _line(isbn="9780000000002", loans=300),
            _line(isbn="9780000000003", kdc="400", loans=999),  # 쓰지 않는 분야
            _line(isbn="9780000000004", loans=50, kdc="833.6"),
        ]
    )

    assert data["version"] == popular.FORMAT_VERSION
    assert [row[0] for row in data["books"]] == [
        "9780000000002",
        "9780000000004",
        "9780000000001",
    ]
    assert "300" not in json.dumps(data)
    index = popular.from_data(data)
    assert index.entries[1] == Entry(
        "9780000000004", "소설", "일본", False, frozenset()
    )


def test_같은_ISBN은_뒤에_받은_것을_쓴다() -> None:
    data = popular.build(
        [_line(isbn="9780000000001", loans=10), _line(isbn="9780000000001", loans=500)]
    )

    assert len(data["books"]) == 1


def test_형식이_다른_파일은_읽지_않는다() -> None:
    with pytest.raises(ValueError):
        popular.from_data({"version": 0, "books": []})


def test_파일이_없거나_깨졌으면_없는_것으로_돈다(tmp_path: Path) -> None:
    assert popular.load(tmp_path / "없음.json.gz") is None
    broken = tmp_path / "깨짐.json.gz"
    broken.write_bytes(b"not gzip")
    assert popular.load(broken) is None


def test_저장한_파일을_다시_읽는다(tmp_path: Path) -> None:
    path = tmp_path / "popular.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(popular.build([_line(isbn="9780000000001")]), f)

    index = popular.load(path)

    assert [e.isbn for e in index.entries] == ["9780000000001"]


def test_저장소의_인기_책_파일을_읽을_수_있다() -> None:
    # 서버 이미지가 이 파일을 싣는다(Dockerfile). 형식이 어긋나면 분야만 말한 요청이 조용히 검색으로 돌아간다
    index = popular.load(Path(__file__).parent.parent / "data/popular_books.json.gz")

    assert index is not None
    assert len(popular.isbns_for(index, Ask(NOVEL), limit=50)) == 50
    assert popular.isbns_for(index, Ask(NOVEL, frozenset({"추리"})))


# --- 고르기·보기 ---


INDEX = popular.PopularBooks(
    (
        Entry("9780000000011", "소설", "한국", False, frozenset()),
        Entry("9780000000012", "소설", "일본", False, frozenset({"추리"})),
        Entry("9780000000013", "수필", "한국", False, frozenset()),
        Entry("9780000000014", "소설", "영미", True, frozenset({"추리", "SF"})),
        Entry("9780000000015", "소설", "한국", False, frozenset()),
        Entry("9780000000016", "소설", "한국", False, frozenset()),
    )
)


@pytest.mark.parametrize(
    ("ask", "isbns"),
    [
        (Ask(NOVEL), ["11", "12", "14", "15", "16"]),
        (Ask(NOVEL, frozenset({"추리"})), ["12", "14"]),
        (Ask(NOVEL, origins=frozenset({"한국"})), ["11", "15", "16"]),
        (Ask(NOVEL, teen=True), ["14"]),
        (Ask(frozenset({"수필"})), ["13"]),
    ],
)
def test_그_분야의_책을_많이_읽힌_순으로_고른다(ask: Ask, isbns: list[str]) -> None:
    assert popular.isbns_for(INDEX, ask) == [f"97800000000{n}" for n in isbns]


def test_넓은_소설_요청에만_좁힐_보기를_준다() -> None:
    assert popular.question(Ask(NOVEL)) == (
        popular.NOVEL_QUESTION,
        popular.NOVEL_OPTIONS,
    )
    assert popular.question(Ask(NOVEL, frozenset({"추리"}))) is None
    assert popular.question(Ask(NOVEL, origins=frozenset({"일본"}))) is None
    assert popular.question(Ask(frozenset({"수필"}))) is None
    assert popular.question(None) is None


@pytest.mark.parametrize("option", popular.NOVEL_OPTIONS)
def test_보기를_고른_말도_분야만_말한_요청이다(option: str) -> None:
    # 다음 턴도 많이 읽힌 책에서 좁혀져야 한다. 아니면 고른 보기가 검색어가 되어 제목 낱말 책이 다시 나온다
    assert popular.parse(option) is not None
    assert popular.parse(f"{option} 소설") is not None


# --- get_candidates 에 이어 붙인 모양 ---


def _book(book_id: int, title: str | None = None) -> dict:
    return {
        "book_id": book_id,
        "title": title or f"책{book_id}",
        "author": f"작가{book_id}",
        "publisher": "출판사",
        "price": 10000,
        "in_stock": True,
        "cover_url": None,
    }


# 11/11 dev: "추리소설" 검색이 제목에 '추리소설'이 든 책으로 카드 3장을 채웠다
TITLE_WORD_BOOKS = [
    _book(91, "추리소설 속 트릭의 비밀"),
    _book(92, "추리소설 세계명단편"),
]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict:
    """DB·검색·리랭커를 가짜로 바꾸고 무엇이 불렸는지 남긴다. 인기 책 ISBN 끝 두 자리를 book_id 로 쓴다."""
    record: dict = {"search": 0, "popular": [], "rerank": 0, "taste": 0}

    class _NoPool:
        def acquire(self):
            return contextlib.nullcontext()

    async def _noop(conn, candidates, user_id, spec) -> None:
        pass

    async def _no_keys(book_ids):
        return set()

    async def _descriptions(book_ids):
        return {book_id: "소개" for book_id in book_ids}

    async def _search(req: SearchRequest) -> SearchOutcome:
        record["search"] += 1
        return SearchOutcome(results=[dict(b) for b in TITLE_WORD_BOOKS], degraded=None)

    async def _popular(isbns: list[str], search_filters: SearchFilters) -> list[dict]:
        record["popular"].append((isbns, search_filters))
        return [_book(int(isbn[-2:])) for isbn in isbns]

    async def _rerank(query, pool):
        record["rerank"] += 1
        return pool

    async def _taste(conn, pool, user_id):
        record["taste"] += 1
        return pool

    monkeypatch.setattr(chat.db, "get_pool", lambda: _NoPool())
    monkeypatch.setattr(chat, "_attach_match_scores", _noop)
    monkeypatch.setattr(chat, "_fetch_edition_keys", _no_keys)
    monkeypatch.setattr(chat, "_fetch_descriptions", _descriptions)
    monkeypatch.setattr(chat.service, "search", _search)
    monkeypatch.setattr(chat, "_popular_books", _popular)
    monkeypatch.setattr(chat.reranker, "rerank", _rerank)
    monkeypatch.setattr(chat, "_popular_order", _taste)
    popular.install(INDEX)
    yield record
    popular.install(None)


def _spec(semantic: str | None, category: str | None = None, **filters) -> Spec:
    return Spec(
        intent="semantic",
        exact=SpecExact(title=None, author=None, publisher=None),
        filters=SearchFilters(category=category, **filters),
        semantic=semantic,
        anchor_book=None,
        exclude=[],
    )


def test_분야만_말하면_검색하지_않고_많이_읽힌_책을_후보로_쓴다(calls: dict) -> None:
    result = asyncio.run(chat.get_candidates(_spec("소설"), [], 1))

    assert [c["book_id"] for c in result] == [11, 12, 14, 15, 16]
    assert calls["search"] == 0
    # 분야 이름과 견줄 글이 없어 리랭커를 쓰지 않고, 취향으로만 다시 세운다
    assert calls["rerank"] == 0
    assert calls["taste"] == 1


def test_세부_장르만_말해도_그_장르의_많이_읽힌_책을_쓴다(calls: dict) -> None:
    result = asyncio.run(chat.get_candidates(_spec("추리소설"), [], 1))

    # 셋이 안 되어 검색으로 간다 — 아래 시험과 같은 규칙. 여기서는 많이 읽힌 책을 먼저 찾아 봤는지만 본다
    assert calls["popular"][0][0] == ["9780000000012", "9780000000014"]
    assert [c["book_id"] for c in result] == [91, 92]


def test_많이_읽힌_책이_카드_수만큼_안_남으면_지금처럼_검색한다(calls: dict) -> None:
    # 이미 보여 준 책을 빼면 둘만 남는다
    result = asyncio.run(chat.get_candidates(_spec("소설"), [11, 12, 14], 1))

    assert [c["book_id"] for c in result] == [91, 92]
    assert calls["search"] == 1


def test_내용이_있는_요청은_지금처럼_검색한다(calls: dict) -> None:
    result = asyncio.run(chat.get_candidates(_spec("반전 있는 추리소설"), [], 1))

    assert [c["book_id"] for c in result] == [91, 92]
    assert calls["popular"] == []
    assert calls["rerank"] == 1


def test_인기_책_파일이_없으면_지금처럼_검색한다(calls: dict) -> None:
    popular.install(None)

    asyncio.run(chat.get_candidates(_spec("소설"), [], 1))

    assert calls == {"search": 1, "popular": [], "rerank": 1, "taste": 0}


def test_분야_칸으로는_다시_거르지_않고_가격_조건은_넘긴다(
    calls: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 외국 수필은 카탈로그 분류가 '일본문학·기타 아시아문학' 같은 나라 문학이라 "에세이" 분류(수필집·한국문학)로 거르면
    # 빠진다(11/11 평가 DB: 많이 읽힌 수필 175권 중 55권, 무라카미 하루키 『달리기를 말할 때 내가 하고 싶은 이야기』 등)
    popular.install(
        popular.PopularBooks(
            tuple(
                Entry(f"97800000000{n}", "수필", "일본", False, frozenset())
                for n in (21, 22, 23)
            )
        )
    )

    async def _japanese_literature(book_ids):
        return {book_id: "일본문학·기타 아시아문학" for book_id in book_ids}

    monkeypatch.setattr(chat, "_fetch_categories", _japanese_literature)

    result = asyncio.run(
        chat.get_candidates(_spec("에세이 추천", "에세이", price_max=15000), [], 1)
    )

    assert [c["book_id"] for c in result] == [21, 22, 23]
    passed = calls["popular"][0][1]
    assert passed.price_max == 15000


def test_많이_읽힌_책_조회가_실패하면_500_대신_검색한다(
    calls: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.undo()  # _popular_books 를 진짜로 되돌려 popular.fetch 실패를 본다

    class _NoPool:
        def acquire(self):
            return contextlib.nullcontext()

    async def _boom(conn, isbns, search_filters):
        raise TimeoutError

    searched = []

    async def _search(req: SearchRequest) -> SearchOutcome:
        searched.append(req.query)
        return SearchOutcome(results=[], degraded=None)

    monkeypatch.setattr(chat.db, "get_pool", lambda: _NoPool())
    monkeypatch.setattr(chat.popular, "fetch", _boom)
    monkeypatch.setattr(chat.service, "search", _search)
    popular.install(INDEX)

    assert asyncio.run(chat.get_candidates(_spec("소설"), [], 1)) == []
    assert searched == ["소설"]


def test_취향이_있으면_취향_가까운_순으로_다시_세운다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        def __init__(self, profile):
            self.profile = profile

        async def fetchrow(self, sql, user_id):
            return self.profile

    async def _similarities(conn, book_ids, centroid):
        return {1: 0.2, 2: 0.9, 3: 0.5}

    monkeypatch.setattr(chat, "_fetch_similarities", _similarities)
    pool = [_book(1), _book(2), _book(3), _book(4)]
    profile = {"centroid": "[0.1, 0.2]", "cold_start": False}

    ordered = asyncio.run(chat._popular_order(_Conn(profile), pool, 1))
    # 벡터가 없는 4번은 뒤로
    assert [c["book_id"] for c in ordered] == [2, 3, 1, 4]

    for no_taste in (
        None,
        {"centroid": None, "cold_start": False},
        {**profile, "cold_start": True},
    ):
        assert asyncio.run(chat._popular_order(_Conn(no_taste), pool, 1)) == pool


def test_취향이_있어도_취향_가까운_책_안에서_매번_다르게_뽑는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def fetchrow(self, sql, user_id):
            return {"centroid": "[0.1, 0.2]", "cold_start": False}

    # 60권, 번호가 클수록 취향에 가깝다
    async def _similarities(conn, book_ids, centroid):
        return {book_id: book_id / 100 for book_id in book_ids}

    monkeypatch.setattr(chat, "_fetch_similarities", _similarities)
    pool = [_book(n) for n in range(1, 61)]
    near = set(range(61 - popular.TASTE_POOL, 61))

    picks = [
        [c["book_id"] for c in asyncio.run(chat._popular_order(_Conn(), pool, 1))]
        for _ in range(6)
    ]

    for ids in picks:
        assert len(set(ids)) == chat.CANDIDATE_LIMIT
        assert set(ids) <= near
    # 새 대화마다 같은 10권이 아니다
    assert len({tuple(sorted(ids)) for ids in picks}) > 1


def test_취향을_모르면_많이_읽힌_순서에_기대어_카드_수만큼_뽑는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def fetchrow(self, sql, user_id):
            return None

    pool = [_book(n) for n in range(1, 31)]

    picked = asyncio.run(chat._popular_order(_Conn(), pool, 1))

    ids = [c["book_id"] for c in picked]
    assert len(set(ids)) == chat.CANDIDATE_LIMIT
    assert set(ids) <= set(range(1, 31))


def test_뽑기는_위쪽을_더_자주_고르되_아래쪽도_고른다() -> None:
    pool = list(range(1, 201))
    rng = random.Random(514)
    counts = {rank: 0 for rank in pool}
    draws = 2000
    for _ in range(draws):
        for rank in popular.sample(pool, 10, rng):
            counts[rank] += 1

    # 1위가 10권에 드는 비율(주석의 "약 30%")
    assert 0.25 < counts[1] / draws < 0.40
    assert counts[1] > counts[50] > counts[200] > 0
    # 2,000번 뽑으면 200권이 거의 다 한 번은 나온다 — 맨 위 10권만 돌지 않는다
    assert sum(1 for c in counts.values() if c) > 190


def test_뽑을_것보다_적으면_다_준다() -> None:
    assert popular.sample([3, 1, 2], 10) == [3, 1, 2]


# --- 라우터: 먼저 묻지 않고 카드와 보기를 같이 ---

client = TestClient(app)

INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}
CARD_BOOKS = [
    {
        "book_id": n,
        "title": title,
        "author": "작가",
        "description": "소개",
        "match_score": 30,
        "popularity": 1.0,
    }
    for n, title in ((1, "나미야 잡화점의 기적"), (2, "소년이 온다"), (3, "채식주의자"))
]
SCOPE_PREFIX = "너는 책 추천 챗봇이다. 사용자의 첫 요청을 보고"
BROAD = json.dumps(
    {
        "scope": "broad",
        "reply": "소설을 찾으시는군요.",
        "question": "어떤 종류의 소설을 찾고 계신가요?",
        "options": ["미스터리 소설", "로맨스 소설"],
    },
    ensure_ascii=False,
)


def _model(
    spec_patch: dict, card_reply: str = "많이 읽힌 소설로 골랐어요. 어떤 쪽이 좋으세요?"
):
    """1단계·넓은 요청 판정·카드 생성에 답하는 가짜 모델."""
    cards = json.dumps(
        {
            "cards": [
                {"book_id": b["book_id"], "reason_short": "이유"} for b in CARD_BOOKS
            ],
            "reply": card_reply,
        },
        ensure_ascii=False,
    )

    def _call(prompt_value) -> AIMessage:
        text = prompt_value.to_messages()[0].content
        if text.startswith(SCOPE_PREFIX):
            return AIMessage(content=BROAD)
        if '"cards"' in text or "카드" in text[:200]:
            return AIMessage(content=cards)
        return AIMessage(content=json.dumps(spec_patch, ensure_ascii=False))

    return RunnableLambda(_call)


@pytest.fixture
def route(monkeypatch: pytest.MonkeyPatch) -> list:
    """먼저 묻기를 켜고(dev 와 같게) 후보 검색을 가짜로 바꾼다. 불린 spec 을 남긴다."""
    monkeypatch.setenv("CHAT_NARROWING", "true")
    get_settings.cache_clear()
    searched: list = []

    async def _candidates(spec, exclude_book_ids, user_id, owned_book_ids=None):
        searched.append(spec)
        return [dict(b) for b in CARD_BOOKS]

    async def _fetch(cards):
        return []

    monkeypatch.setattr(chat, "get_candidates", _candidates)
    monkeypatch.setattr(shown, "fetch", _fetch)
    popular.install(INDEX)
    yield searched
    popular.install(None)
    get_settings.cache_clear()


def _post(message: str, recent_turns: list | None = None) -> dict:
    body = {
        "user_id": 123,
        "consented": True,
        "spec": INITIAL_SPEC,
        "message": message,
        "recent_turns": recent_turns or [],
        "exclude_isbns": [],
        "image_ref": None,
    }
    return client.post("/recommendations/chat", json=body).json()["data"]


def test_넓은_소설_요청은_먼저_묻지_않고_카드와_좁힐_보기를_같이_준다(
    route: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1단계가 분야 칸만 채운다("그냥 소설 추천해줘", 11/11 dev). 넓은 요청 판정은 묻자고 한다
    monkeypatch.setattr(
        chat, "get_chat_model", lambda: _model({"filters": {"category": "소설"}})
    )

    data = _post("그냥 소설 추천해줘")

    assert len(route) == 1  # 묻지 않고 찾았다
    assert [c["title"] for c in data["cards"]] == [
        "나미야 잡화점의 기적",
        "소년이 온다",
        "채식주의자",
    ]
    assert data["followup"] == (
        "어떤 소설이 끌리세요?\n\n• 추리·스릴러\n• 한국 소설\n• 일본 소설\n• 영미 소설"
    )
    # 말풍선 끝의 모델 질문은 카드 아래 보기 질문과 겹치지 않게 뗀다
    assert "?" not in data["reply"]


def test_이미_좁힌_요청에는_보기를_붙이지_않는다(
    route: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat,
        "get_chat_model",
        lambda: _model({"semantic": "추리소설"}, card_reply="골랐어요."),
    )

    data = _post("추리소설 추천해줘")

    assert len(route) == 1
    assert data["followup"] is None or popular.NOVEL_QUESTION not in data["followup"]


def test_직전에_물었으면_보기를_또_붙이지_않는다(
    route: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat,
        "get_chat_model",
        lambda: _model({"semantic": "소설"}, card_reply="골랐어요."),
    )
    turns = [
        {"role": "user", "text": "소설 추천해줘"},
        {"role": "assistant", "text": "골랐어요.\n어떤 소설이 끌리세요?"},
    ]

    data = _post("다른 거", turns)

    assert data["followup"] is None or popular.NOVEL_QUESTION not in data["followup"]


def test_내용이_있는_넓은_요청은_지금처럼_먼저_묻는다(
    route: list, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        chat, "get_chat_model", lambda: _model({"semantic": "위로가 되는 소설"})
    )

    data = _post("위로되는 소설 추천해줘")

    assert route == []  # 찾기 전에 물었다
    assert data["cards"] == []
    assert data["followup"].startswith("어떤 종류의 소설을 찾고 계신가요?")


# --- 실제 DB ---

_DB_URL = os.environ.get("SEARCH_TEST_DATABASE_URL")
_needs_db = pytest.mark.skipif(
    not _DB_URL,
    reason="실제 PostgreSQL 이 필요하다. SEARCH_TEST_DATABASE_URL 에 주소를 준다",
)


def _run(check):
    async def _go():
        conn = await asyncpg.connect(_DB_URL)
        tx = conn.transaction()
        await tx.start()
        try:
            return await check(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(_go())


async def _insert(
    conn: asyncpg.Connection, book_id: int, isbn: str, pub_year: int, category: str
) -> None:
    await conn.execute(
        "INSERT INTO v_books (book_id, title, author, publisher, price, in_stock,"
        " cover_url, category, pub_year, description, isbn13)"
        " VALUES ($1, $2, '작가', '출판사', 10000, true, NULL, $3, $4, '소개', $5)",
        book_id,
        f"인기시험책{book_id}",
        category,
        pub_year,
        isbn,
    )


@_needs_db
def test_DB에서_많이_읽힌_순서대로_가져오고_연도_조건은_걸고_분야_칸은_걸지_않는다() -> (
    None
):
    async def check(conn):
        await _insert(conn, 9_514_001, "9799990514001", 2010, "한국문학")
        await _insert(
            conn, 9_514_002, "9799990514002", 2024, "일본문학·기타 아시아문학"
        )
        await _insert(conn, 9_514_003, "9799990514003", 2022, "한국문학")
        return await popular.fetch(
            conn,
            # 많이 읽힌 순서: 003 → 서점에 없는 책 → 002 → 001
            ["9799990514003", "9799990514999", "9799990514002", "9799990514001"],
            SearchFilters(category="에세이", pub_year_from=2020),
        )

    books = _run(check)

    assert [b["book_id"] for b in books] == [9_514_003, 9_514_002]
    assert {"isbn", "title", "author", "publisher", "price"} <= set(books[0])


def test_뽑은_책은_순서를_섞어_카드_LLM_이_앞쪽만_고르지_않게_한다() -> None:
    rng = random.Random(7)
    orders = [popular.sample(list(range(1, 201)), 10, rng) for _ in range(20)]

    assert any(order != sorted(order) for order in orders)


@pytest.mark.parametrize(
    ("message", "recalls"),
    [
        ("소설 추천해줘", False),
        ("그냥 에세이", False),
        ("추리소설 아무거나", False),
        # 내용이 있으면 지금처럼 떠올린 제목도 부른다
        ("위로되는 소설 추천해줘", True),
        ("고양이가 탐정으로 나오는 추리소설", True),
    ],
)
def test_분야만_말한_턴에는_떠올린_제목을_부르지_않는다(
    monkeypatch: pytest.MonkeyPatch, message: str, recalls: bool
) -> None:
    # 떠올린 제목 호출이 매번 『종의 기원』·『불편한 편의점』을 넣어 카드가 그 책으로 쏠렸다(11/11 실측 6번 중 4번)
    monkeypatch.setenv("CHAT_AGENT", "true")
    get_settings.cache_clear()
    popular.install(INDEX)
    try:
        assert chat._may_recall(message) is recalls
    finally:
        popular.install(None)
