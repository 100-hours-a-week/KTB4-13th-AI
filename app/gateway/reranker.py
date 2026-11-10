"""③ 분위기 요청의 후보를 외부 리랭커 API 로 다시 줄 세운다(#638).

검색은 질문과 책을 따로 벡터로 만들어 가까운 순으로 30권을 고르고, 그 순서대로 10권을 잘라 카드 LLM 에 넘긴다.
리랭커는 질문과 책 글(제목·저자·소개)을 같이 읽고 얼마나 맞는지 매긴다. 그 점수로 줄을 다시 세운 뒤 10권을 자른다.

왜 API 인가(2026-11-10). 서버 안에서 돌리는 bge-reranker-v2-m3 는 운영과 같은 t3 CPU 에서 30권에 22초(int8·64토큰으로
줄여도 8초)가 걸려 쓸 수 없었다(#639). OpenRouter `POST /api/v1/rerank` 로 Cohere rerank-5-fast 를 부르면 서울 AWS 에서
중앙 0.60초·최장 0.92초였다. 서술형 35문항 10권 nDCG 는 검색 순서 0.592 → 0.740(서버 안 모델 0.695). 운영 카드 모델
(gpt-4o)로 카드를 3회씩 만들면 카드 평균 점수는 그대로였고(+0.049, 구분 안 됨) 0점 카드가 105턴 중 12장 → 2장으로
줄었다. 건당 약 $0.00017.

리랭커 때문에 답이 막히지 않는다. 꺼져 있거나, 시간을 넘기거나, 실패하거나, 응답 모양이 틀리면 받은 목록을 그대로
돌려준다(검색 순서).

이 API 는 ZDR 엔드포인트가 없다(OpenRouter 에 zdr 를 걸면 404). 채팅 게이트웨이는 LLM_PROVIDER=openrouter 면 ZDR
엔드포인트로만 보내므로, 그 설정에서는 켜도 보내지 않는다(app/gateway/decision.py 의 Jev 와 같은 규칙). 보내는 것은
조건 뽑기가 만든 분위기 문장(spec.semantic)과 후보 책 글이다.
"""

import asyncio
import logging
import math
import time

import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def skip_reason(settings: Settings) -> str | None:
    """CHAT_RERANKER 를 켰는데 쓰지 않는 까닭. 쓰면(또는 켜지 않았으면) None."""
    if not settings.chat_reranker or settings.llm_mock:
        return None
    if settings.llm_provider == "openrouter":
        # app/gateway/llm.py get_chat_model 이 ZDR 을 켜는 조건과 같다
        return "LLM_PROVIDER=openrouter(ZDR 켬)라 ZDR 엔드포인트가 없는 리랭커 API 로 보내지 않는다"
    if not settings.external_ai_api_key:
        return "EXTERNAL_AI_API_KEY 가 비어 있다"
    return None


def enabled() -> bool:
    """③ 분위기 요청의 후보를 리랭커로 다시 줄 세울지."""
    settings = get_settings()
    return (
        settings.chat_reranker
        and not settings.llm_mock
        and skip_reason(settings) is None
    )


_client: httpx.AsyncClient | None = None
_client_loop: asyncio.AbstractEventLoop | None = None


def _http() -> httpx.AsyncClient:
    """연결을 다시 쓰려고 클라이언트 하나를 둔다. 연결은 만든 이벤트 루프에서만 쓸 수 있어 루프가 바뀌면 새로 만든다."""
    global _client, _client_loop
    loop = asyncio.get_running_loop()
    if _client is None or _client.is_closed or _client_loop is not loop:
        _client = httpx.AsyncClient()
        _client_loop = loop
    return _client


async def aclose() -> None:
    """서버가 내려갈 때 연결을 닫는다."""
    global _client
    if _client is not None and _client_loop is asyncio.get_running_loop():
        await _client.aclose()
    _client = None


def _book_text(book: dict) -> str:
    """리랭커가 질문과 같이 읽을 책 글. 비어 있는 칸은 뺀다. 소개는 자르지 않는다(잰 조건 그대로)."""
    parts = (book.get("title"), book.get("author"), book.get("description"))
    return "\n".join(p.strip() for p in parts if p and p.strip())


def _number(value: object) -> bool:
    # bool 은 int 의 하위형이라 따로 막는다
    return isinstance(value, int | float) and not isinstance(value, bool)


def _scores(payload: object, count: int) -> list[float] | None:
    """응답의 results 를 받은 목록 자리별 점수로 바꾼다. 모양이 틀리면 None.

    응답에 없는 자리는 -inf 로 두어 맨 뒤에 검색 순서대로 남긴다.
    """
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results:
        return None
    scores = [-math.inf] * count
    seen: set[int] = set()
    for item in results:
        if not isinstance(item, dict):
            return None
        index, score = item.get("index"), item.get("relevance_score")
        if not isinstance(index, int) or isinstance(index, bool):
            return None
        if not 0 <= index < count or index in seen:
            return None
        if not _number(score) or not math.isfinite(score):
            return None
        seen.add(index)
        scores[index] = float(score)
    return scores


async def rerank(query: str, candidates: list[dict]) -> list[dict]:
    """질문에 잘 맞는 순서로 다시 줄 세운 새 목록을 돌려준다. 못 하면 받은 목록을 그대로 돌려준다.

    점수가 같으면 검색 순서를 지킨다(sorted 는 안정 정렬).
    """
    if not query or len(candidates) < 2 or not enabled():
        return candidates
    settings = get_settings()
    body = {
        "model": settings.reranker_model_id,
        "query": query,
        "documents": [_book_text(c) for c in candidates],
    }
    headers = {"Authorization": f"Bearer {settings.external_ai_api_key}"}
    timeout = settings.reranker_timeout_seconds
    started = time.perf_counter()
    try:
        # httpx 의 제한은 연결·읽기 단계마다 걸려 전체를 못 막아서 호출 전체에 건다
        async with asyncio.timeout(timeout):
            response = await _http().post(
                settings.reranker_url, json=body, headers=headers
            )
    except TimeoutError:
        logger.warning(
            "리랭커가 %.1f초 안에 답하지 않아 검색 순서를 씁니다 (%d권)",
            timeout,
            len(candidates),
        )
        return candidates
    except httpx.HTTPError as e:
        # 예외 문구에 주소가 실릴 수 있어 종류만 남긴다
        logger.warning("리랭커 호출 실패(%s). 검색 순서를 씁니다", type(e).__name__)
        return candidates
    if response.status_code != 200:
        # 응답 본문은 보낸 질문을 되풀이할 수 있어 남기지 않는다(#202)
        logger.warning(
            "리랭커가 HTTP %d 로 답해 검색 순서를 씁니다", response.status_code
        )
        return candidates
    try:
        payload = response.json()
    except ValueError:
        payload = None
    scores = _scores(payload, len(candidates))
    if scores is None:
        logger.warning("리랭커 응답 모양이 틀려 검색 순서를 씁니다")
        return candidates
    order = sorted(range(len(candidates)), key=lambda i: -scores[i])
    logger.info("리랭커 %d권 %.2f초", len(candidates), time.perf_counter() - started)
    return [candidates[i] for i in order]
