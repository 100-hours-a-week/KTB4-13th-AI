"""⑦ 예산과 권수 안에서 책 조합을 고른다(#363). LLM 이 아니라 코드가 정한다.

LLM 에게 조합을 고르게 하면 예산을 넘기거나 품절 책을 고른다. 후보는 직전 카드 안에서만(#344 에서
확정) 가져오고, 가격·재고는 호출하는 쪽이 BE 에서 읽어 넘긴다. 이 모듈은 입출력이 없는 계산이라 LLM
도 BE 도 없이 시험한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from math import comb


@dataclass(frozen=True)
class Candidate:
    """고를 수 있는 책 한 권. 재고가 있는 카드만 후보다."""

    isbn: str
    title: str
    price: int
    match_score: int
    rank: int  # 사용자에게 보인 카드 번호


def alternatives(candidates: int, count: int) -> int:
    """따져 보는 조합의 수. 예산을 넘는 조합도 따져는 본다."""
    return comb(candidates, count)


def choose(
    candidates: Sequence[Candidate], budget_total: int, count: int
) -> tuple[Candidate, ...] | None:
    """합계가 예산 이하이고 match_score 합이 가장 큰 count권. 없으면 None.

    점수 합이 같으면 합계가 낮은 쪽, 그래도 같으면 카드 번호가 앞선 쪽이다. 카드 수가 최대 10장이라
    조합을 전부 따져도 252개(10권 중 5권)가 넘지 않는다.
    """
    ordered = sorted(candidates, key=lambda c: c.rank)
    best: tuple[Candidate, ...] | None = None
    best_key: tuple[int, int, tuple[int, ...]] | None = None
    for combo in combinations(ordered, count):
        total = sum(c.price for c in combo)
        if total > budget_total:
            continue
        key = (-sum(c.match_score for c in combo), total, tuple(c.rank for c in combo))
        if best_key is None or key < best_key:
            best, best_key = combo, key
    return best
