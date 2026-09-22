"""① /search 의 검색 흐름: 키워드 검색 + 벡터 검색 → 이어붙이기 → 정렬."""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.core import cursor, db
from app.gateway import embedding
from app.search import books, keyword, sorting, vector
from app.search.schemas import SearchRequest

logger = logging.getLogger(__name__)

KEYWORD_ONLY = "keyword-only"
FULL = "full"

# 가격순·최신순·인기순은 등수를 무시하고 줄을 다시 세운다. 벡터 검색은 관련이 없어도 50권을
# 채워 주므로 다 넣으면 상관없는 책이 "제일 싸다"는 이유로 1등이 된다. 그래서 벡터 쪽은 앞의
# 20권만 정렬 대상에 넣는다. 고정 검색어 40개로 재니 벡터가 찾아낸 정답은 20등 안이 84%,
# 50등까지 넓히면 100% 였다. 남은 16%를 얻자고 관련이 약한 30권을 들이는 쪽이 손해라고 봤다.
SORT_VECTOR_LIMIT = 20


class CursorExpired(Exception):
    """커서를 쓸 수 없다. 클라이언트는 첫 페이지부터 다시 요청해야 한다(410)."""


@dataclass
class SearchOutcome:
    results: list[dict[str, Any]]
    # 기능을 줄여 응답했으면 그 이름(X-Degraded 헤더 값). 온전하면 None.
    degraded: str | None
    # 다음 페이지가 있으면 커서, 마지막 페이지면 None.
    # ③ chat 처럼 첫 페이지만 쓰는 호출부는 두 값을 몰라도 되게 기본값을 둔다.
    next_cursor: str | None = None
    # 첫 페이지였는가. 0건 안내(fallback)는 첫 페이지가 비었을 때만 붙인다.
    first_page: bool = True


def _fingerprint(req: SearchRequest) -> str:
    """검색 조건의 지문. 커서를 만들 때와 조건이 같은지 확인하는 데 쓴다.

    순위는 매번 다시 계산하므로, 조건이 다르면 같은 offset 이 전혀 다른 책을 가리킨다.
    """
    conditions = {
        "q": req.query.strip(),
        "f": req.filters.model_dump(),
        "s": req.sort,
    }
    raw = json.dumps(conditions, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _read_offset(req: SearchRequest, fingerprint: str) -> tuple[int, str | None]:
    """커서에서 (몇 번째부터, 커서를 만들 때의 응답 모드) 를 꺼낸다. 첫 페이지면 (0, None)."""
    if req.cursor is None:
        return 0, None
    try:
        data = cursor.decode(req.cursor)
        offset, mode, issued_for = data["o"], data["m"], data["f"]
    except (cursor.CursorError, KeyError, TypeError) as exc:
        raise CursorExpired from exc
    if issued_for != fingerprint or not isinstance(offset, int) or offset < 0:
        raise CursorExpired
    return offset, mode


def _keyword_first(keyword_ids: list[int], vector_ids: list[int]) -> list[int]:
    """키워드 결과를 순서 그대로 먼저 놓고, 키워드가 못 찾은 벡터 결과를 뒤에 붙인다.

    두 순위를 점수로 합치면(RRF) 양쪽에 다 나온 책이 점수를 더 받아, 키워드에서만 1등인
    정답을 넘어선다. 고정 검색어 40개 중 키워드 1등 15개가 합친 뒤 3개 밀렸다("명상 하는
    마음" 1등 → 8등). 이어붙이면 15개 모두 1등을 지킨다. 대신 저자 검색 일부가 1등에서
    2·3등이 되지만 첫 페이지 안에 남는다(측정 기록은 이슈 #62).
    """
    seen = set(keyword_ids)
    return keyword_ids + [book_id for book_id in vector_ids if book_id not in seen]


async def _embed_query(query: str) -> list[float] | None:
    """검색어를 벡터로 바꾼다. 실패하면 None — 검색은 키워드만으로 계속한다."""
    try:
        vectors, _, _ = await embedding.embed([query], "query")
    except Exception:
        logger.exception("검색어 임베딩 실패. 키워드 검색만으로 응답한다")
        return None
    return vectors[0]


async def _vector_ids(
    conn: asyncpg.Connection, query_vector: list[float] | None, req: SearchRequest
) -> list[int] | None:
    """벡터 검색 결과. 벡터 검색을 할 수 없는 상태면 None."""
    if query_vector is None:
        return None
    try:
        ids = await vector.search_ids(conn, query_vector, req.filters)
        # 0건이 필터 때문인지, 책 벡터가 아직 없어서인지 가른다(명세: 초기 적재 중이면 축소 응답).
        if not ids and not await vector.has_any(conn):
            return None
    except Exception:
        # 예외 종류를 좁히지 않는다. 벡터 검색이 어떤 이유로 못 되든 할 일은 같다 —
        # 키워드 결과만 돌려주고 헤더로 알린다(명세 ①). 좁게 잡으면 빠지는 게 생긴다:
        # asyncpg.InterfaceError(연결 끊김)와 TimeoutError(command_timeout 초과)는
        # PostgresError 하위가 아니라서, 키워드 결과가 멀쩡한데도 요청 전체가 500이 됐다.
        # 우리 쪽 버그로 여기 들어와도 로그에 스택이 남고 X-Degraded 비율로 드러난다.
        logger.exception("벡터 검색 실패. 키워드 검색만으로 응답한다")
        return None
    return ids


async def search(req: SearchRequest) -> SearchOutcome:
    fingerprint = _fingerprint(req)
    # 못 쓰는 커서는 검색을 돌리기 전에 거른다.
    offset, issued_mode = _read_offset(req, fingerprint)

    async with db.get_pool().acquire() as conn:
        # 임베딩은 CPU, 키워드 검색은 DB 를 기다리는 일이라 동시에 돌린다.
        # 키워드 검색이 실패하면 예외가 그대로 올라가 500 이 된다(DB 가 죽은 상태).
        query_vector, keyword_ids = await asyncio.gather(
            _embed_query(req.query),
            keyword.search_ids(conn, req.query, req.filters),
        )
        vector_ids = await _vector_ids(conn, query_vector, req)

        by_relevance = req.sort == "relevance"
        if vector_ids is None:
            ranked, degraded = keyword_ids, KEYWORD_ONLY
        else:
            pool = vector_ids if by_relevance else vector_ids[:SORT_VECTOR_LIMIT]
            ranked = _keyword_first(keyword_ids, pool)
            degraded = None

        # 앞 페이지는 벡터 결과까지 이은 순위였는데 지금은 키워드만이면(또는 그 반대) 순위가 달라
        # 같은 offset 이 다른 책을 가리킨다. 명세대로 410 으로 첫 페이지부터 다시 받게 한다.
        mode = degraded or FULL
        if issued_mode is not None and issued_mode != mode:
            raise CursorExpired

        if not by_relevance:
            ranked = await sorting.sort_ids(conn, ranked, req.sort)

        end = offset + req.size
        results = await books.fetch(conn, ranked[offset:end])

    next_cursor = None
    if end < len(ranked):
        next_cursor = cursor.encode({"o": end, "f": fingerprint, "m": mode})
    return SearchOutcome(
        results=results,
        next_cursor=next_cursor,
        first_page=offset == 0,
        degraded=degraded,
    )
