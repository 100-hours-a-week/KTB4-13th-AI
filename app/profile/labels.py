"""⑥ 온보딩 라벨 벡터 — 서버가 뜰 때 한 번 만들어 메모리에 둔다(#94).

명세 ⑥에는 503·504가 없어, 요청 중에 임베딩 모델을 부르지 않는다. 라벨은 대응표(#110)에 있는
온보딩 카테고리다. 세부 태그는 전체 목록이 아직 없어 넣지 않았다(#165).
"""

import logging

from app.core import categories
from app.gateway import embedding

logger = logging.getLogger(__name__)

LABELS: tuple[str, ...] = tuple(categories.ONBOARDING_TO_CATALOG)

# 기억 문장처럼 사용자가 고른 관심사를 나타내므로 기억과 같은 용도로 만든다.
_PURPOSE = "query"

_vectors: dict[str, list[float]] = {}


async def load() -> None:
    """라벨 벡터를 만든다. 실패하면 ERROR 로그만 남기고 빈 채로 둔다.

    라벨 벡터가 없어도 ⑥은 라벨을 빼고 계산할 수 있어, 기동을 막지 않는다.
    """
    global _vectors
    try:
        vectors, _, _ = await embedding.embed(list(LABELS), _PURPOSE)
    except Exception:
        logger.exception(
            "온보딩 라벨 벡터를 만들지 못했습니다. ⑥은 라벨을 빼고 계산합니다"
        )
        return
    _vectors = dict(zip(LABELS, vectors, strict=True))


def vector(label: str) -> list[float] | None:
    """라벨의 벡터. 목록에 없는 라벨이거나 아직 못 만들었으면 None."""
    return _vectors.get(label)
