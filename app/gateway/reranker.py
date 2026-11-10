"""③ 후보 다시 줄 세우기 — 교차 인코더(bge-reranker-v2-m3) 자체 서빙 (CPU, #573).

검색은 질문과 책을 따로 벡터로 만들어 가까운 순으로 고른다. 이 모델은 질문과 책 글(제목·저자·소개)을 한 번에
같이 읽고 얼마나 맞는지 매긴다. 검색이 고른 30권을 이 점수로 다시 줄 세워, 3단계(카드)에 넘길 10권을 고른다.
#573 실험에서 10권 nDCG 0.578 → 0.685. 같은 신호(벡터 유사도)로 순서만 바꾼 #523 은 카드가 그대로였다.

리랭커 때문에 답이 막히지 않는다. 모델이 없거나, 메모리 한도가 모자라 띄우지 않았거나, 다른 요청이 쓰는 중이거나,
시간을 넘기거나, 실패하면 모두 검색 순서를 그대로 돌려준다.
"""

import asyncio
import json
import logging
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sentence_transformers import CrossEncoder

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_model: CrossEncoder | None = None

# 추론은 프로세스에서 한 번에 하나만 한다. CPU 두 개에서 두 요청이 같이 돌면 둘 다 느려져 시간 제한을 같이 넘긴다.
# 일꾼 스레드를 하나만 두고, 그 스레드가 일하는 동안 온 요청은 기다리지 않고 검색 순서를 쓴다.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reranker")
_slot = threading.Lock()

# 메모리 한도를 읽는 곳. 컨테이너 안에서 보이는 cgroup 값이 먼저고, 없으면 ECS 태스크 메타데이터를 본다.
_CGROUP_LIMIT_FILES = (
    Path("/sys/fs/cgroup/memory.max"),  # cgroup v2
    Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),  # cgroup v1
)
# cgroup v1 은 한도가 없을 때 아주 큰 수를 적는다. 이보다 크면 한도 없음으로 본다.
_NO_LIMIT_BYTES = 1 << 60
# 서버 전체에 남은 메모리를 읽는 곳(_server_available_mib)
_MEMINFO = Path("/proc/meminfo")


def _memory_limit_mib() -> int | None:
    """이 컨테이너(또는 ECS 태스크)의 메모리 한도(MiB). 한도가 없거나 못 읽으면 None(로컬 개발 환경)."""
    for path in _CGROUP_LIMIT_FILES:
        try:
            raw = path.read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < _NO_LIMIT_BYTES:
            return int(raw) // (1024 * 1024)
    # ECS 가 태스크 단위로만 한도를 걸면 그 한도는 컨테이너의 cgroup 에 안 보이고 메타데이터에만 있다.
    base = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if base:
        try:
            with urllib.request.urlopen(f"{base}/task", timeout=2) as resp:
                memory = json.load(resp).get("Limits", {}).get("Memory")
        except Exception:
            logger.warning(
                "ECS 태스크 메타데이터에서 메모리 한도를 못 읽었습니다", exc_info=True
            )
            return None
        if isinstance(memory, int | float) and memory > 0:
            return int(memory)
    return None


def _server_available_mib() -> int | None:
    """서버(EC2) 전체에서 지금 더 쓸 수 있는 메모리(MiB). 리눅스가 아니면 None.

    컨테이너 안의 /proc/meminfo 는 컨테이너가 아니라 서버 전체 값이다. 태스크 한도를 서버 크기만큼 올려도
    서버의 운영체제·ECS 에이전트·로그 수집기가 그 메모리를 같이 쓰므로, 한도만 보고 띄우면 서버가 바닥난다.
    """
    try:
        for line in _MEMINFO.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _memory_shortfall() -> str | None:
    """모델을 띄우면 메모리가 모자랄 까닭. 넉넉하거나 잴 수 없으면(로컬 개발 환경) None."""
    settings = get_settings()
    limit = _memory_limit_mib()
    if limit is not None and limit < settings.reranker_min_memory_mib:
        return f"메모리 한도 {limit}MiB 가 리랭커에 필요한 {settings.reranker_min_memory_mib}MiB 보다 작습니다"
    available = _server_available_mib()
    if available is not None and available < settings.reranker_min_free_mib:
        return (
            f"서버에 남은 메모리 {available}MiB 가 리랭커에 필요한 "
            f"{settings.reranker_min_free_mib}MiB 보다 작습니다"
        )
    return None


