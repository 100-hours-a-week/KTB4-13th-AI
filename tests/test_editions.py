"""같은 책의 판본 키(app/core/editions.py) 테스트(#452)."""

import pytest

from app.core import editions


@pytest.mark.parametrize(
    ("author", "expected"),
    [
        ("한강", "한강"),
        ("홍 길동 지음", "홍길동"),  # 역할 접미어와 띄어쓰기
        ("지은이: 한강", "한강"),  # 역할 접두어
        ("J.K. 롤링 지음 ;강동혁 옮김", "jk롤링"),  # 첫 저자만
        ("한강, 데보라 스미스", "한강"),
        ("글로리아", "글로리아"),  # 이름 속 "글"은 떼지 않는다
        (None, ""),
    ],
)
def test_첫_저자를_역할_말과_띄어쓰기_없이_본다(
    author: str | None, expected: str
) -> None:
    assert editions.author_key(author) == expected


def test_띄어쓰기와_문장부호만_다른_제목과_첫_저자가_같으면_같은_판본이다() -> None:
    first = editions.edition_key("해리 포터: 1", "J.K. 롤링, 강동혁")
    second = editions.edition_key("해리포터 1", "J.K. 롤링 지음 ;김혜원 옮김")

    assert first == second == ("해리포터1", "jk롤링")


def test_제목이나_저자가_비면_묶지_않는다() -> None:
    assert editions.edition_key("책", None) is None
    assert editions.edition_key("", "한강") is None
