"""① POST /search 라우터 테스트 — 요청 검사와 응답 모양."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import search as search_router
from app.routers.search import FALLBACK_MESSAGE, parse_request
from app.search.service import SearchOutcome

client = TestClient(app)

_BOOK = {
    "book_id": 2077,
    "title": "여행의 이유",
    "author": "김영하",
    "publisher": "문학동네",
    "price": 13500,
    "in_stock": True,
    "cover_url": None,
}


def _fake_search(
    results: list[dict],
    degraded: str | None = "keyword-only",
    next_cursor: str | None = None,
    first_page: bool = True,
):
    async def _search(req):
        return SearchOutcome(
            results=results,
            next_cursor=next_cursor,
            first_page=first_page,
            degraded=degraded,
        )

    return _search


@pytest.fixture(autouse=True)
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 없이 돈다. 기본은 0건. 검색 자체는 test_search_keyword.py 가 본다."""
    monkeypatch.setattr(search_router.service, "search", _fake_search([]))


def test_0건이면_200과_안내_문구를_돌려준다() -> None:
    res = client.post("/search", json={"query": "김영하 여행의 이유"})

    assert res.status_code == 200
    assert res.json() == {
        "message": "search_success",
        "data": {
            "results": [],
            "next_cursor": None,
            "fallback": {"message": FALLBACK_MESSAGE},
        },
    }


def test_생략한_값은_명세_기본값으로_채운다() -> None:
    req = parse_request({"query": "책"})

    assert req is not None
    assert (req.sort, req.size, req.cursor) == ("relevance", 15, None)
    assert req.filters.in_stock_only is False


def test_명세의_입력_예시가_그대로_통과한다() -> None:
    req = parse_request(
        {
            "query": "김영하 여행의 이유",
            "filters": {
                "category": "에세이",
                "price_min": 10000,
                "price_max": 20000,
                "pub_year_from": 2020,
                "pub_year_to": 2026,
                "in_stock_only": True,
            },
            "sort": "relevance",
            "cursor": None,
            "size": 15,
        }
    )

    assert req is not None
    assert req.filters.price_max == 20000


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "가" * 201},
        {"query": 123},
        {"query": "책", "size": 0},
        {"query": "책", "size": 51},
        {"query": "책", "size": "15"},
        {"query": "책", "sort": "cheapest"},
        {"query": "책", "filters": {"price_min": -1}},
        {"query": "책", "filters": {"price_min": 20000, "price_max": 10000}},
        {"query": "책", "filters": {"pub_year_from": 2026, "pub_year_to": 2020}},
        {"query": "책", "filters": {"in_stock_only": "yes"}},
        {"query": "책", "filters": "에세이"},
        ["책"],
        # #201 — int32 범위·NUL.
        {"query": "김영\x00하"},
        {"query": "책", "filters": {"price_min": 2_147_483_648}},
        {"query": "책", "filters": {"pub_year_from": -2_147_483_649}},
    ],
)
def test_계약을_어기면_400이다(payload: object) -> None:
    res = client.post("/search", json=payload)

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_검색어에_짝_없는_서로게이트가_있으면_400이다() -> None:
    """#201 — httpx 테스트 클라이언트는 파이썬 str에 실제 서로게이트가 있으면 그 자체를
    보내지도 못한다(클라이언트 쪽 UTF-8 인코딩 에러). 실제 공격 벡터는 JSON 문자열
    escape(`\\ud800`)가 와이어를 타고 서버 json.loads()에서 그 문자로 풀리는 경우라,
    본문을 raw bytes로 직접 보내 재현한다.
    """
    res = client.post(
        "/search",
        content=b'{"query": "\\ud800"}',
        headers={"Content-Type": "application/json"},
    )

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_경계값_200자와_50개는_통과한다() -> None:
    res = client.post("/search", json={"query": "가" * 200, "size": 50})

    assert res.status_code == 200


def test_본문이_JSON이_아니면_400이다() -> None:
    res = client.post(
        "/search", content=b"not json", headers={"Content-Type": "application/json"}
    )

    assert res.status_code == 400


def test_본문이_UTF8이_아니면_500이_아니라_400이다() -> None:
    res = client.post(
        "/search", content=b"\xff\xfe{", headers={"Content-Type": "application/json"}
    )

    assert res.status_code == 400


def test_본문이_상한을_넘으면_413이다() -> None:
    # 헤더만 크게 속여도 본문을 읽기 전에 막아야 한다(#99).
    res = client.post(
        "/search",
        content=b'{"query":"book"}',
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(search_router.MAX_BODY_BYTES + 1),
        },
    )

    assert res.status_code == 413
    assert res.json() == {"message": "payload_too_large", "data": None}


def test_결과가_있으면_책을_싣고_안내_문구는_null이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_router.service, "search", _fake_search([_BOOK]))

    res = client.post("/search", json={"query": "김영하"})

    assert res.status_code == 200
    assert res.json()["data"] == {
        "results": [_BOOK],
        "next_cursor": None,
        "fallback": None,
    }


def test_기능을_줄여_응답했으면_헤더로_알린다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_router.service, "search", _fake_search([_BOOK], "keyword-only")
    )

    res = client.post("/search", json={"query": "김영하"})

    assert res.headers["X-Degraded"] == "keyword-only"


def test_온전한_응답에는_헤더가_없다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_router.service, "search", _fake_search([_BOOK], None))

    res = client.post("/search", json={"query": "김영하"})

    assert "X-Degraded" not in res.headers


def test_검색이_실패하면_500_internal_server_error다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail(req):
        raise RuntimeError("DB 연결 끊김")

    monkeypatch.setattr(search_router.service, "search", _fail)

    res = client.post("/search", json={"query": "김영하"})

    assert res.status_code == 500
    assert res.json() == {"message": "internal_server_error", "data": None}


def test_다음_페이지_커서를_응답에_싣는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_router.service, "search", _fake_search([_BOOK], next_cursor="커서글자")
    )

    res = client.post("/search", json={"query": "김영하"})

    assert res.json()["data"]["next_cursor"] == "커서글자"


def test_커서를_못_쓰면_410_cursor_expired다(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _expired(req):
        raise search_router.service.CursorExpired

    monkeypatch.setattr(search_router.service, "search", _expired)

    res = client.post("/search", json={"query": "김영하", "cursor": "옛커서"})

    assert res.status_code == 410
    assert res.json() == {"message": "cursor_expired", "data": None}


def test_둘째_페이지_이후가_비어도_0건_안내는_붙이지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        search_router.service, "search", _fake_search([], first_page=False)
    )

    res = client.post("/search", json={"query": "김영하", "cursor": "커서"})

    assert res.json()["data"]["fallback"] is None
