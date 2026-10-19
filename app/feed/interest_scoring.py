"""④ 관심사별 목록의 매칭 점수(0–100) — 지금 식의 틀에서 카테고리 항 자리를 관심사의 몫이 대신한다(#448, 설계 #395).

비중은 지금 식(app/feed/scoring.py)의 것을 그대로 쓴다. 챗봇이 그 파일을 쓰므로 고치지 않고 여기 둔다(#420).
유사도를 펴는 구간은 검색어 종류마다 따로 둔다. 측정은 #448.
"""

from collections.abc import Mapping

from app.feed.scoring import CATEGORY_WEIGHT, SIMILARITY_WEIGHT

# 관심사 이름의 벡터는 짧은 검색어라 책끼리보다 낮게 나온다(아무 책과 0.78, 뽑힌 위 200권의 가운데 0.86).
NAME_SIMILARITY_FLOOR = 0.80
NAME_SIMILARITY_CEILING = 0.88
# 관심사 책들로 뽑은 책은 그 관심사의 내 책 가운데 가장 비슷한 책과의 유사도를 편다(#483). 평균과의 코사인은
# 평균 낸 권수가 많을수록 아무 책과도 높아지고(1권 0.81, 10권 0.87), 그 책들과의 유사도 평균은 책이 많고
# 다양할수록 낮아져(비슷한 10권 59점, 흩어진 10권 45점) 구간 하나로 펼 수 없었다. 가장 비슷한 책과의 유사도는
# 1권이면 둘과 같고, 쓰는 책을 10권까지로 묶어 아무 책과 0.81–0.84 다.
BOOKS_SIMILARITY_FLOOR = 0.82
BOOKS_SIMILARITY_CEILING = 0.90


def _spread(similarity: float, floor: float, ceiling: float) -> float:
    return min(1.0, max(0.0, (similarity - floor) / (ceiling - floor)))


def similarity_part(
    similarity: float | None, *, by_name: bool, author_matched: bool = False
) -> float:
    """관심사별 목록에서 뽑은 책의 유사도 항(0–1). 책 벡터가 없으면 0.

    similarity 는 검색어가 관심사 이름의 벡터(by_name)면 그 벡터와의 코사인 유사도이고, 그 관심사의 내 책으로
    뽑았으면 내 책 가운데 가장 비슷한 책과의 코사인 유사도다(#483). 내 책을 모르면 책 벡터 평균과의 내적, 곧
    그 책들과의 유사도 평균을 쓴다(평균은 길이를 줄이지 않고 저장한다, app/profile/interests.py). 한 책이
    두 관심사에서 뽑혔으면 이 항이 높은 쪽을 쓴다(#395).

    작가 관심사에서 저자 칸이 그 이름과 맞는 책은 1 이다. 작가 이름의 벡터는 책과 맞을 이유가 없어서다.
    """
    if author_matched:
        return 1.0
    if similarity is None:
        return 0.0
    if by_name:
        return _spread(similarity, NAME_SIMILARITY_FLOOR, NAME_SIMILARITY_CEILING)
    return _spread(similarity, BOOKS_SIMILARITY_FLOOR, BOOKS_SIMILARITY_CEILING)


def share_parts(seats: Mapping[str, int]) -> dict[str, float]:
    """관심사마다 몫 항(0–1). 몫은 그 관심사가 받은 자리 수이고, 가장 많이 받은 관심사를 1 로 친다(#395)."""
    most = max(seats.values(), default=0)
    if most <= 0:
        return {}
    return {name: count / most for name, count in seats.items()}


def match_score(similarity_part: float, share_part: float) -> int:
    """매칭 점수(0–100). 유사도 항 60%, 그 관심사의 몫 항 25%.

    인기도 항(15%)은 자리만 두고 아직 더하지 않아, 가장 높은 점수는 85 다(#395).
    """
    total = SIMILARITY_WEIGHT * similarity_part + CATEGORY_WEIGHT * share_part
    return round(total * 100)
