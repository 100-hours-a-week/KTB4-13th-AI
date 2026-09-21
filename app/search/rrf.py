"""순위 합치기(RRF, Reciprocal Rank Fusion).

키워드 점수와 벡터 유사도는 단위가 달라 점수끼리 더할 수 없다. 그래서 점수는 버리고
각 목록에서 몇 등이었는지만 본다: 책마다 1 / (K + 등수) 를 목록별로 구해 더한다.
두 목록에 다 나온 책이 위로 가고, 한쪽에서만 1등인 책도 상위에 남는다.
"""

# 원 논문이 제안한 값. 클수록 상위 등수 간 차이가 줄어든다.
K = 60


def fuse(
    rankings: list[list[int]], weights: list[float] | None = None, k: int = K
) -> list[int]:
    """여러 순위 목록을 하나로 합친다. 점수가 같으면 book_id 가 작은 쪽이 먼저다(결과 고정).

    weights 를 주면 목록마다 비중을 달리한다(같은 등수라도 비중 큰 목록 쪽이 점수가 높다).
    """
    if weights is None:
        weights = [1.0] * len(rankings)
    scores: dict[int, float] = {}
    for ranking, weight in zip(rankings, weights, strict=True):
        for rank, book_id in enumerate(ranking, start=1):
            scores[book_id] = scores.get(book_id, 0.0) + weight / (k + rank)
    return sorted(scores, key=lambda book_id: (-scores[book_id], book_id))
