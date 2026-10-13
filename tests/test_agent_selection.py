"""⑦ 조합 고르기(app/agent/selection.py) 테스트(#363). 입출력이 없는 계산이라 LLM 도 BE 도 쓰지 않는다.

조합은 코드가 정한다. 여기서는 그 규칙이 정답과 같은지를 본다: 예산 이하, 점수 합 최대, 동점이면 싼 쪽,
그래도 같으면 카드 번호가 앞선 쪽. 평가 사례(evals/agent)의 정답을 낼 때 쓰는 전수 계산과 같은 것이다.
"""

from itertools import combinations

import pytest

from app.agent.selection import Candidate, alternatives, choose


def _c(rank: int, price: int, score: int) -> Candidate:
    return Candidate(f"isbn{rank}", f"책{rank}", price, score, rank)


def _isbns(chosen) -> list[str] | None:
    return None if chosen is None else [c.isbn for c in chosen]


def test_예산_안에서_점수_합이_가장_큰_조합을_고른다() -> None:
    # 1+2 는 예산을 넘고, 남은 조합 중 점수 합이 가장 큰 것은 1+3 이다
    cards = [_c(1, 20000, 92), _c(2, 15000, 87), _c(3, 10000, 81)]

    assert _isbns(choose(cards, 30000, 2)) == ["isbn1", "isbn3"]


def test_합계가_예산과_같아도_된다() -> None:
    cards = [_c(1, 15000, 95), _c(2, 15000, 90), _c(3, 18000, 85)]

    assert _isbns(choose(cards, 30000, 2)) == ["isbn1", "isbn2"]


def test_예산_안에_드는_조합이_없으면_None이다() -> None:
    cards = [_c(1, 20000, 92), _c(2, 15000, 87)]

    assert choose(cards, 10000, 2) is None


def test_카드보다_많은_권수를_달라면_None이다() -> None:
    assert choose([_c(1, 1000, 90)], 100000, 2) is None
    assert choose([], 100000, 1) is None


def test_한_권만_고른다() -> None:
    cards = [_c(1, 25000, 95), _c(2, 18000, 90), _c(3, 12000, 80)]

    assert _isbns(choose(cards, 20000, 1)) == ["isbn2"]  # 1번은 예산 밖이다


def test_점수_합이_같으면_합계가_낮은_조합이다() -> None:
    # 예산 26,000원에서 1+4 와 2+3 이 점수 합 150 으로 같다. 1+2, 1+3 은 예산을 넘는다.
    # 합계는 1+4 가 25,000원, 2+3 이 19,000원이라 2+3 이다
    cards = [_c(1, 20000, 90), _c(2, 9000, 80), _c(3, 10000, 70), _c(4, 5000, 60)]

    assert _isbns(choose(cards, 26000, 2)) == ["isbn2", "isbn3"]


def test_점수와_합계가_모두_같으면_카드_번호가_앞선_조합이다() -> None:
    cards = [_c(1, 10000, 80), _c(2, 10000, 80), _c(3, 10000, 80)]

    assert _isbns(choose(cards, 20000, 2)) == ["isbn1", "isbn2"]


def test_받은_순서와_상관없이_같은_답이다() -> None:
    cards = [_c(1, 20000, 92), _c(2, 15000, 87), _c(3, 10000, 81)]

    assert _isbns(choose(list(reversed(cards)), 30000, 2)) == ["isbn1", "isbn3"]


def test_고른_책은_카드_번호_순서로_돌려준다() -> None:
    cards = [_c(3, 10000, 99), _c(1, 10000, 98), _c(2, 10000, 97)]

    assert _isbns(choose(cards, 20000, 2)) == ["isbn1", "isbn3"]


def test_따져_보는_조합_수는_후보_수_중_권수를_고르는_수다() -> None:
    assert alternatives(3, 2) == 3  # 명세 ⑦ 예시와 같다
    assert alternatives(10, 5) == 252
    assert alternatives(2, 3) == 0


@pytest.mark.parametrize("budget", [5000, 12000, 21000, 26000, 31000, 99000])
@pytest.mark.parametrize("count", [1, 2, 3])
def test_전수_계산과_모든_예산_권수에서_같다(budget, count) -> None:
    cards = [
        _c(1, 9000, 88),
        _c(2, 12000, 95),
        _c(3, 7000, 60),
        _c(4, 15000, 91),
        _c(5, 4000, 70),
    ]
    best = None
    for combo in combinations(cards, count):
        if sum(c.price for c in combo) > budget:
            continue
        key = (
            -sum(c.match_score for c in combo),
            sum(c.price for c in combo),
            tuple(c.rank for c in combo),
        )
        if best is None or key < best[0]:
            best = (key, combo)

    chosen = choose(cards, budget, count)

    assert _isbns(chosen) == (None if best is None else [c.isbn for c in best[1]])
