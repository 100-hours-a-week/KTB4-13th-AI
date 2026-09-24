"""④ 개인화 점수 — 취향 유사도·카테고리·인기를 0–100 으로 합친다(명세 ④).

⑦ 쇼핑 에이전트의 "골라 담기"가 같은 채점을 재사용하므로 라우터가 아니라 여기에 둔다(4단계 설계).

숫자는 모두 임시값이다(#109). 시드 사용자로 결과를 보고 정하기 전까지 쓰는 값이고, 바꿀 때는
여기만 고친다. 세 항을 각각 0–1 로 바꾼 뒤 비중대로 더하고 마지막에 100 을 곱한다.
"""

# 세 항의 비중. 합이 1 이다. 작가 항은 입력원이 없어(온보딩에 작가가 없고 기억은 자유 문장) V1 은 없다.
SIMILARITY_WEIGHT = 0.6
CATEGORY_WEIGHT = 0.25
POPULARITY_WEIGHT = 0.15

# 유사도를 0–1 로 펴는 구간. e5 는 관련 없는 책도 0.85 안팎이 나와, 그대로 쓰면 모두 80점대가 된다.
# 요청마다 최저·최고로 펴지 않는다 — 같은 책이 후보에 따라 다른 점수를 받으면 match_score_min 의
# 뜻이 사람마다 달라진다. 측정은 이슈 #109.
SIMILARITY_FLOOR = 0.85
SIMILARITY_CEILING = 0.95

# 카테고리 원점수를 0–1 로 바꿀 때 나누는 값. 온보딩에서 고른 분류(+2)만 있으면 0.5, 거기에 한 권을
# 사면(+3) 1 이 된다. 원점수는 ⑥ 이 저장한다(#92).
CATEGORY_SATURATION = 4


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def similarity_part(similarity: float | None) -> float:
    """취향 벡터와 책 벡터의 코사인 유사도를 0–1 로. 벡터가 없으면 0."""
    if similarity is None:
        return 0.0
    span = SIMILARITY_CEILING - SIMILARITY_FLOOR
    return _clamp((similarity - SIMILARITY_FLOOR) / span)


def category_part(raw_score: float | None) -> float:
    """그 책 분류의 원점수를 0–1 로. 싫다고 한 책 때문에 음수면 0 이다."""
    if raw_score is None:
        return 0.0
    return _clamp(raw_score / CATEGORY_SATURATION)


def popularity_part(popularity: float | None, catalog_max: float | None) -> float:
    """인기 점수를 카탈로그 최고 점수로 나눠 0–1 로. 집계가 비어 있으면 모두 0 이다."""
    if not popularity or not catalog_max or catalog_max <= 0:
        return 0.0
    return _clamp(popularity / catalog_max)


def match_score(
    similarity: float | None,
    category_raw: float | None,
    popularity: float | None,
    catalog_max: float | None,
    *,
    with_similarity: bool = True,
) -> int:
    """세 항을 비중대로 더해 0–100 정수로. 벡터를 쓸 수 없으면 유사도 항을 빼고 나머지로만 낸다.

    유사도 항을 뺄 때 남은 비중을 다시 1 로 키우지 않는다. 그러면 같은 책이 축소 응답에서 더 높은
    점수를 받아 match_score_min 의 뜻이 상황마다 달라진다(명세: 축소 응답도 같은 잣대).
    """
    total = CATEGORY_WEIGHT * category_part(category_raw)
    total += POPULARITY_WEIGHT * popularity_part(popularity, catalog_max)
    if with_similarity:
        total += SIMILARITY_WEIGHT * similarity_part(similarity)
    return round(total * 100)
