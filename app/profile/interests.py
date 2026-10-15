"""⑥ 관심사 점수 — 재료(온보딩, 취향 기억, 책)를 관심사별 점수로 바꾸고 합친다(설계 #395). DB·모델 없이 돈다.

취향 기억은 #435, 책을 관심사로 가르기는 #437, 온보딩·책의 점수와 재료끼리 합치기는 #438 이다.

관심사는 카테고리와 세부 카테고리(이름 그대로), 작가("작가:김영하"), 키워드("키워드:고양이"),
분위기("분위기:잔잔한")다. 취향 기억 한 줄이 어느 관심사에 점수를 주는지는 종류와 어디에로 정한다.

- 주제: 어디에의 이름마다. 이름이 여럿이면 세기를 나눠 준다. 어디에가 비면 키워드:핵심어
- 작가: 작가:핵심어. 분위기: 분위기:핵심어(저장만 하고 아직 쓰지 않는다)
- 제목만 말한 줄(핵심어가 『』)과 조건은 관심사에 점수를 주지 않는다. 조건은 따로 목록으로 모은다

좋아함과 피함이 겹치면 마지막 방향을 따른다. 저장한 때(created_at) 순으로 보고, 마지막 줄과 같은
방향의 줄만 끝에서부터 더한다. 피함의 부탁(-1)은 건너뛴다. 그 대화에서의 부탁이라 싫다는 뜻이 아니다.
"""

import math
import operator
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from app.core import categories

# 관심사 이름의 앞머리. 카테고리와 세부 카테고리는 앞머리 없이 이름 그대로 쓴다
AUTHOR = "작가:"
KEYWORD = "키워드:"
MOOD = "분위기:"

# 피함의 부탁("이번엔 ○○ 말고"). ⑤가 응답에서 빼서 오지 않지만, 오더라도 건너뛴다
REQUEST_TO_AVOID = -1
# 조건은 늘 지킨다고 말한 것(세기 2 이상)만 목록에 넣는다. ⑤도 그것만 내지만 여기서 한 번 더 본다
MIN_CONDITION_STRENGTH = 2


class MemoryLine(Protocol):
    """취향 기억 한 줄에서 관심사 계산에 쓰는 칸(명세 ⑥ memories[])."""

    @property
    def type(self) -> str: ...
    @property
    def strength(self) -> int: ...
    @property
    def applies_to(self) -> Sequence[str]: ...
    @property
    def keyword(self) -> str: ...
    @property
    def created_at(self) -> datetime: ...


def _is_title(keyword: str) -> bool:
    return keyword.startswith("『") and keyword.endswith("』")


def interests_of(line: MemoryLine) -> tuple[str, ...]:
    """이 줄이 점수를 주는 관심사. 제목만 말한 줄과 조건은 없다."""
    if line.type == "topic":
        if _is_title(line.keyword):
            return ()
        if line.applies_to:
            return tuple(dict.fromkeys(line.applies_to))
        return (KEYWORD + line.keyword,)
    if line.type == "author":
        return (AUTHOR + line.keyword,)
    if line.type == "mood":
        return (MOOD + line.keyword,)
    return ()


def _in_order(lines: Iterable[MemoryLine]) -> list[MemoryLine]:
    # 저장한 때가 같으면 받은 순서를 지킨다(sorted 는 순서를 지키는 정렬이다)
    return sorted(lines, key=lambda line: line.created_at)


def memory_scores(lines: Iterable[MemoryLine]) -> dict[str, float]:
    """관심사마다 취향 기억의 점수. 마지막 방향을 따른다.

    마지막 줄이 좋아함이면 그 앞의 마지막 피함 뒤에 온 좋아함만 더해 양수, 마지막 줄이 피함이면 그
    앞의 마지막 좋아함 뒤에 온 피함만 더해 음수가 된다. 더하고 뺀 합을 따르면 두 번 좋아한다고 했다가
    한 번 싫다고 해도 계속 추천되기 때문이다(#395).
    """
    shares: dict[str, list[float]] = {}
    for line in _in_order(lines):
        if line.strength == REQUEST_TO_AVOID:
            continue
        names = interests_of(line)
        for name in names:
            shares.setdefault(name, []).append(line.strength / len(names))

    scores: dict[str, float] = {}
    for name, values in shares.items():
        positive = values[-1] > 0
        total = 0.0
        for value in reversed(values):
            if (value > 0) != positive:
                break
            total += value
        scores[name] = total
    return scores


