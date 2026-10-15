"""④ 관심사별 홈 추천 목록 — 자리 나누기, 번갈아 놓기(#447, 설계 #395). DB 없이 돈다.

⑥이 프로필을 계산할 때 200자리 목록을 미리 뽑아 두고, 홈과 더 보기는 그 목록을 읽기만 한다(#395).
관심사마다 책을 뽑는 것과 목록을 조립하는 것은 #449–#452 다.
"""

from collections import deque
from collections.abc import Hashable, Mapping, Sequence

from app.profile import interests

# 목록의 자리 수. 홈의 20권은 이 줄의 앞 20권이다. ⑥ 시간 때문에 200으로 둔다(#395)
TOTAL_SEATS = 200
# 자리를 받는 관심사의 수. 넘는 관심사는 점수만 남는다
MAX_INTERESTS = 5


def allocate(
    scores: Mapping[str, float],
    total: int = TOTAL_SEATS,
    max_interests: int = MAX_INTERESTS,
) -> dict[str, int]:
    """관심사마다 자리 수. 점수가 양수인 관심사 가운데 큰 것부터 max_interests 개까지, 점수에 비례해 나눈다.

    분위기는 아직 자리를 받지 않는다(#395). 나눠 떨어지지 않는 자리는 소수점 아래가 큰 관심사부터 하나씩 준다.
    점수가 같으면 받은 순서를 따른다.
    """
    eligible = sorted(
        (
            (name, score)
            for name, score in scores.items()
            if score > 0 and not name.startswith(interests.MOOD)
        ),
        key=lambda item: -item[1],
    )[:max_interests]
    whole = sum(score for _, score in eligible)
    if not whole:
        return {}
    raw = {name: score / whole * total for name, score in eligible}
    seats = {name: int(share) for name, share in raw.items()}
    remainder = total - sum(seats.values())
    for name in sorted(raw, key=lambda name: -(raw[name] - seats[name]))[:remainder]:
        seats[name] += 1
    return {name: count for name, count in seats.items() if count > 0}


def interleave[T: Hashable](
    lists: Mapping[str, Sequence[T]],
    seats: Mapping[str, int],
    total: int = TOTAL_SEATS,
) -> list[tuple[str, T]]:
    """관심사마다 뽑은 책을 자리 비율대로 번갈아 놓는다. 같은 책은 처음 나온 자리에만 둔다.

    차례는 받은 자리 대비 놓은 수가 가장 적은 관심사에 간다. 자리를 다 채운 관심사는 다른 관심사가 아직
    놓을 책이 있으면 건너뛰고, 어느 관심사의 책이 모자라면 남은 자리를 다른 관심사가 채운다. 설계 때 잰
    방법 그대로다(#399).
    """
    queues = {name: deque(lists.get(name, ())) for name in seats if seats[name] > 0}
    taken = dict.fromkeys(queues, 0)
    used: set[T] = set()
    out: list[tuple[str, T]] = []

    def ready(name: str) -> bool:
        queue = queues[name]
        while queue and queue[0] in used:
            queue.popleft()
        return bool(queue)

    while len(out) < total:
        for name in sorted(queues, key=lambda name: taken[name] / seats[name]):
            if not ready(name):
                continue
            full = taken[name] >= seats[name]
            if full and any(taken[n] < seats[n] and ready(n) for n in queues):
                continue
            item = queues[name].popleft()
            used.add(item)
            out.append((name, item))
            taken[name] += 1
            break
        else:
            break
    return out
