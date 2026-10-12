"""⑦ 라우터가 쓸 장바구니(CommerceGateway)를 고른다(#358).

진짜 BE 호출은 11단계에서 만든다. 그 전에는 COMMERCE_MOCK 이 켜진 로컬에서만 메모리 가짜 장바구니를
쓰고, 꺼져 있으면 CommerceNotConfigured 를 던져 /agent/act 가 503 으로 답하게 한다. 배포 환경에서 가짜
장바구니로 "담았어요"라고 답하는 일을 막으려는 것이다.
"""

from app.core.config import get_settings
from app.gateway.commerce import CommerceGateway
from app.gateway.commerce_fake import FakeBook, FakeCommerce


class CommerceNotConfigured(Exception):
    """진짜 BE 장바구니가 연결되지 않았고 가짜도 꺼져 있다."""


# 로컬에서 손으로 시험할 때 쓰는 가공의 서가. 평가 사례(evals/agent)의 서가와 같은 것이 아니다.
_DEV_BOOKS = [
    FakeBook("9788900001001", "처음 만나는 파이썬", 15000, 50),
    FakeBook("9788900001018", "데이터베이스 설계 노트", 22000, 50),
    FakeBook("9788900001025", "쿠버네티스 입문 가이드", 28000, 50),
    FakeBook("9788900001032", "알고리즘 문제 해결 전략", 25000, 50),
    FakeBook("9788900001049", "소프트웨어 설계의 기본기", 18000, 3),
]

_dev_commerce: FakeCommerce | None = None


def get_commerce(user_id: int) -> CommerceGateway:
    """이번 요청의 사용자에게 쓸 장바구니. 가짜는 프로세스 안에서 계속 이어진다(개발용)."""
    global _dev_commerce
    if not get_settings().commerce_mock:
        raise CommerceNotConfigured
    if _dev_commerce is None:
        _dev_commerce = FakeCommerce(_DEV_BOOKS)
    _dev_commerce.register_user(user_id)
    return _dev_commerce


def reset_dev_commerce() -> None:
    """테스트가 서로의 장바구니를 보지 않게 비운다."""
    global _dev_commerce
    _dev_commerce = None
