"""③ 1단계가 낸 분야 값 검사(_settle_category, #359).

1단계 LLM은 분야 칸에 대응표에 없는 말("SF", "알고리즘")도 넣는다. 그대로 필터로 걸면
후보가 0건이 된다. 여기서는 그 값을 필터로 쓸지, 검색 문장으로 옮길지를 본다. 책 표 조회
(_category_has_books)는 가짜로 바꿔 끼운다. DB·LLM 없이 돈다.
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from app.chat.schemas import Spec, SpecExact
from app.main import app
from app.routers import chat
from app.search.schemas import SearchFilters

client = TestClient(app)

_CATALOG_NAMES = {"한국문학", "심리학"}


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """책 표에는 _CATALOG_NAMES 분류의 책만 있다고 친다. 물어본 값을 돌려줘 조회 여부를 볼 수 있다."""
    asked: list[str] = []

    async def _fake(name: str) -> bool:
        asked.append(name)
        return name in _CATALOG_NAMES

    monkeypatch.setattr(chat, "_category_has_books", _fake)
    return asked


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


def _settle(spec: Spec) -> Spec:
    return asyncio.run(chat._settle_category(spec))


def test_분야가_없으면_그대로_두고_책_표를_읽지_않는다(fake_catalog: list[str]) -> None:
    spec = _spec(semantic="비 오는 날 읽을 책")

    assert _settle(spec) == spec
    assert fake_catalog == []


def test_대응표에_있는_온보딩_값은_필터로_두고_책_표를_읽지_않는다(
    fake_catalog: list[str],
) -> None:
    spec = _spec(semantic="위로되는 책", filters=SearchFilters(category="소설"))

    assert _settle(spec) == spec
    assert fake_catalog == []


def test_카탈로그_분류명_그대로면_필터로_둔다(fake_catalog: list[str]) -> None:
    spec = _spec(semantic="마음을 다룬 책", filters=SearchFilters(category="심리학"))

    assert _settle(spec) == spec
    assert fake_catalog == ["심리학"]


@pytest.mark.parametrize(
    ("category", "semantic", "expected"),
    [
        # 문장에 없는 말이면 뒤에 붙인다.
        (
            "알고리즘",
            "코딩테스트 준비에 맞는 파이썬 책",
            "코딩테스트 준비에 맞는 파이썬 책 알고리즘",
        ),
        # 이미 문장에 든 말이면 다시 붙이지 않는다. 대소문자는 가리지 않는다.
        ("SF", "한국 작가가 쓴 SF", "한국 작가가 쓴 SF"),
        ("sf", "한국 작가가 쓴 SF", "한국 작가가 쓴 SF"),
        # 문장이 비었으면 그 말이 검색 문장이 된다.
        ("추리", None, "추리"),
    ],
)
def test_대응표에도_카탈로그에도_없는_값은_필터를_풀고_검색_문장으로_옮긴다(
    category: str, semantic: str | None, expected: str
) -> None:
    settled = _settle(
        _spec(semantic=semantic, filters=SearchFilters(category=category))
    )

    assert settled.filters.category is None
    assert settled.semantic == expected


def test_옮길_때_다른_조건은_그대로_둔다() -> None:
    spec = _spec(
        semantic="파이썬 책",
        filters=SearchFilters(category="입문자용", price_max=20000, pub_year_from=2022),
    )

    settled = _settle(spec)

    assert settled.filters == SearchFilters(price_max=20000, pub_year_from=2022)
    assert settled.exclude == spec.exclude
    assert settled.intent == "semantic"


def test_분류로_가를_수_없는_온보딩_값은_검색_문장으로_옮긴다(
    fake_catalog: list[str],
) -> None:
    # 어린이는 ①④도 거르지 않는 값이다(#228). 필터로 남겨도 걸리는 게 없다.
    settled = _settle(
        _spec(semantic="읽어 줄 책", filters=SearchFilters(category="어린이"))
    )

    assert settled.filters.category is None
    assert settled.semantic == "읽어 줄 책 어린이"
    assert fake_catalog == []


def test_제목이나_저자를_콕_집은_요청은_필터만_풀고_문장은_건드리지_않는다() -> None:
    spec = _spec(
        intent="exact",
        exact=SpecExact(title="해리포터", author=None, publisher=None),
        filters=SearchFilters(category="판타지"),
    )

    settled = _settle(spec)

    assert settled.filters.category is None
    assert settled.semantic is None
    assert settled.exact == spec.exact


@pytest.mark.parametrize("category", ["", "   "])
def test_빈_분야_값은_없는_것으로_돌린다(category: str) -> None:
    settled = _settle(
        _spec(semantic="위로되는 책", filters=SearchFilters(category=category))
    )

    assert settled.filters.category is None
    assert settled.semantic == "위로되는 책"


@pytest.mark.parametrize("category", ["경제경영", "심리학"])
def test_분야만_있고_찾을_말이_없으면_분야_이름을_검색_문장으로_쓴다(
    category: str,
) -> None:
    settled = _settle(_spec(filters=SearchFilters(category=category)))

    # 분야 필터는 그대로 걸려 있고, 검색어만 생긴다.
    assert settled.filters.category == category
    assert settled.semantic == category
    assert chat._query_text(settled) == category


def test_분야와_출판사만_있어도_분야_이름을_검색_문장으로_쓴다() -> None:
    # 출판사는 제목·저자·소개글에 없는 말이라 검색어로는 최후 수단이다(_query_text).
    spec = _spec(
        exact=SpecExact(title=None, author=None, publisher="한빛미디어"),
        filters=SearchFilters(category="IT"),
    )

    assert chat._query_text(_settle(spec)) == "IT"


def test_찾을_말이_있으면_분야_이름으로_덮어쓰지_않는다() -> None:
    by_title = _spec(
        intent="exact",
        exact=SpecExact(title="총 균 쇠", author=None, publisher=None),
        filters=SearchFilters(category="역사"),
    )

    assert _settle(by_title) == by_title


# 아래는 라우터에 끼워 본다. 1단계 LLM 답만 가짜로 주고, 2단계가 받은 spec과 응답 spec을 본다.

_INITIAL_SPEC = {
    "intent": "semantic",
    "exact": {"title": None, "author": None, "publisher": None},
    "filters": {},
    "semantic": None,
    "anchor_book": None,
    "exclude": [],
}


def _post(
    monkeypatch: pytest.MonkeyPatch, message: str, **spec_patch
) -> tuple[dict, list[Spec]]:
    """1단계가 spec_patch를 냈을 때의 (응답 data, 2단계가 받은 spec 목록). 후보는 0건으로 둔다."""
    received: list[Spec] = []

    async def _no_candidates(
        spec, exclude_book_ids, user_id, owned_book_ids=None
    ) -> list[dict]:
        received.append(spec)
        return []

    reply = AIMessage(content=json.dumps(spec_patch, ensure_ascii=False))
    monkeypatch.setattr(chat, "get_candidates", _no_candidates)
    monkeypatch.setattr(
        chat, "get_chat_model", lambda: RunnableLambda(lambda _prompt: reply)
    )

    res = client.post(
        "/recommendations/chat",
        json={
            "user_id": 123,
            "consented": False,
            "spec": _INITIAL_SPEC,
            "message": message,
            "recent_turns": [],
            "exclude_book_ids": [],
        },
    )

    assert res.status_code == 200
    return res.json()["data"], received


def test_1단계가_없는_분야를_내면_2단계는_필터_없이_그_말로_찾는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data, received = _post(
        monkeypatch,
        "추리 소설 같은 책 추천해줘",
        semantic="긴장감 있는 책",
        filters={"category": "추리"},
    )

    assert received[0].filters.category is None
    assert received[0].semantic == "긴장감 있는 책 추리"
    # 응답 spec도 고친 값이다. 다음 턴에 못 쓰는 분야 값이 되돌아오지 않는다.
    assert data["spec"]["filters"]["category"] is None
    assert data["spec"]["semantic"] == "긴장감 있는 책 추리"
    # 못 쓰는 분야 값을 "걸린 조건"으로 안내하지 않는다.
    assert "추리 조건" not in data["followup"]


def test_되묻기가_안내한_분야만_말해도_검색어가_생긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 되묻기(#283)는 "경제경영 책 추천해줘"처럼 답하라고 안내한다. 1단계가 분야만 채워도
    # 검색어가 비지 않아야 한다. 비면 같은 질문("어떤 분야가 좋으세요?")을 되풀이한다.
    data, received = _post(
        monkeypatch, "경제경영 책 추천해줘", filters={"category": "경제경영"}
    )

    assert received[0].filters.category == "경제경영"
    assert chat._query_text(received[0]) == "경제경영"
    assert "어떤 분야가 좋으세요?" not in data["followup"]


def test_책_표_조회가_실패하면_공통_형식의_500이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(name: str) -> bool:
        raise RuntimeError("DB 연결 실패")

    monkeypatch.setattr(chat, "_category_has_books", _boom)
    reply = AIMessage(
        content=json.dumps({"filters": {"category": "추리"}}, ensure_ascii=False)
    )
    monkeypatch.setattr(
        chat, "get_chat_model", lambda: RunnableLambda(lambda _prompt: reply)
    )

    res = client.post(
        "/recommendations/chat",
        json={
            "user_id": 123,
            "consented": False,
            "spec": _INITIAL_SPEC,
            "message": "추리 소설 추천해줘",
            "recent_turns": [],
            "exclude_book_ids": [],
        },
    )

    assert res.status_code == 500
    assert res.json()["message"] == "internal_server_error"
