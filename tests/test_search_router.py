"""① POST /search 라우터 테스트 — 요청 검사와 응답 모양."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


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