def conditions(lines: Iterable[MemoryLine]) -> list[str]:
    """조건 목록. 늘 지킨다고 말한 것만, 저장한 때 순으로 한 번씩."""
    return list(
        dict.fromkeys(
            line.keyword
            for line in _in_order(lines)
            if line.type == "condition" and line.strength >= MIN_CONDITION_STRENGTH
        )
    )


def living_likes[T: MemoryLine](lines: Sequence[T]) -> list[T]:
    """챗봇 벡터에 섞을 줄. 세기가 양수이고 조건이 아니며, 그 뒤에 같은 관심사를 피한다고 하지 않은 줄.

    핵심어 벡터에는 좋고 싫음이 담기지 않아서, 싫다고 한 것을 섞으면 그쪽으로 끌려간다(명세 ⑥).
    받은 순서를 지켜 돌려준다.
    """
    last_avoid: dict[str, datetime] = {}
    for line in lines:
        if line.strength >= REQUEST_TO_AVOID:
            continue
        for name in interests_of(line):
            if name not in last_avoid or line.created_at > last_avoid[name]:
                last_avoid[name] = line.created_at
    return [
        line
        for line in lines
        if line.strength > 0
        and line.type != "condition"
        and not any(
            name in last_avoid and last_avoid[name] > line.created_at
            for name in interests_of(line)
        )
    ]


# 책을 가를 때 쓰는 것(#437)
# 문학 분류(카탈로그). 번호가 없는 책이 이 분류면 소설로 보고 이름 벡터로 장르를 고른다
_LITERATURE = frozenset(
    {
        *categories.ONBOARDING_TO_CATALOG["소설"].core,
        *categories.ONBOARDING_TO_CATALOG["소설"].partial,
    }
)
_SUBCATEGORIES = tuple(
    name for group in categories.SUBCATEGORIES.values() for name in group
)


def nearest_subcategory(
    vector: Sequence[float] | None,
    name_vector: Callable[[str], Sequence[float] | None],
) -> str | None:
    """책 벡터와 이름 벡터가 가장 가까운 세부 카테고리. 책 벡터나 이름 벡터가 없으면 None.

    벡터는 모두 길이 1이라 내적이 코사인 유사도다.
    """
    if not vector:
        return None
    best, best_score = None, float("-inf")
    for name in _SUBCATEGORIES:
        named = name_vector(name)
        if named is None:
            continue
        score = sum(map(operator.mul, vector, named))
        if score > best_score:
            best, best_score = name, score
    return best


def book_interest(
    class_no: str | None,
    category: str | None,
    vector: Sequence[float] | None,
    name_vector: Callable[[str], Sequence[float] | None],
) -> str | None:
    """책 한 권이 점수를 주는 관심사 하나(#395 "3. 산 책과 나의 도서관 책").

    1. 세부 분류 번호가 표에 있으면 그 관심사(categories.CLASS_INTERESTS)
    2. 표에 없는 문학 번호(813 등)와, 번호가 없는 문학 책은 책 벡터와 가장 가까운 세부 카테고리
    3. 그 밖의 책은 도서관 분류가 속한 카테고리
    4. 그래도 없으면 2와 같이 가장 가까운 세부 카테고리
    벡터가 필요한데 없으면 None 이다. 소개글이 없어 벡터가 없는 책은 지금처럼 쓰지 않는다.
    """
    by_class = categories.class_interest(class_no)
    if by_class:
        return by_class
    head = (class_no or "").strip()[:3]
    numbered = len(head) == 3 and head.isdigit()
    literature = head.startswith("8") if numbered else category in _LITERATURE
    if not literature:
        for name, match in categories.ONBOARDING_TO_CATALOG.items():
            if category in match.core:
                return name
    return nearest_subcategory(vector, name_vector)


# 온보딩과 책, 재료끼리 합치기(#438)
# 재료 이름. 프로필에 저장하는 점수표의 칸이다: {"추리/스릴러": {"memory": 2, "books": 3}}
ONBOARDING = "onboarding"
MEMORY = "memory"
BOOKS = "books"
# 재료끼리의 무게(#395 "재료끼리의 무게"). 설계 때 잰 값이다(#399). 온보딩은 통틀어 1, 산 책은 한 권(무게
# 3)마다 0.2, 직접 말한 취향은 한 줄(세기 2)마다 0.4. 가입 때 하나 고르고 세 권 사고 한 번 말하면 5 : 3 : 2
# 가 된다. 처음에는 온보딩이 크고 책과 취향 기억이 쌓일수록 그쪽이 커진다. 실제 사용자를 보며 정한다(#362).
ONBOARDING_SHARE = 1.0
SHARE_PER_PURCHASE = 0.2
SHARE_PER_SAID = 0.4
_PURCHASE_WEIGHT = 3.0  # 산 책 한 권의 무게(app/core/history.py)
_SAID_STRENGTH = 2.0  # 직접 말한 취향 한 줄의 세기

