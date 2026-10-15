"""④ 관심사별 목록의 매칭 점수(app/feed/interest_scoring.py) 테스트(#448). DB 없이 돈다."""

import pytest

from app.feed import interest_scoring


@pytest.mark.parametrize(
    ("similarity", "by_name", "expected"),
    [
        (0.80, True, 0.0),  # 이름 벡터 구간의 아래 끝
        (0.84, True, 0.5),
        (0.88, True, 1.0),
        (0.82, False, 0.0),  # 책 벡터 평균 구간의 아래 끝
        (0.86, False, 0.5),
        (0.90, False, 1.0),
        (0.95, False, 1.0),  # 넘치면 자른다
        (None, False, 0.0),  # 책 벡터가 없는 경우
    ],
)
def test_유사도는_검색어_종류마다_다른_구간으로_편다(
    similarity: float | None, by_name: bool, expected: float
) -> None:
    part = interest_scoring.similarity_part(similarity, by_name=by_name)

    assert part == pytest.approx(expected)


def test_작가_관심사에서_저자가_맞으면_유사도와_상관없이_1이다() -> None:
    part = interest_scoring.similarity_part

    assert part(0.5, by_name=False, author_matched=True) == 1
    assert part(None, by_name=True, author_matched=True) == 1


def test_몫은_가장_많이_받은_관심사를_1로_친다() -> None:
    shares = interest_scoring.share_parts({"요리": 120, "SF": 60, "역사": 20})

    assert shares == pytest.approx({"요리": 1.0, "SF": 0.5, "역사": 1 / 6})


def test_자리가_없으면_몫도_없다() -> None:
    assert interest_scoring.share_parts({}) == {}


def test_매칭_점수는_유사도_60_몫_25다() -> None:
    # 0.6*0.5 + 0.25*1.0 = 0.55
    assert interest_scoring.match_score(0.5, 1.0) == 55


def test_인기도_항은_자리만_두어_가장_높은_점수는_85다() -> None:
    assert interest_scoring.match_score(1.0, 1.0) == 85
