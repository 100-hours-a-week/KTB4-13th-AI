"""③ 후보 고르기의 실험 두 가지를 앱 코드를 고치지 않고 끼운다.

    base    지금 그대로. ① 검색 30권 → 사후 거르기 → 검색 순서로 10권 → 10권에만 match_score
    rerank  (a) 거른 뒤 남은 책 전부에 match_score 를 먼저 매기고, 그 점수 순서로 10권을 자른다
    vector  (b) intent 가 semantic 이면 ① 검색 대신 벡터 검색(vector.search_ids)으로 30권을 받는다

① 검색 코드(app/search)는 팀원 소유라 고치지 않는다. 실험은 ③ 라우터(app/routers/chat.py)의 이름 몇 개를
바꿔 끼운다. 운영에 넣을지는 결과를 보고 정하고, 넣게 되면 chat.py 를 직접 고친다(이 파일을 쓰지 않는다).

`install` 은 한 프로세스에 하나만 건다. 평가 서버(serve.py)는 띄울 때 한 번, 단계 측정기(run.py)는 실험마다
걸고 되돌린다. 요청이 동시에 들어와도 섞이지 않게, 요청의 intent 는 전역 변수가 아니라 contextvar 로 넘긴다.
"""

import contextvars
from collections.abc import Callable

from app.core import db
from app.gateway import embedding
from app.routers import chat
from app.search import books, service, vector

VARIANTS = ("base", "rerank", "vector")

# 지금 처리 중인 요청의 spec.intent. get_candidates 를 감싼 함수가 넣고, 검색을 바꿔 끼운 쪽이 읽는다
_intent: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "chat_stages_intent", default=None
)


class VectorFirst:
    """(b) chat.py 가 부르는 `service.search` 자리에 들어간다. semantic 요청만 벡터 검색으로 받는다.

    ①의 하이브리드 검색은 키워드 등수를 3배로 쳐서(app/search/service.py 의 KEYWORD_WEIGHT), 검색어의 흔한
    낱말이 제목에 든 책이 위를 채운다. 벡터 검색은 소개글이 있는 책만 찾는데, semantic 이면 ③이 어차피
    소개글 없는 책을 빼므로 잃는 책이 없다. 임베딩이나 벡터 검색이 실패하면 ①로 돌아간다.
    """

    async def search(self, req):
        if _intent.get() != "semantic":
            return await service.search(req)
        try:
            vectors, _, _ = await embedding.embed([req.query], "query")
            async with db.get_pool().acquire() as conn:
                ids = await vector.search_ids(
                    conn, vectors[0], req.filters, limit=req.size
                )
                results = await books.fetch(conn, ids)
        except Exception:  # noqa: BLE001 — ①(service._vector_ids)과 같이 어떤 이유로 실패해도 할 일이 같다
            chat.logger.exception("벡터 검색 실패. ① 검색으로 대신한다")
            return await service.search(req)
        return service.SearchOutcome(results=results, degraded=None)


def install(name: str) -> Callable[[], None]:
    """실험 하나를 chat 모듈에 건다. 되돌리는 함수를 돌려준다."""
    if name not in VARIANTS:
        raise ValueError(f"실험 이름은 {VARIANTS} 중 하나다: {name}")
    saved = {
        attr: getattr(chat, attr)
        for attr in (
            "CANDIDATE_LIMIT",
            "_attach_match_scores",
            "get_candidates",
            "service",
        )
    }

    def undo() -> None:
        for attr, value in saved.items():
            setattr(chat, attr, value)

    if name == "rerank":
        limit, attach = saved["CANDIDATE_LIMIT"], saved["_attach_match_scores"]

        async def attach_then_cut(conn, candidates, user_id, spec):
            # get_candidates 는 CANDIDATE_LIMIT 로 자른 목록을 여기 넘기고 그 목록을 그대로 돌려준다.
            # 자르기를 늦추려고 CANDIDATE_LIMIT 를 검색 권수로 올려 두고, 점수를 붙인 뒤 이 자리에서 자른다.
            await attach(conn, candidates, user_id, spec)
            if spec.intent == "semantic" and spec.semantic:
                # exact(제목·저자 찾기)는 제목이 같은 책을 맨 앞에 둔 ①의 순서를 그대로 둔다.
                # 점수가 같으면 검색 순서를 지킨다(sort 는 안정 정렬).
                candidates.sort(key=lambda c: -c["match_score"])
            del candidates[limit:]

        chat.CANDIDATE_LIMIT = chat.SEARCH_SIZE
        chat._attach_match_scores = attach_then_cut
    elif name == "vector":
        get_candidates = saved["get_candidates"]

        async def with_intent(spec, *args, **kwargs):
            token = _intent.set(spec.intent)
            try:
                return await get_candidates(spec, *args, **kwargs)
            finally:
                _intent.reset(token)

        chat.get_candidates = with_intent
        chat.service = VectorFirst()
    return undo
