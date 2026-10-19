"""⑥ 한 번이 몇 초 걸리는지 저장 없이 잰다(#492). AI 컨테이너 안에서 서버와 따로 돈다.

속도는 운영(또는 운영과 같은 크기·사양)에서 잰다. 로컬은 10배 넘게 빨라 근거가 못 된다(#478). 그래서 운영
서버에서 돌려도 되게 만들었다.

- 평가 묶음(cases.json)의 가상 사용자로 ⑥ 요청을 만들어, 플래그를 켠 것처럼 관심사 점수와 추천 목록까지
  계산한다. 플래그는 이 프로세스 안에서만 켜고 서버의 설정은 건드리지 않는다
- 저장하는 문장(INSERT·UPDATE·DELETE)만 건너뛰고 읽기 전용 트랜잭션 안에서 돌린다. 빠뜨린 쓰기가 있으면 DB 에
  남지 않고 오류가 난다
- 임베딩 모델을 따로 읽지 않고 같은 컨테이너의 서버(② /embeddings)에 묻는다. 작은 서버라 모델을 두 번 올리면
  메모리가 모자란다. 벡터는 재기 전에 다 만들어 둔다(실제 ⑥ 은 BE 에게서 벡터를 받는다)
- 한 번에 한 명씩 부른다. BE 밤 배치가 ⑥ 을 그렇게 부른다

    curl -fsSL <이 파일 주소> | docker exec -i <AI 컨테이너> python - <cases.json 주소> [사용자 수] [건너뛸 수]
"""

import asyncio
import json
import statistics
import sys
import time
import urllib.request

from app.core import categories, db
from app.core.config import get_settings
from app.extraction.service import MEMORY_VECTOR_PURPOSE
from app.feed import interest_feed
from app.profile import labels, service
from app.profile.schemas import ProfileRequest

SERVER = "http://127.0.0.1:8000"
EMBED_BATCH = 256  # 명세 ② 의 한 번 호출 상한
_WRITES = {"INSERT", "UPDATE", "DELETE"}

_original_build = interest_feed.build
_last: dict = {}


def embed(texts: list[str], purpose: str) -> list[list[float]]:
    """같은 컨테이너의 서버에 벡터를 묻는다. 토큰은 서버와 같은 설정에서 읽고 찍지 않는다."""
    token = get_settings().ai_service_token
    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBED_BATCH):
        body = json.dumps(
            {"texts": texts[start : start + EMBED_BATCH], "purpose": purpose}
        )
        request = urllib.request.Request(
            SERVER + "/embeddings", data=body.encode(), method="POST"
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(request, timeout=120) as response:
            vectors += json.loads(response.read())["data"]["vectors"]
    return vectors


def prepare(
    url: str, limit: int | None, offset: int
) -> list[tuple[str, ProfileRequest]]:
    """가상 사용자의 ⑥ 요청을 만든다. 라벨·관심사 이름 벡터는 서버가 뜰 때와 같은 글과 용도로 채운다."""
    with urllib.request.urlopen(url, timeout=60) as response:
        cases = json.loads(response.read())["cases"][offset:]
    cases = cases[:limit] if limit else cases
    labels._vectors = dict(
        zip(labels.LABELS, embed(list(labels.LABELS), labels._PURPOSE), strict=True)
    )
    labels._name_vectors = dict(
        zip(
            categories.NAME_TEXTS,
            embed(list(categories.NAME_TEXTS.values()), labels._PURPOSE),
            strict=True,
        )
    )
    requests = []
    for case in cases:
        user = case["user"]
        memories = []
        if user["memories"]:
            # 취향 기억의 벡터는 ⑤ 가 붙이는 것과 같은 방법으로 만든다(핵심어)
            vectors = embed(
                [m["keyword"] for m in user["memories"]], MEMORY_VECTOR_PURPOSE
            )
            memories = [
                {**m, "vector": v, "dim": len(v)}
                for m, v in zip(user["memories"], vectors, strict=True)
            ]
        body = {
            "user_id": case["user_id"],
            "idempotency_key": "timing",
            "onboarding": user["onboarding"],
            "memories": memories,
            "history": user["history"],
        }
        requests.append((case["id"], ProfileRequest.model_validate(body)))
    return requests


class SkipWrites:
    """저장하는 문장만 건너뛰고 나머지는 그대로 넘긴다."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self.skipped = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    async def execute(self, sql: str, *args, **kwargs):
        if sql.lstrip().split(None, 1)[0].upper() in _WRITES:
            self.skipped += 1
            return "SKIPPED"
        return await self._conn.execute(sql, *args, **kwargs)


async def _timed_build(*args, **kwargs):
    started = time.perf_counter()
    feed = await _original_build(*args, **kwargs)
    _last.update(
        build=time.perf_counter() - started,
        interests=len(args[1]),
        books=len(feed.books),
        substituted=len(feed.substituted),
    )
    return feed


async def measure(requests: list[tuple[str, ProfileRequest]]) -> list[dict]:
    get_settings().interest_feed = True
    interest_feed.build = _timed_build
    await db.connect()
    rows = []
    try:
        async with db.get_pool().acquire() as raw:
            for case_id, req in requests:
                conn = SkipWrites(raw)
                _last.clear()
                started = time.perf_counter()
                async with raw.transaction(readonly=True):
                    # 실제 ⑥ 처럼 사용자별 잠금을 걸고 계산한다(service.rebuild_locked_on)
                    await raw.execute(
                        "SELECT pg_advisory_xact_lock($1, $2)",
                        service.LOCK_SPACE,
                        req.user_id,
                    )
                    await service.rebuild_on(conn, req)
                row = {
                    "id": case_id,
                    "total": time.perf_counter() - started,
                    "skipped": conn.skipped,
                    **_last,
                }
                rows.append(row)
                print(
                    f"{case_id} total={row['total']:.2f} build={row.get('build', 0):.2f} "
                    f"interests={row.get('interests')} books={row.get('books')} "
                    f"substituted={row.get('substituted')} skipped={conn.skipped}",
                    flush=True,
                )
    finally:
        await db.disconnect()
    return rows


def _at(values: list[float], share: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(share * (len(ordered) - 1)))]


def summary(rows: list[dict]) -> str:
    totals = [r["total"] for r in rows]
    return (
        f"SUMMARY users={len(rows)} median={statistics.median(totals):.2f} "
        f"p90={_at(totals, 0.9):.2f} max={max(totals):.2f} "
        f"over3={sum(t > 3 for t in totals)} over5={sum(t > 5 for t in totals)} "
        f"substituted_users={sum(1 for r in rows if r.get('substituted'))} "
        f"empty_lists={sum(1 for r in rows if r.get('books') == 0)}"
    )


def main(argv: list[str]) -> None:
    url = argv[0]
    limit = int(argv[1]) if len(argv) > 1 else None
    offset = int(argv[2]) if len(argv) > 2 else 0
    rows = asyncio.run(measure(prepare(url, limit, offset)))
    print(summary(rows), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
