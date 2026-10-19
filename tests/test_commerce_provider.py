"""⑦ 장바구니·카탈로그를 고르는 곳(app/gateway/commerce_provider.py) 테스트(#358, #363, #489).

장바구니는 BE 를 부르지 않고 요청에 실린 장바구니와 가격·재고로 만든 복사본이다. 그래서 COMMERCE_MOCK 과
상관없이 운영에서도 돈다. 카탈로그는 COMMERCE_MOCK 이 켜지면 개발용 서가, 아니면 AI DB 다.
"""

import asyncio

import pytest

from app.agent.schemas import parse_request
from app.core.config import get_settings
from app.gateway.catalog import CatalogGateway
from app.gateway.catalog_db import DbCatalog
from app.gateway.commerce import CommerceGateway
from app.gateway.commerce_provider import get_catalog, get_commerce

A, B = "9788900000011", "9788900000028"


@pytest.fixture(autouse=True)
def _fresh():
    yield
    get_settings.cache_clear()


def _mock(monkeypatch, on: bool) -> None:
    monkeypatch.setenv("COMMERCE_MOCK", "true" if on else "false")
    get_settings.cache_clear()


def _request(**fields):
    body = {
        "user_id": 1,
        "message": "1번 담아줘",
        "user_context": {"logged_in": True},
        "idempotency_key": "k",
        **fields,
    }
    req = parse_request(body)
    assert req is not None
    return req


@pytest.mark.parametrize("on", [False, True])
def test_장바구니는_COMMERCE_MOCK과_상관없이_요청에_실린_값으로_만든다(
    monkeypatch, on: bool
) -> None:
    _mock(monkeypatch, on)
    req = _request(
        cart=[
            {
                "isbn": A,
                "title": "책1",
                "price": 15000,
                "quantity": 2,
                "available": True,
            }
        ],
        offers=[{"isbn": B, "title": "책2", "price": 22000, "in_stock": True}],
    )

    commerce = get_commerce(req)

    assert isinstance(commerce, CommerceGateway)
    lines = asyncio.run(commerce.get_cart(1))
    assert [(line.isbn, line.quantity) for line in lines] == [(A, 2)]
    offers = asyncio.run(commerce.get_offers([B]))
    assert [(o.isbn, o.price, o.in_stock) for o in offers] == [(B, 22000, True)]


def test_요청마다_새_복사본이라_앞_요청의_변경이_남지_않는다() -> None:
    req = _request(
        cart=[
            {
                "isbn": A,
                "title": "책1",
                "price": 15000,
                "quantity": 1,
                "available": True,
            }
        ]
    )
    first = get_commerce(req)
    asyncio.run(first.remove_item(1, A))

    second = get_commerce(req)

    assert asyncio.run(first.get_cart(1)) == []
    assert [line.isbn for line in asyncio.run(second.get_cart(1))] == [A]


def test_COMMERCE_MOCK이_꺼져_있으면_카탈로그는_AI_DB다(monkeypatch) -> None:
    _mock(monkeypatch, False)

    assert isinstance(get_catalog(), DbCatalog)


def test_개발용_서가에는_소개글이_있는_책이_둘_이상_있다(monkeypatch) -> None:
    # 로컬에서 "1번이랑 2번 뭐가 달라?"를 손으로 시험할 수 있어야 한다
    _mock(monkeypatch, True)
    isbns = ["9788900001001", "9788900001018"]

    catalog = get_catalog()
    texts = asyncio.run(catalog.get_texts(isbns))

    assert isinstance(catalog, CatalogGateway)
    assert [t.isbn for t in texts] == isbns
    assert all(t.description for t in texts)
