"""온보딩 관심 카테고리 → 카탈로그 분류(`v_books.category`) 대응표(#110).

온보딩 값(소설, 에세이 등)은 앱이 보여 주는 값이고, 카탈로그 분류는 도서관 분류명(한국문학, 경제학 등)이라
글자가 달라 그대로는 이어지지 않는다. 온보딩 값마다 카탈로그 분류를 두 갈래로 묶는다.

- 핵심: 그 온보딩 값에 거의 그대로 맞는 분류
- 일부: 다른 내용이 섞여 있는 분류. 예) 한국문학은 소설·시·에세이가 한 분류(810)라 에세이에는 일부다

한 분류가 여러 온보딩 값에 들어갈 수 있다. 온보딩 보기는 BE 가 만들어 바뀔 수 있으므로, 여기 없는 값은
쓰는 쪽에서 건너뛴다. 어린이는 분류로 가를 수 없어(여러 분류에 흩어져 있다) 넣지 않았다(#110).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Match:
    core: tuple[str, ...]
    partial: tuple[str, ...] = ()


_FOREIGN_LITERATURE = (
    "영미문학",
    "일본문학·기타 아시아문학",
    "중국문학",
    "프랑스문학",
    "독일문학",
    "스페인·포르투갈문학",
    "이탈리아문학",
    "기타 제문학",
)

ONBOARDING_TO_CATALOG: dict[str, Match] = {
    "소설": Match(
        core=("한국문학", *_FOREIGN_LITERATURE, "한국소설"),
        partial=("문학",),
    ),
    "에세이": Match(
        core=("강연집·수필집·연설문집", "에세이"),
        partial=("한국문학", "문학"),
    ),
    "인문": Match(
        core=("심리학", "언어", "한국어", "풍속·예절·민속학", "전기(인물)"),
        partial=("철학", "역사"),
    ),
    "경제경영": Match(core=("경제학",), partial=("윤리학·도덕철학", "통계자료")),
    # 윤리학·도덕철학은 이름과 달리 경영(325)·처세(199) 책이 대부분이다(정보나루 세부 분류로 확인).
    "자기계발": Match(core=("윤리학·도덕철학",), partial=("심리학", "경제학")),
    "라이프스타일": Match(
        core=("생활과학", "오락·스포츠"),
        partial=("공예", "회화·도화·디자인", "농업·농학"),
    ),
    "과학": Match(
        core=(
            "자연과학",
            "수학",
            "물리학",
            "화학",
            "생명과학",
            "동물학",
            "식물학",
            "천문학",
            "지학",
            "광물학",
        ),
        partial=("의학", "기술과학"),
    ),
    "외국어": Match(
        core=(
            "영어",
            "일본어·기타 아시아제어",
            "중국어",
            "프랑스어",
            "독일어",
            "스페인어·포르투갈어",
            "이탈리아어",
            "기타 제어",
        ),
        partial=("언어",),
    ),
    "철학": Match(
        core=(
            "철학",
            "동양철학·사상",
            "서양철학",
            "형이상학",
            "인식론·인과론·인간학",
            "철학의 체계",
            "논리학",
            "경학",
        ),
        partial=("윤리학·도덕철학",),
    ),
    "역사": Match(
        core=(
            "역사",
            "아시아사",
            "유럽사",
            "북아메리카사",
            "남아메리카사",
            "아프리카사",
            "오세아니아사",
        ),
        partial=("전기(인물)",),
    ),
    # 지리는 대부분 기행(981)이다.
    "여행": Match(core=("지리",)),
    "사회": Match(
        core=(
            "사회과학",
            "사회학·사회문제",
            "정치학",
            "행정학",
            "법학",
            "교육학",
            "신문·저널리즘",
            "국방·군사학",
        ),
        partial=("경제학",),
    ),
    # 총류는 대부분 컴퓨터(004, 005) 책이다.
    "IT": Match(core=("총류",), partial=("전기·전자·통신공학",)),
}


def catalog_scores(
    onboarding_categories: list[str], core_points: int, partial_points: int
) -> dict[str, int]:
    """고른 온보딩 카테고리를 카탈로그 분류별 점수로 푼다. 대응표에 없는 값은 건너뛴다.

    여러 개를 고르면 점수를 더한다(소설 + 에세이 → 한국문학은 핵심 + 일부). 같은 값을 두 번
    보내도 한 번만 친다.
    """
    scores: dict[str, int] = {}
    for picked in dict.fromkeys(onboarding_categories):
        match = ONBOARDING_TO_CATALOG.get(picked)
        if match is None:
            continue
        for category in match.core:
            scores[category] = scores.get(category, 0) + core_points
        for category in match.partial:
            scores[category] = scores.get(category, 0) + partial_points
    return scores


def filter_categories(name: str) -> tuple[str, ...] | None:
    """①④ 의 category 필터로 쓸 카탈로그 분류. 온보딩 값이면 핵심 분류만, 대응표에 없으면 None.

    일부 분류는 필터에 넣지 않는다. "에세이"로 걸렀는데 한국문학의 소설이 섞여 나오면 안 된다(#110).
    """
    match = ONBOARDING_TO_CATALOG.get(name)
    return None if match is None else match.core


def is_onboarding(name: str) -> bool:
    """대응표에 있는 온보딩 값인지. ①④ 요청의 category 는 이 값만 받는다(#219)."""
    return name in ONBOARDING_TO_CATALOG
