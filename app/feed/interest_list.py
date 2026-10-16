"""④ 관심사별 홈 추천 목록 — 자리 나누기, 번갈아 놓기, 한 작가 상한(#447), 조립(#452, 설계 #395). DB 없이 돈다.

⑥이 프로필을 계산할 때 200자리 목록을 미리 뽑아 두고, 홈과 더 보기는 그 목록을 읽기만 한다(#395).
관심사마다 책을 뽑는 것은 app/feed/interest_picks.py 다.
"""

import re
from collections import Counter, deque
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass

from app.core import editions
from app.feed import interest_scoring
from app.feed.interest_picks import Pick
from app.profile import interests

# 목록의 자리 수. 홈의 20권은 이 줄의 앞 20권이다. ⑥ 시간 때문에 200으로 둔다(#395)
TOTAL_SEATS = 200
# 자리를 받는 관심사의 수. 넘는 관심사는 점수만 남는다
MAX_INTERESTS = 5
# 한 작가는 이만큼마다 이만큼까지 둔다. 작가를 말한 사람의 홈이 그 작가 책으로만 차지 않게 한다(#395 의 (안))
AUTHOR_BLOCK = 20
AUTHOR_PER_BLOCK = 5

# ⑤ 가 조건을 적는 꼴(app/extraction/rules.py): "300쪽 이하", "1만 원 이하", "1만 5천 원 이하", "9,900원 이하",
# "2024년 이후". 꼴이 바뀌면 여기도 같이 고친다
_PAGES = re.compile(r"(\d+)쪽 (이하|이상)")
_PRICE = re.compile(r"(?:(\d+)만 (?:(\d)천 )?원|([\d,]+)원) (이하|이상)")
_YEAR = re.compile(r"(\d{4})년 (이후|이전)")


@dataclass(frozen=True)
class Condition:
    """책 정보로 가르는 조건 하나. field 는 pages·price·year 이고, upper 면 limit 이하(이전)다."""

    field: str
    limit: int
    upper: bool

    def met_by(self, value: int | None) -> bool:
        """책의 값이 조건을 지키는지. 값을 모르면 지키지 않는 것으로 본다."""
        if value is None:
            return False
        return value <= self.limit if self.upper else value >= self.limit


def parse_condition(text: str) -> Condition | None:
    """⑤ 가 적은 조건을 읽는다. 쪽수·가격·출간연도의 꼴이 아니면 None 이다."""
    if match := _PAGES.fullmatch(text):
        return Condition("pages", int(match[1]), match[2] == "이하")
    if match := _PRICE.fullmatch(text):
        if match[1]:
            amount = int(match[1]) * 10000 + int(match[2] or 0) * 1000
        else:
            amount = int(match[3].replace(",", ""))
        return Condition("price", amount, match[4] == "이하")
    if match := _YEAR.fullmatch(text):
        return Condition("year", int(match[1]), match[2] == "이전")
    return None


def meets_all(conditions: Sequence[Condition], facts: Mapping[str, int | None]) -> bool:
    """책 정보(pages·price·year)가 조건을 모두 지키는지. 조건이 없으면 거짓이라 순서를 바꾸지 않는다."""
    return bool(conditions) and all(c.met_by(facts.get(c.field)) for c in conditions)


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


def cap_authors[T](
    items: Sequence[T],
    author_of: Callable[[T], str | None],
    block: int = AUTHOR_BLOCK,
    per_block: int = AUTHOR_PER_BLOCK,
) -> list[T]:
    """한 작가는 block 권마다 per_block 권까지 둔다. 넘는 책은 순서를 지킨 채 다음 묶음의 앞으로 미룬다.

    작가를 모르는 책은 세지 않는다.
    """
    pending = deque(items)
    out: list[T] = []
    while pending:
        counts: Counter[str] = Counter()
        placed: list[T] = []
        deferred: list[T] = []
        while pending and len(placed) < block:
            item = pending.popleft()
            author = author_of(item)
            if author and counts[author] >= per_block:
                deferred.append(item)
                continue
            if author:
                counts[author] += 1
            placed.append(item)
        out.extend(placed)
        pending.extendleft(reversed(deferred))
    return out


@dataclass(frozen=True)
class Placed:
    """목록의 한 자리. 어느 관심사에서 왔는지와 매칭 점수(0–100)."""

    book_id: int
    interest: str
    match_score: int


def _edition(pick: Pick) -> tuple[str, str]:
    """같은 책의 판본을 묶는 키. 제목이나 저자를 모르면 그 책만의 키다(저자 칸이 빈 키와 겹치지 않음)."""
    return editions.edition_key(pick.title, pick.author) or (str(pick.book_id), "")


def assemble(
    picks: Mapping[str, Sequence[Pick]],
    seats: Mapping[str, int],
    *,
    keep: Callable[[Pick], bool] = lambda _pick: True,
    meets: Callable[[Pick], bool] = lambda _pick: False,
    total: int = TOTAL_SEATS,
) -> list[Placed]:
    """관심사마다 뽑은 책으로 목록을 만든다. seats 는 allocate 가 나눈 자리다.

    관심사마다 keep 이 거르는 책(가진 책과 그 판본, 음수인 관심사의 책)을 빼고, 같은 책의 판본은 처음 것만
    남기고, 조건을 모두 지키는 책(meets)을 순서를 지킨 채 앞으로 옮긴다. 그다음 자리 비율대로 번갈아 놓고
    (같은 책의 판본은 처음 놓인 자리에만), 한 작가 상한을 건다.

    매칭 점수의 유사도 항은 그 책을 뽑은 관심사들 가운데 높은 쪽이고(#395), 몫 항은 놓인 관심사의 것이다.
    """
    best: dict[tuple[str, str], float] = {}
    chosen: dict[tuple[str, tuple[str, str]], Pick] = {}
    lists: dict[str, list[tuple[str, str]]] = {}
    for name, got in picks.items():
        keys: list[tuple[str, str]] = []
        for pick in got:
            if not keep(pick):
                continue
            key = _edition(pick)
            best[key] = max(best.get(key, 0.0), pick.part)
            if (name, key) not in chosen:
                chosen[(name, key)] = pick
                keys.append(key)
        lists[name] = sorted(keys, key=lambda key: not meets(chosen[(name, key)]))

    placed = cap_authors(
        interleave(lists, seats, total),
        lambda item: editions.author_key(chosen[item].author) or None,
    )
    shares = interest_scoring.share_parts(seats)
    return [
        Placed(
            book_id=chosen[(name, key)].book_id,
            interest=name,
            match_score=interest_scoring.match_score(best[key], shares[name]),
        )
        for name, key in placed
    ]
