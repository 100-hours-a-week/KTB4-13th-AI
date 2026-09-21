"""임베딩 모델 어댑터 — multilingual-e5-small 자체 서빙 (CPU).

② /embeddings 가 쓰는 유일한 경로다. 다른 모델로 폴백하지 않는다 —
모델이 다르면 좌표계가 달라져 저장된 벡터와 비교할 수 없기 때문이다(6단계 설계).
장애 시에는 ① 키워드 전용, ④ 규칙 점수만으로 강등한다.
"""

import asyncio
import threading
import time

from sentence_transformers import SentenceTransformer

from app.core.config import get_settings

# e5 계열은 입력 앞에 용도 접두어를 붙여야 학습된 대로 동작한다.
# 붙이지 않으면 오류 없이 품질만 떨어지고, 한 번 색인한 뒤에는
# 전량 재임베딩 없이 규칙을 바꿀 수 없다. 명세의 purpose 와 1:1로 맞춘다.
_PREFIX = {"query": "query: ", "document": "passage: "}

_model: SentenceTransformer | None = None
# 첫 요청 두 개가 동시에 오면 모델을 두 번 읽어 메모리가 두 배가 된다.
_load_lock = threading.Lock()

# 읽기에 실패했을 때 다음 시도까지 기다리는 시간(초).
# 캐싱하지 않으면 요청마다 1.4GB 모델 읽기를 처음부터 다시 시도해
# 스레드와 CPU를 잡아먹고 장애를 키운다. 이 시간 동안은 바로 실패시킨다.
_LOAD_RETRY_COOLDOWN_SECONDS = 60.0
_load_failed_at: float | None = None
_load_error: Exception | None = None


def build_inputs(texts: list[str], purpose: str) -> list[str]:
    """용도 접두어를 붙인다. purpose 가 enum 밖이면 document 로 본다(명세 기본값)."""
    prefix = _PREFIX.get(purpose, _PREFIX["document"])
    return [prefix + t for t in texts]


def _build_model() -> SentenceTransformer:
    """모델 파일을 실제로 읽는다. 첫 호출은 내려받기 때문에 수십 초 걸린다."""
    settings = get_settings()
    model = SentenceTransformer(settings.embedding_model, device="cpu")
    dim = model.get_embedding_dimension()
    # DDL 의 vector(384) 와 어긋난 모델을 그대로 서빙하면 저장 단계에서야
    # 실패하거나, 차원이 같은 다른 모델이면 조용히 섞인다. 여기서 끊는다.
    if dim != settings.embedding_dim:
        raise RuntimeError(
            f"모델 차원({dim})이 설정값({settings.embedding_dim})과 다릅니다. "
            f"모델: {settings.embedding_model}"
        )
    return model


def load_model() -> SentenceTransformer:
    """모델을 한 번만 읽어 재사용한다.

    읽기에 실패하면 실패한 시각을 기억해 두고, 쿨다운 동안 들어온 요청은
    모델을 다시 읽지 않고 곧바로 실패시킨다. 그렇게 하지 않으면 요청마다
    1.4GB 읽기를 새로 시도해 스레드와 CPU가 말라붙는다.
    """
    global _model, _load_failed_at, _load_error
    if _model is not None:
        return _model

    with _load_lock:
        if _model is not None:
            return _model

        if (
            _load_failed_at is not None
            and time.monotonic() - _load_failed_at < _LOAD_RETRY_COOLDOWN_SECONDS
        ):
            raise RuntimeError(
                f"임베딩 모델 읽기가 최근에 실패해 {_LOAD_RETRY_COOLDOWN_SECONDS:.0f}초 동안 "
                "다시 시도하지 않습니다"
            ) from _load_error

        try:
            model = _build_model()
        except Exception as exc:
            _load_failed_at = time.monotonic()
            _load_error = exc
            raise

        _model = model
        _load_failed_at = None
        _load_error = None
    return _model


def is_loaded() -> bool:
    """⑧ /health 의 embedding 칸. 모델을 읽는 부작용 없이 상태만 본다."""
    return _model is not None


async def embed(texts: list[str], purpose: str) -> tuple[list[list[float]], int, str]:
    """텍스트를 벡터로 만든다. (vectors, dim, model) 을 돌려준다.

    encode 는 CPU 를 오래 쓰는 동기 함수라 그대로 부르면 이벤트 루프가 멈춘다.
    스레드로 넘겨 다른 요청이 계속 처리되게 한다.
    """
    settings = get_settings()
    model = await asyncio.to_thread(load_model)
    inputs = build_inputs(texts, purpose)

    vectors = await asyncio.to_thread(
        model.encode,
        inputs,
        # 코사인 유사도로 검색하므로(pgvector vector_cosine_ops) 미리 정규화한다.
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    out: list[list[float]] = vectors.tolist()
    # dim 은 설정값이 아니라 실제로 만든 벡터의 길이를 돌려준다.
    # 호출자는 이 값으로 자기 인덱스 차원과 대조하므로(명세 ②),
    # 설정값을 그대로 복창하면 검사가 아무것도 걸러내지 못한다.
    dim = len(out[0]) if out else settings.embedding_dim
    return out, dim, settings.embedding_model
