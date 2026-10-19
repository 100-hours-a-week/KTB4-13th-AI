"""⑦ 라우터가 쓸 장바구니(CommerceGateway)와 카탈로그(CatalogGateway)를 고른다(#358, #363, #489).

장바구니는 BE 를 부르지 않는다. BE 가 요청에 실어 보낸 장바구니와 가격·재고로 이번 턴만 쓰는 복사본을 만들고,
끝나면 그 차이를 actions 로 돌려준다(A안, #489). 그래서 운영과 개발이 같은 길로 돈다.

카탈로그(소개글)는 AI DB 의 책 표에서 읽는다(#415). COMMERCE_MOCK 이 켜진 로컬에서는 DB 없이 손으로 시험할 수
있게 가공의 서가를 쓴다.
"""

from app.agent.schemas import AgentActRequest
from app.core.config import get_settings
from app.gateway.catalog import CatalogGateway
from app.gateway.catalog_db import DbCatalog
from app.gateway.commerce import BookOffer, CartLine
from app.gateway.commerce_fake import FakeBook, FakeCommerce
from app.gateway.commerce_snapshot import SnapshotCommerce

# 로컬에서 손으로 시험할 때 쓰는 가공의 서가(소개글). 평가 사례(evals/agent)의 서가와 같은 것이 아니다.
_DEV_BOOKS = [
    FakeBook(
        "9788900001001",
        "처음 만나는 파이썬",
        15000,
        50,
        description="프로그래밍이 처음인 사람을 위한 파이썬 입문서. 변수와 함수부터 파일 다루기까지 차근차근 설명한다.",
    ),
    FakeBook(
        "9788900001018",
        "데이터베이스 설계 노트",
        22000,
        50,
        description="테이블 설계와 정규화, 인덱스를 사례로 설명하는 실무 노트. 쿼리 성능을 높이는 방법도 다룬다.",
    ),
    FakeBook("9788900001025", "쿠버네티스 입문 가이드", 28000, 50),
    FakeBook("9788900001032", "알고리즘 문제 해결 전략", 25000, 50),
    FakeBook("9788900001049", "소프트웨어 설계의 기본기", 18000, 3),
]


def get_commerce(req: AgentActRequest) -> SnapshotCommerce:
    """이번 요청에 실린 장바구니와 가격·재고로 만든 복사본."""
    cart = [
        CartLine(line.isbn, line.title, line.price, line.quantity, line.available)
        for line in req.cart
    ]
    offers = [
        BookOffer(offer.isbn, offer.title, offer.price, offer.in_stock)
        for offer in req.offers
    ]
    return SnapshotCommerce(cart, offers)


def get_catalog() -> CatalogGateway:
    """책의 제목과 소개글을 읽는 곳."""
    if get_settings().commerce_mock:
        return FakeCommerce(_DEV_BOOKS)
    return DbCatalog()
