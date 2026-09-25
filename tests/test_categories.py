"""온보딩 카테고리 → 카탈로그 분류 대응표 테스트. DB 없이 돈다."""

import pytest

from app.core import categories
from app.core.categories import ONBOARDING_TO_CATALOG


def _scores(picked: list[str]) -> dict[str, int]:
    return categories.catalog_scores(picked, core_points=2, partial_points=1)


def test_핵심_분류와_일부_분류에_점수를_다르게_준다() -> None:
    scores = _scores(["소설"])

    assert scores["한국문학"] == 2
    assert scores["영미문학"] == 2
    assert scores["문학"] == 1


def test_여러_개를_고르면_같은_분류의_점수를_더한다() -> None:
    # 한국문학은 소설의 핵심이자 에세이의 일부다.
    assert _scores(["소설", "에세이"])["한국문학"] == 3


def test_같은_값을_두_번_보내도_한_번만_친다() -> None:
    assert _scores(["여행", "여행"]) == {"지리": 2}


def test_대응표에_없는_값은_건너뛴다() -> None:
    # 어린이는 분류로 가를 수 없어 대응표에 없다. 보기가 바뀌어 모르는 값이 와도 실패하지 않는다.
    assert _scores(["어린이", "만화"]) == {}


@pytest.mark.parametrize("picked", sorted(ONBOARDING_TO_CATALOG))
def test_한_온보딩_값_안에서_핵심과_일부가_겹치지_않는다(picked: str) -> None:
    match = ONBOARDING_TO_CATALOG[picked]
    names = [*match.core, *match.partial]

    assert len(names) == len(set(names))


def test_필터용_분류는_핵심만이다() -> None:
    # 일부 분류는 필터에 넣지 않는다(#110). 에세이의 일부인 한국문학이 빠진다.
    assert categories.filter_categories("에세이") == (
        "강연집·수필집·연설문집",
        "에세이",
    )


def test_대응표에_없는_값은_필터용_분류가_없다() -> None:
    assert categories.filter_categories("한국문학") is None


def test_온보딩_값인지_가린다() -> None:
    assert categories.is_onboarding("에세이")
    assert not categories.is_onboarding("한국문학")
