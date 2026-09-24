"""⑥ 취향 프로필 계산 — 취향 벡터, 태그 가중치, 판 번호. DB 없이 돈다.

취향 벡터는 재료 벡터들의 가중평균이다(명세 ⑥). 재료는 좋아한 책·이력 책의 책 벡터, 취향 기억의
문장 벡터, 온보딩에서 고른 카테고리·태그의 라벨 벡터다(#94).
"""

import math
import struct
from dataclasses import dataclass

from app.core import categories

# 명세는 이력의 비중(구매 3, 리뷰 4.0점 이상 2, 담기 1)만 정했다. 아래는 우리가 정한 임시값이다(#94, #109).
# 바꿀 때는 여기만 고친다. 저장된 프로필은 ⑥이 다시 불릴 때까지 옛 값이다.
# 좋아한 책은 사용자가 직접 고른 책이라 좋은 리뷰(2)와 같게 둔다.
LIKED_BOOK_WEIGHT = 2.0
MEMORY_WEIGHT = 1.0
# 온보딩에서 고른 라벨은 몇 개를 고르든 모두 합쳐 이 비중을 나눠 가진다. 하나당 1이면 태그 9개가 책
# 한 권을 9 : 2로 누른다. 좋아한 책(2)과 같게 두면 결이 다른 카테고리 하나가 그 책 쪽 목록을 거의 다
# 밀어낸다(피드 위 20권 중 0–15%만 남음, #164). 카테고리는 ④ 채점의 카테고리 점수로도 들어가므로
# 벡터에서는 기억 한 문장만큼만 둔다.
LABELS_TOTAL_WEIGHT = 1.0
ONBOARDING_TAG_WEIGHT = 1
# 온보딩 카테고리는 대응표(#110)로 카탈로그 분류 점수로 풀어 이력의 카테고리 점수와 합친다. 직접 고른
# 관심사라 핵심 분류는 좋은 리뷰 한 번(2)과 같게, 다른 내용이 섞인 일부 분류는 그 절반으로 둔다.
ONBOARDING_CORE_WEIGHT = 2
ONBOARDING_PARTIAL_WEIGHT = 1


@dataclass
class Profile:
    # 재료가 하나도 없으면 None 이다. 이때 cold_start 다.
    centroid: list[float] | None
    tag_weights: dict[str, int]
    cold_start: bool


def book_weights(
    liked: list[int], history_weights: dict[int, int], disliked: set[int]
) -> dict[int, float]:
    """취향 벡터에 넣을 책마다 가중치 하나. 좋아한 책과 이력 책을 합친다.

    온보딩에서 고른 책은 "내 서재에 담고"로 나의 도서관에도 들어가(피그마 ONBOARD-005), 같은
    책이 좋아한 책(2)과 담기(1)로 두 번 들어올 수 있다. 두 번 세면 온보딩 책만 두 배로 끌어당기므로
    이력과 같은 규칙으로 큰 값 하나만 쓴다.

    싫다고 한 책(2.0점 이하 리뷰)은 좋아한 책이거나 산 책이어도 뺀다(명세 ⑥ 각주). 이력 가중치는
    큰 값 하나라 사고 1점을 준 책이 3 으로 남으므로, 가중치가 아니라 disliked 목록으로 가린다.
    """
    weights = {book_id: LIKED_BOOK_WEIGHT for book_id in liked}
    for book_id, weight in history_weights.items():
        if weight > weights.get(book_id, 0):
            weights[book_id] = weight
    return {
        book_id: weight
        for book_id, weight in weights.items()
        if weight > 0 and book_id not in disliked
    }


def label_parts(vectors: list[list[float]]) -> list[tuple[list[float], float]]:
    """고른 라벨 벡터들에 LABELS_TOTAL_WEIGHT 를 똑같이 나눠 준다."""
    if not vectors:
        return []
    each = LABELS_TOTAL_WEIGHT / len(vectors)
    return [(vector, each) for vector in vectors]


def centroid(parts: list[tuple[list[float], float]]) -> list[float] | None:
    """(벡터, 가중치) 들의 가중평균을 길이 1로 맞춰 돌려준다. 쓸 재료가 없으면 None.

    길이를 1로 맞추면 가중치 합으로 나눌 필요가 없다(방향만 남는다). 책 벡터도 길이 1이라
    ③④가 내적으로 비교해도 코사인과 같은 값이 된다. 가중치가 0 이하인 재료는 쓰지 않는다
    — 2.0점 이하 리뷰(−2)는 취향 벡터에 넣지 않고 카테고리 점수만 깎는다(명세 ⑥).
    """
    used = [(vector, weight) for vector, weight in parts if weight > 0]
    if not used:
        return None
    total = [0.0] * len(used[0][0])
    for vector, weight in used:
        for i, x in enumerate(vector):
            total[i] += weight * x
    norm = math.sqrt(sum(x * x for x in total))
    if norm == 0:
        return None
    return [x / norm for x in total]


def tag_weights(
    tags: list[str], onboarding_categories: list[str], category_scores: dict[str, int]
) -> dict[str, int]:
    """온보딩 태그·카테고리와 이력의 카테고리 점수를 한 맵으로 합친다. 합이 0인 칸은 뺀다.

    온보딩 카테고리는 이름 그대로가 아니라 카탈로그 분류로 풀어 넣는다. ④가 책의 `category` 로 점수를
    찾기 때문이다. 기억 종류 집계와 읽는 시간대·고르는 기준은 명세에 합치는 방법이 없어 아직 넣지
    않는다(#92).
    """
    weights = dict(category_scores)
    picked = categories.catalog_scores(
        onboarding_categories, ONBOARDING_CORE_WEIGHT, ONBOARDING_PARTIAL_WEIGHT
    )
    for category, points in picked.items():
        weights[category] = weights.get(category, 0) + points
    for tag in tags:
        weights[tag] = weights.get(tag, 0) + ONBOARDING_TAG_WEIGHT
    return {key: value for key, value in weights.items() if value}


def _as_float32(vector: list[float]) -> list[float]:
    return [struct.unpack("f", struct.pack("f", x))[0] for x in vector]


def _same_centroid(old: list[float] | None, new: list[float] | None) -> bool:
    if old is None or new is None:
        return old is new
    # DB 는 4바이트 실수로 저장한다. 계산값(8바이트)과 그대로 비교하면 늘 다르게 나와
    # 호출마다 판 번호가 오른다. 저장 정밀도로 맞춘 뒤 비교한다.
    return _as_float32(old) == _as_float32(new)


def next_version(stored: tuple[Profile, int] | None, new: Profile) -> int:
    """처음이면 1, 순위에 영향을 주는 값(취향 벡터·태그 가중치·cold_start)이 바뀌었을 때만 +1.

    피드 커서에 실리는 번호라, 바뀐 게 없는데 오르면 보던 목록이 괜히 끊긴다(명세 ⑥).
    """
    if stored is None:
        return 1
    old, version = stored
    unchanged = (
        old.cold_start == new.cold_start
        and old.tag_weights == new.tag_weights
        and _same_centroid(old.centroid, new.centroid)
    )
    return version if unchanged else version + 1
