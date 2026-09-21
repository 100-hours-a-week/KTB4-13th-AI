"""① POST /search 라우터 테스트 — 요청 검사와 응답 모양."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.search import FALLBACK_MESSAGE, parse_request

client = TestClient(app)


def test_검색어만_있으면_200과_0건_모양을_돌려준다() -> None:
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
    ],
)
def test_계약을_어기면_400이다(payload: object) -> None:
    res = client.post("/search", json=payload)

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
