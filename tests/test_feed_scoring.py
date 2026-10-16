"""④ 개인화 점수 테스트 — 세 항을 0–1 로 바꾸고 합치는 규칙. DB 없이 돈다."""

import pytest

from app.feed import scoring


@pytest.mark.parametrize(
    ("similarity", "expected"),
    [
        (0.85, 0.0),  # 구간의 아래 끝
        (0.90, 0.5),
        (0.95, 1.0),  # 구간의 위 끝
        (0.99, 1.0),  # 넘치면 자른다
        (0.50, 0.0),  # 모자라도 자른다
        (None, 0.0),  # 책 벡터가 없는 경우
    ],
)
def test_유사도는_정해진_구간으로_편다(
    similarity: float | None, expected: float
) -> None:
    assert scoring.similarity_part(similarity) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (2, 0.5),  # 온보딩에서 고른 분류만 있는 경우
        (5, 1.0),  # 거기에 한 권 더 사면 넘친다
        (-2, 0.0),  # 싫다고 한 책 때문에 음수면 0
        (None, 0.0),  # 프로필에 그 분류가 없는 경우
    ],
)
def test_카테고리_점수는_4를_기준으로_편다(raw: float | None, expected: float) -> None:
    assert scoring.category_part(raw) == pytest.approx(expected)


def test_인기는_카탈로그_최고_점수로_나눈다() -> None:
    assert scoring.popularity_part(4.0, 8.0) == pytest.approx(0.5)


def test_인기_집계가_비어_있으면_0이다() -> None:
    # BE 집계가 들어오기 전에는 모든 책이 0 점이라 이 항이 순위를 바꾸지 않는다(#56).
    assert scoring.popularity_part(0, 0) == 0.0


def test_세_항을_비중대로_더해_100점_만점으로_낸다() -> None:
    # 유사도 0.5, 카테고리 0.5, 인기 1.0 → 0.6*0.5 + 0.25*0.5 + 0.15*1.0 = 0.575
    score = scoring.match_score(0.90, 2, 8.0, 8.0)

    assert score == 58


def test_모든_항이_최고면_100점이다() -> None:
    assert scoring.match_score(0.95, 4, 8.0, 8.0) == 100


def test_벡터를_쓸_수_없으면_유사도_항만_빼고_나머지_비중은_그대로다() -> None:
    # 남은 비중을 1 로 키우지 않는다. 키우면 같은 책이 축소 응답에서 더 높은 점수를 받는다.
    score = scoring.match_score(0.95, 4, 8.0, 8.0, with_similarity=False)

    assert score == 40
