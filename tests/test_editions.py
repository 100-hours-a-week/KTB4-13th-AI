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


@pytest.mark.parametrize(
    ("author", "description", "expected"),
    [
        # 『듄』 그래픽노블(11/11 dev 시험, #691): 저자 칸의 "각색"과 소개글 둘 다 표시가 있다
        (
            "프랭크 허버트, 브라이언 허버트, 케빈 J. 앤더슨 각색, 라울 앨런",
            "프랭크 허버트의 대작 『듄』의 그래픽노블 3권.",
            True,
        ),
        ("허영만 만화", "", True),
        ("나관중", "삼국지를 만화로 옮긴 책", True),
        ("프랭크 허버트, 김승욱", "역사상 가장 많이 팔린 SF", False),
        # 삽화가 든 소설·그림책은 원작과 다른 꼴이 아니다
        ("김영하 지음, 이우일 그림", "", False),
        # 원작 소설의 소개글에 흔한 말은 각색판 표시가 아니다
        ("김호연", "웹툰과 드라마로도 만들어진 화제작", False),
        ("히가시노 게이고", "영화와 만화로도 만들어진 소설", False),
        (None, None, False),
    ],
)
def test_그래픽노블_만화_각색판을_저자_칸과_소개글로_알아본다(
    author: str | None, description: str | None, expected: bool
) -> None:
    assert editions.is_adaptation(author, description) is expected
