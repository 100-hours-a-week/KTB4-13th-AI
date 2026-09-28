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


def test_어린이는_온보딩_값이지만_거르지_않는다() -> None:
    # 어린이책은 여러 분류에 흩어져 있어 분류로 가를 수 없다(#110, #228).
    assert categories.is_onboarding("어린이")
    assert categories.filter_categories("어린이") == ()


def test_세부_태그는_BE_보기_49개에서_겹친_하나를_뺀_48개다() -> None:
    # 여행 에세이가 에세이와 여행 밑에 둘 다 있다. 개수가 바뀌면 BE 보기가 바뀐 것이다(#165).
    assert len(categories.ONBOARDING_TAGS) == 48
    assert len(set(categories.ONBOARDING_TAGS)) == 48


def test_세부_태그마다_대응하는_분류를_적어_두었다() -> None:
    # 태그 목록이 바뀌었는데 대응표를 안 고치면 그 태그는 조용히 점수에서 빠진다.
    assert set(categories.TAG_TO_CATALOG) == set(categories.ONBOARDING_TAGS)


def test_세부_태그를_카탈로그_분류_점수로_푼다() -> None:
    assert categories.tag_scores(["재테크", "마케팅", "우주"], 1) == {
        "경제학": 2,
        "천문학": 1,
    }


def test_맞는_분류가_없는_태그는_부모_관심_분류의_핵심_분류를_쓴다() -> None:
    assert set(categories.tag_scores(["추리/스릴러"], 1)) == set(
        ONBOARDING_TO_CATALOG["소설"].core
    )


def test_윤리학_태그는_처세_책이_많은_윤리학_도덕철학에_잇지_않는다() -> None:
    assert "윤리학·도덕철학" not in categories.tag_scores(["윤리학"], 1)


def test_어린이_태그와_목록에_없는_태그는_점수가_없다() -> None:
    assert categories.tag_scores(["그림책", "힐링"], 1) == {}


def test_같은_태그를_두_번_보내도_한_번만_친다() -> None:
    assert categories.tag_scores(["재테크", "재테크"], 1) == {"경제학": 1}