_PARENT = {
    name: category
    for category, group in categories.SUBCATEGORIES.items()
    if category
    for name in group
}


def onboarding_scores(
    picked_categories: Iterable[str], tags: Iterable[str]
) -> dict[str, float]:
    """온보딩에서 고른 것의 관심사 점수. 고른 세부 카테고리마다 1, 카테고리만 골랐으면 그 카테고리에 1.

    카테고리와 그 밑의 세부 카테고리를 같이 골랐으면 세부 카테고리에만 준다. 좁혀 고른 것으로 본다(#395
    의 (안)). 서버 목록에 없는 값은 건너뛴다. BE 가 보기를 바꿔도 400 이 아니다(명세 ⑥).
    """
    known = [tag for tag in dict.fromkeys(tags) if tag in categories.NAME_TEXTS]
    scores = dict.fromkeys(known, 1.0)
    narrowed = {_PARENT.get(tag) for tag in known}
    for category in dict.fromkeys(picked_categories):
        if category in categories.CATEGORIES and category not in narrowed:
            scores[category] = 1.0
    return scores


def book_scores(
    books: Iterable[tuple[str, float, Sequence[float]]],
) -> tuple[dict[str, float], dict[str, list[float]]]:
    """(관심사, 책의 무게, 책 벡터) 들 → 관심사마다 무게의 합과, 무게를 준 책 벡터 평균(길이 1).

    벡터 평균은 그 관심사의 책을 찾는 검색어다. 같은 요리라도 그 사람이 산 결(베이킹인지 비건인지)이
    남는다(#395).
    """
    scores: dict[str, float] = {}
    sums: dict[str, list[float]] = {}
    for interest, weight, vector in books:
        scores[interest] = scores.get(interest, 0.0) + weight
        acc = sums.setdefault(interest, [0.0] * len(vector))
        for i, x in enumerate(vector):
            acc[i] += weight * x
    vectors = {}
    for interest, acc in sums.items():
        norm = math.sqrt(sum(x * x for x in acc))
        if norm:
            vectors[interest] = [x / norm for x in acc]
    return scores, vectors


def per_interest(
    onboarding: Mapping[str, float],
    memory: Mapping[str, float],
    books: Mapping[str, float],
) -> dict[str, dict[str, float]]:
    """재료별 점수를 관심사마다 한 칸으로 모은다. 프로필에 이 모양으로 저장한다."""
    out: dict[str, dict[str, float]] = {}
    for source, scores in ((ONBOARDING, onboarding), (MEMORY, memory), (BOOKS, books)):
        for interest, value in scores.items():
            if value:
                out.setdefault(interest, {})[source] = value
    return out


def combined(per_source: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    """관심사마다 점수 하나. 양수인 관심사는 자리를 받고 음수인 관심사는 뺀다(#395).

    재료마다 자기 몫을 자기 관심사들에 점수 비율대로 나눠 주고 더한다. 취향 기억의 마지막 방향이 피함인
    관심사는 온보딩에서 골랐거나 그 관심사의 책을 샀어도 음수다. 싫다고 했는데 또 나오는 것은 한 번만
    겪어도 믿음이 깨진다.
    """
    totals = dict.fromkeys((ONBOARDING, MEMORY, BOOKS), 0.0)
    for scores in per_source.values():
        for source, value in scores.items():
            if source in totals:
                totals[source] += abs(value)
    shares = {
        ONBOARDING: ONBOARDING_SHARE if totals[ONBOARDING] else 0.0,
        MEMORY: SHARE_PER_SAID * totals[MEMORY] / _SAID_STRENGTH,
        BOOKS: SHARE_PER_PURCHASE * totals[BOOKS] / _PURCHASE_WEIGHT,
    }
    whole = sum(shares.values())
    if not whole:
        return {}
    out = {}
    for interest, scores in per_source.items():
        parts = {
            source: shares[source] / whole * value / totals[source]
            for source, value in scores.items()
            if totals.get(source)
        }
        if parts.get(MEMORY, 0.0) < 0:
            out[interest] = parts[MEMORY]
        else:
            out[interest] = sum(parts.values())
    return out
