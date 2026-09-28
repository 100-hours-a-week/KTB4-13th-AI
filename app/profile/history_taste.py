"""취향 프로필이 없을 때 구매·리뷰 이력만으로 만드는 임시 취향(#246).

명세 ⑥은 "구매나 리뷰가 하나라도 있으면 개인화를 켠다"인데, ⑥은 온보딩 완료·기억 변경 때만 불린다
(명세 호출 시점표). 그래서 ⑥ 때 재료가 없던 사용자는 이력이 쌓여도 취향 벡터가 없는 채로 남아 계속
개인화가 꺼진다. ③④는 저장된 벡터가 없을 때만 이 함수로 요청 때 임시 취향을 만들어 쓴다.

저장하지 않는다. ⑥이 불려 벡터가 생기면 그쪽이 이긴다. 재료와 무게는 ⑥과 같고, 좋아한 책·라벨·기억은
⑥ 요청으로만 들어오는 값이라 여기엔 없다.
"""

from dataclasses import dataclass
from datetime import datetime

import asyncpg

from app.core import history
from app.profile import compute, service


@dataclass
class HistoryTaste:
    # 벡터가 있는 이력 책이 하나도 없으면 None 이다. 이때는 개인화를 켤 수 없다.
    centroid: list[float] | None
    # ⑥ 이 저장하는 태그 가중치와 같은 모양(카테고리별 이력 점수, 0 인 칸은 뺌).
    tag_weights: dict[str, int]


async def from_history(
    conn: asyncpg.Connection, user_id: int, until: datetime | None = None
) -> HistoryTaste:
    """until 까지의 이력으로 취향 벡터와 태그 가중치를 만든다. ⑥ 이 이력만 있을 때 만드는 값과 같다."""
    hist = await history.read(conn, user_id, until)
    weights = compute.book_weights([], hist.weights, hist.disliked_book_ids)
    vectors = await service.book_vectors(conn, list(weights))
    parts = [(vectors[b], w) for b, w in weights.items() if b in vectors]
    return HistoryTaste(
        centroid=compute.centroid(parts),
        tag_weights=compute.tag_weights([], [], hist.category_scores),
    )
