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
        # 개수가 어긋나는 것도 실패로 본다. ⑥ 요청 안에서도 불리므로(ensure_loaded) 밖으로 새면 500 이 된다.
        loaded = dict(zip(LABELS, vectors, strict=True))
    except Exception:
        logger.exception(
            "온보딩 라벨 벡터를 만들지 못했습니다. ⑥은 라벨을 빼고 계산합니다"
        )
        return
    _vectors = loaded


async def ensure_loaded() -> None:
    """기동 때 못 만들었는데 지금은 모델이 떠 있으면 만든다. 모델을 새로 읽지는 않는다.

    기동 때 모델이 실패했다가 ② 요청으로 나중에 뜨면, 이게 없을 때는 서버를 다시 켤 때까지 ⑥이
    조용히 라벨을 빼고 계산한다. 라벨 13개는 0.1초 남짓이라 요청 안에서 만들어도 된다.
    """
    if not _vectors and embedding.is_loaded():
        await load()


def vector(label: str) -> list[float] | None:
    """라벨의 벡터. 목록에 없는 라벨이거나 아직 못 만들었으면 None."""
    return _vectors.get(label)