def load_model() -> None:
    """모델을 읽고 한 번 돌려 둔다. 앱이 뜰 때 뒤쪽 스레드에서 한 번만 부른다(start_loading).

    메모리가 모자라면 읽지 않는다(_memory_shortfall). 지금 앱(약 1.1GB)에 모델(fp32 약 2.3GB)이 더해지면 3GiB
    태스크 한도도, 4GiB 서버(t3.medium)도 넘어 앱이 메모리 부족으로 죽고 다시 뜨기를 반복한다. main 에 머지되면
    개발 서버에 바로 배포되므로, 클라우드가 메모리를 늘리기 전이어도 앱이 죽지 않게 여기서 막는다.
    """
    global _model
    settings = get_settings()
    shortfall = _memory_shortfall()
    if shortfall is not None:
        logger.warning(
            "%s. 리랭커를 띄우지 않고 챗봇 후보는 검색 순서로 고릅니다", shortfall
        )
        return
    limit = _memory_limit_mib()

    started = time.perf_counter()
    model = CrossEncoder(
        settings.reranker_model,
        device="cpu",
        max_length=settings.reranker_max_length,
    )
    # 첫 추론은 두 번째보다 두 배 넘게 걸린다(맥 2스레드 30권 4.9초 vs 2.0초). 첫 사용자 요청이 시간 제한에
    # 걸리지 않게 여기서 미리 한 번 돌린다.
    model.predict([("예열", "예열")], show_progress_bar=False)
    _model = model
    logger.info(
        "리랭커 모델 %s 를 %.1f초 만에 띄웠습니다 (메모리 한도 %s)",
        settings.reranker_model,
        time.perf_counter() - started,
        f"{limit}MiB" if limit is not None else "없음",
    )


def _load_quietly() -> None:
    try:
        load_model()
    except Exception:
        # 모델이 없어도 챗봇은 검색 순서로 계속 답한다.
        logger.exception("리랭커 모델 읽기 실패. 챗봇 후보는 검색 순서로 고릅니다")


def start_loading() -> None:
    """앱이 뜰 때 부른다. 모델 읽기(수 초)를 기다리지 않고 바로 돌아온다. 다 읽기 전 요청은 검색 순서를 쓴다."""
    if not get_settings().chat_reranker:
        logger.info("CHAT_RERANKER 가 꺼져 있어 리랭커를 띄우지 않습니다")
        return
    threading.Thread(target=_load_quietly, name="reranker-load", daemon=True).start()


def is_loaded() -> bool:
    return _model is not None


def _book_text(book: dict) -> str:
    """모델이 질문과 같이 읽을 책 글. 비어 있는 칸은 뺀다."""
    parts = (book.get("title"), book.get("author"), book.get("description"))
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _predict(model: CrossEncoder, pairs: list[tuple[str, str]]) -> list[float]:
    batch_size = get_settings().reranker_batch_size
    scores = model.predict(pairs, batch_size=batch_size, show_progress_bar=False)
    return [float(s) for s in scores]


async def rerank(query: str, candidates: list[dict]) -> list[dict]:
    """질문에 잘 맞는 순서로 다시 줄 세운 새 목록을 돌려준다. 못 하면 받은 목록을 그대로 돌려준다.

    점수가 같으면 검색 순서를 지킨다(sorted 는 안정 정렬).
    """
    model = _model
    if model is None or not query or len(candidates) < 2:
        return candidates
    if not _slot.acquire(blocking=False):
        logger.info("리랭커가 다른 요청을 처리하는 중이라 검색 순서를 씁니다")
        return candidates

    pairs = [(query, _book_text(c)) for c in candidates]
    started = time.perf_counter()
    try:
        future = _executor.submit(_predict, model, pairs)
    except Exception:
        _slot.release()
        logger.exception("리랭커 추론을 시작하지 못해 검색 순서를 씁니다")
        return candidates
    # 시간을 넘겨 기다리기를 그만둬도 스레드의 추론은 끝까지 돈다. 슬롯은 추론이 실제로 끝날 때 푼다.
    # 그동안 온 요청은 위에서 바로 검색 순서를 쓴다.
    future.add_done_callback(lambda _: _slot.release())

    timeout = get_settings().reranker_timeout_seconds
    try:
        scores = await asyncio.wait_for(asyncio.wrap_future(future), timeout=timeout)
    except TimeoutError:
        logger.warning(
            "리랭커가 %.1f초 안에 끝나지 않아 검색 순서를 씁니다 (%d권)",
            timeout,
            len(pairs),
        )
        return candidates
    except Exception:
        logger.exception("리랭커 추론 실패. 검색 순서를 씁니다")
        return candidates

    order = sorted(range(len(candidates)), key=lambda i: -scores[i])
    logger.info("리랭커 %d권 %.2f초", len(pairs), time.perf_counter() - started)
    return [candidates[i] for i in order]
