"""고치기 전(기준)과 후(후보)의 평가 결과를 같은 사례끼리 짝지어 판정할 때 쓰는 계산.

검색(#752)과 취향 뽑기(#753)의 판정 규칙이 함께 쓴다. 규칙 자체(어느 값이 몇이면 탈락인지)는 각 평가에 둔다.
DB 와 LLM 을 쓰지 않는다. 시드가 같으면 같은 구간이 나온다. V3 판정(검색 #556 시드 556, 취향 뽑기 #569 시드 569)과
같은 계산이라, 같은 결과 파일을 넣으면 그때의 구간이 그대로 다시 나온다.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

# 짝 부트스트랩에서 사례를 다시 뽑는 횟수
BOOTSTRAP = 10_000
ALPHA = 0.05
# 서술형 검색어에서 화면에 나온 책 가운데 라벨이 있는 책의 비율(Judged)이 이보다 낮으면, 라벨 없는 책이 많아
# 점수를 믿을 수 없다(#556 「품질 지표와 라벨」)
JUDGED_MIN = 0.9


@dataclass(frozen=True)
class Interval:
    """짝지은 차이(후보 − 기준)의 평균과 95% 구간. p 는 여러 후보를 한꺼번에 볼 때(Holm)만 쓴다."""

    mean: float
    low: float
    high: float
    p: float


def bootstrap(diffs: Sequence[float], *, seed: int) -> Interval:
    """사례마다의 차이로 평균과 짝 부트스트랩 95% 구간(백분위)을 낸다.

    사례를 같은 수만큼 중복을 허락해 다시 뽑아 평균을 내는 일을 BOOTSTRAP 번 하고, 그 평균들의 2.5%·97.5% 지점을
    구간으로 삼는다. p 는 다시 뽑은 평균이 0 의 반대쪽에 놓인 비율의 두 배(양쪽)다. 차이가 모두 0이면 구간도 0이다.
    """
    d = np.asarray(diffs, dtype=float)
    if d.size == 0:
        raise ValueError("짝지은 사례가 없다")
    if not d.any():
        return Interval(0.0, 0.0, 0.0, 1.0)
    rng = np.random.default_rng(seed)
    means = d[rng.integers(0, d.size, size=(BOOTSTRAP, d.size))].mean(axis=1)
    low, high = np.percentile(means, [2.5, 97.5])
    p = min(1.0, 2 * min(float((means <= 0).mean()), float((means >= 0).mean())))
    return Interval(float(d.mean()), float(low), float(high), p)


def mcnemar(lost: int, gained: int) -> float:
    """맞힘·못 맞힘처럼 둘로 갈리는 결과에서, 한쪽만 맞힌 사례만 세는 McNemar 정확 검정의 양쪽 p 값.

    lost 는 기준만 맞힌 사례 수, gained 는 후보만 맞힌 사례 수다. 둘 다 맞히거나 둘 다 틀린 사례는 판정에 들어가지 않는다.
    엇갈린 사례가 없으면 1 이다.
    """
    if lost < 0 or gained < 0:
        raise ValueError("사례 수는 0 이상이어야 한다")
    n = lost + gained
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(lost, gained) + 1)) / 2**n
    return min(1.0, 2 * tail)


def holm(pvalues: Mapping[str, float], *, alpha: float = ALPHA) -> dict[str, bool]:
    """후보 여럿을 한꺼번에 볼 때 우연히 하나쯤 넘는 일을 막는 Holm 보정. 이름마다 넘었는지를 돌려준다.

    작은 p 부터 alpha / (남은 후보 수) 와 견주고, 처음 못 넘은 뒤로는 모두 못 넘은 것으로 본다.
    """
    passed = dict.fromkeys(pvalues, False)
    ordered = sorted(pvalues, key=lambda name: pvalues[name])
    for i, name in enumerate(ordered):
        if pvalues[name] >= alpha / (len(ordered) - i):
            break
        passed[name] = True
    return passed


def enough_labels(*judged: float, minimum: float = JUDGED_MIN) -> bool:
    """견주는 결과 모두에서 Judged 평균이 문턱 이상인가. 하나라도 낮으면 라벨을 보충한 뒤에 판정한다."""
    if not judged:
        raise ValueError("Judged 값이 없다")
    return min(judged) >= minimum
