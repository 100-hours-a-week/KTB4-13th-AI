"""⑥ 관심사 점수 — 취향 기억을 관심사별 점수와 조건 목록으로 바꾼다(#435, 설계 #395). DB·모델 없이 돈다.

관심사는 카테고리와 세부 카테고리(이름 그대로), 작가("작가:김영하"), 키워드("키워드:고양이"),
분위기("분위기:잔잔한")다. 취향 기억 한 줄이 어느 관심사에 점수를 주는지는 종류와 어디에로 정한다.

- 주제: 어디에의 이름마다. 이름이 여럿이면 세기를 나눠 준다. 어디에가 비면 키워드:핵심어
- 작가: 작가:핵심어. 분위기: 분위기:핵심어(저장만 하고 아직 쓰지 않는다)
- 제목만 말한 줄(핵심어가 『』)과 조건은 관심사에 점수를 주지 않는다. 조건은 따로 목록으로 모은다

좋아함과 피함이 겹치면 마지막 방향을 따른다. 저장한 때(created_at) 순으로 보고, 마지막 줄과 같은
방향의 줄만 끝에서부터 더한다. 피함의 부탁(-1)은 건너뛴다. 그 대화에서의 부탁이라 싫다는 뜻이 아니다.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

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
