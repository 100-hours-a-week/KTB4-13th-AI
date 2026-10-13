"""⑦ 장바구니·카탈로그를 고르는 곳(app/gateway/commerce_provider.py) 테스트(#358, #363).

진짜 BE 가 연결되기 전에는 COMMERCE_MOCK 이 켜져 있을 때만 가짜를 준다. 꺼져 있는데 가짜로 답하면 배포
환경에서 "담았어요"가 거짓이 된다.
"""

import asyncio

import pytest

from app.core.config import get_settings
from app.gateway import commerce_provider
from app.gateway.catalog import CatalogGateway
from app.gateway.commerce import CommerceGateway
from app.gateway.commerce_provider import (
    CommerceNotConfigured,
    get_catalog,
    get_commerce,
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    commerce_provider.reset_dev_commerce()
    yield
    commerce_provider.reset_dev_commerce()
    get_settings.cache_clear()


def _mock(monkeypatch, on: bool) -> None:
    monkeypatch.setenv("COMMERCE_MOCK", "true" if on else "false")
    get_settings.cache_clear()


def test_COMMERCE_MOCK이_꺼져_있으면_장바구니도_카탈로그도_연결되지_않은_것이다(
    monkeypatch,
) -> None:
    _mock(monkeypatch, False)

    with pytest.raises(CommerceNotConfigured):
        get_commerce(1)
    with pytest.raises(CommerceNotConfigured):
        get_catalog()


def test_켜져_있으면_같은_개발용_서가를_장바구니와_카탈로그로_쓴다(monkeypatch) -> None:
    _mock(monkeypatch, True)

    commerce = get_commerce(1)
    catalog = get_catalog()

    assert isinstance(commerce, CommerceGateway)
    assert isinstance(catalog, CatalogGateway)
    assert commerce is catalog  # 같은 서가여야 담은 책과 소개글이 어긋나지 않는다


def test_개발용_서가에는_소개글이_있는_책이_둘_이상_있다(monkeypatch) -> None:
    # 로컬에서 "1번이랑 2번 뭐가 달라?"를 손으로 시험할 수 있어야 한다
    _mock(monkeypatch, True)
    isbns = ["9788900001001", "9788900001018"]

    texts = asyncio.run(get_catalog().get_texts(isbns))

    assert [t.isbn for t in texts] == isbns
    assert all(t.description for t in texts)
