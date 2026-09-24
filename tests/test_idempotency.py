"""멱등 키 공용 모듈 테스트 — 본문 해시와 키 나누기. DB 없이 돈다.

저장·조회·409 는 실제 DB 가 필요해 tests/test_profile_service.py 에서 본다.
"""

from app.core import idempotency


def test_키_순서와_공백이_달라도_같은_본문으로_본다() -> None:
    a = {"user_id": 1, "onboarding": {"tags": ["힐링"], "liked_book_ids": [1]}}
    b = {"onboarding": {"liked_book_ids": [1], "tags": ["힐링"]}, "user_id": 1}

    assert idempotency.body_hash(a) == idempotency.body_hash(b)


def test_값이_다르면_다른_본문이다() -> None:
    assert idempotency.body_hash({"tags": ["힐링"]}) != idempotency.body_hash(
        {"tags": ["성장"]}
    )


def test_배열_순서가_다르면_다른_본문이다() -> None:
    # 좋아한 책은 앞 50권만 쓰므로 순서가 결과를 바꾼다.
    assert idempotency.body_hash([1, 2]) != idempotency.body_hash([2, 1])


def test_API마다_키_앞에_이름을_붙여_나눈다() -> None:
    assert idempotency.scoped_key("profile", "k1") != idempotency.scoped_key(
        "agent", "k1"
    )
