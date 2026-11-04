"""프로필 계산 뒤 홈 목록을 뒤에서 만드는 장치(#580, 방향 #541).

프로필 계산이 BE 요청 안에서 목록까지 만들면, 목록을 뽑는 시간이 BE 가 기다리는 시간에 묶인다. 그래서
프로필만 저장해 답하고 목록은 응답 뒤에 만든다. 이때 지켜야 할 것이 둘이다.

- 같은 사람의 목록 만들기가 겹쳐도 최신 요청으로 만든 목록이 남아야 한다. 프로필 계산이 요청마다 목록 칸에
  새 요청 표를 적고(mark), 뒤의 작업은 표가 그대로일 때만 저장한다(save). 그새 새 요청이 오면 늦게 끝난 옛
  목록은 버린다. 판 번호(profile_version)는 취향 벡터·태그만 보고 올라서, 산 책이나 "싫다"는 기억만 바뀐
  요청을 가르지 못한다.
- 재료는 그 요청으로 계산한 값을 그대로 받는다(Job). 밤 다시 뽑기가 쓰는 저장값은 읽지 않는다. 밤 다시
  뽑기가 남아 있는 동안 읽도록 저장만 지금처럼 목록과 함께 한다.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

import asyncpg

from app.core import db
from app.feed import interest_feed
from app.profile import interests
from app.profile import service as profile_service

logger = logging.getLogger(__name__)

_PENDING = interest_feed.PENDING

# 저장 때 사용자 잠금을 기다리는 한도. 앱 연결의 문장 제한(5초)을 따르면, 밤 다시 뽑기가 그 사용자를 6초
# 예산으로 뽑는 동안 저장이 끊겨 최신 요청의 목록이 사라진다. 밤 다시 뽑기 한 번(20초)보다 넉넉히 준다
LOCK_WAIT_SECONDS = 30.0

# 요청 표만 바꾼다. 판 번호와 앞 판은 목록을 저장할 때 한 번만 바꾼다. 여기서도 바꾸면 프로필 계산 한 번에
# 판이 두 번 올라, 더 보기를 넘기던 사람이 앞 판을 잃는다(커서는 지금 판 - 1 만 앞 판으로 읽는다)
_MARK_SQL = f"""
UPDATE taste_profile
SET feed = jsonb_set(coalesce(feed, '{{}}'::jsonb), '{{{_PENDING}}}', to_jsonb($2::text))
WHERE user_id = $1
"""

# 표가 그대로일 때만 저장한다. 앞 판에는 책이 있던 목록만 남긴다(처음 쓰는 사용자는 표만 있었다)
_SAVE_SQL = f"""
UPDATE taste_profile
SET interest_scores = $2::jsonb, interest_vectors = $3::jsonb,
    conditions = $4::jsonb, book_first_seen = $5::jsonb,
    feed_prev = CASE WHEN feed ? 'books' THEN feed - '{_PENDING}' ELSE feed_prev END,
    feed = $6::jsonb, feed_version = feed_version + 1, feed_partial = $7
WHERE user_id = $1 AND feed->>'{_PENDING}' = $8
"""


@dataclass(frozen=True)
class Job:
    """뒤에서 만들 목록 하나. 프로필 계산이 그 요청으로 계산한 재료를 담는다.

    table 은 관심사마다 출처별 점수, book_vectors 는 관심사마다 책 벡터 평균, keyword_vectors 는 키워드
    관심사마다 핵심어 벡터다. owned 는 목록에서 뺄 가진 책이고 disliked 는 그중 2점 이하 리뷰를 단 책이다.
    first_seen 은 이력의 책마다 처음 본 날, book_anchors 는 관심사마다 매칭 점수에 쓰는 내 책이다. seconds 는
    목록을 뽑는 시간 예산이다.
    """

    user_id: int
    token: str
    table: Mapping[str, Mapping[str, float]]
    book_vectors: Mapping[str, Sequence[float]]
    keyword_vectors: Mapping[str, Sequence[float]]
    conditions: Sequence[str]
    owned: frozenset[int]
    disliked: frozenset[int]
    first_seen: Mapping[str, str]
    book_anchors: Mapping[str, Sequence[int]]
    seconds: float


async def mark(conn: asyncpg.Connection, user_id: int) -> str:
    """목록 칸에 새 요청 표를 적고 그 표를 돌려준다. 프로필 행이 이미 있어야 한다."""
    token = uuid.uuid4().hex
    await conn.execute(_MARK_SQL, user_id, token)
    return token


async def save(conn: asyncpg.Connection, job: Job, feed: interest_feed.Feed) -> bool:
    """표가 그대로면 목록과 관심사 값을 저장하고 True, 그새 바뀌었으면 버리고 False 다.

    사용자 잠금을 잡고 저장한다. 밤 다시 뽑기는 이 잠금을 쥔 채 옛 관심사 값으로 목록을 뽑으므로, 기다리지
    않으면 먼저 저장한 새 목록을 밤 다시 뽑기가 옛 값으로 만든 목록으로 덮는다.
    """
    async with conn.transaction():
        await conn.execute(
            "SELECT pg_advisory_xact_lock($1, $2)",
            profile_service.LOCK_SPACE,
            job.user_id,
            timeout=LOCK_WAIT_SECONDS,
        )
        status = await conn.execute(
            _SAVE_SQL,
            job.user_id,
            json.dumps(job.table, ensure_ascii=False),
            json.dumps({**job.book_vectors, **job.keyword_vectors}, ensure_ascii=False),
            json.dumps(list(job.conditions), ensure_ascii=False),
            json.dumps(dict(job.first_seen)),
            json.dumps(feed.stored(job.owned, job.disliked), ensure_ascii=False),
            bool(feed.substituted),
            job.token,
        )
    return status == "UPDATE 1"


async def build_and_save(conn: asyncpg.Connection, job: Job) -> bool:
    """받은 재료로 목록을 뽑아 표가 그대로면 저장한다. 저장했으면 True 다.

    목록 뽑기는 사용자 잠금 없이 하고(서버 안에서 한 번에 하나만 뽑는 잠금은 interest_feed.build 가 건다),
    저장할 때만 사용자 잠금을 잡는다. 잠금을 쥔 채 다른 잠금을 기다리는 일이 없어 서로 멈추지 않는다.
    """
    started = time.monotonic()
    feed = await interest_feed.build(
        conn,
        interests.combined(job.table),
        job.book_vectors,
        job.keyword_vectors,
        job.conditions,
        job.owned,
        seconds=job.seconds,
        book_anchors=job.book_anchors,
    )
    saved = await save(conn, job, feed)
    logger.info(
        "홈 목록을 만들었다(사용자 %s): %s, %.2f초, 대신 채운 관심사 %d개",
        job.user_id,
        "저장" if saved else "새 요청이 있어 버림",
        time.monotonic() - started,
        len(feed.substituted),
    )
    return saved


class Queue:
    """서버 안의 대기줄과 일꾼 하나. 기다리는 작업은 DB 연결을 붙잡지 않는다.

    같은 사람의 작업이 줄에 있으면 새 작업으로 바꾸고 자리는 그대로 둔다. 옛 작업의 목록은 어차피 저장 때
    버려진다. 줄은 메모리에만 있어 서버가 꺼지면 사라진다. 그 사용자는 다음 프로필 계산 때 갱신된다.
    """

    def __init__(self, run: Callable[[Job], Awaitable[object]]) -> None:
        self._run = run
        self._jobs: dict[int, tuple[Job, float]] = {}
        # 이벤트는 처음 기다린 루프에 묶인다. 켤 때마다 그 루프에서 새로 만든다
        self._ready: asyncio.Event | None = None
        self._worker: asyncio.Task | None = None
        # 일꾼이 지금 만드는 작업의 사용자. 내려갈 때 버리는 수에 넣는다
        self._building: int | None = None

    @property
    def waiting(self) -> int:
        return len(self._jobs)

    def submit(self, job: Job) -> None:
        self._jobs[job.user_id] = (job, time.monotonic())
        if self._ready is not None:
            self._ready.set()

    def start(self) -> None:
        if self._worker is None or self._worker.done():
            self._ready = asyncio.Event()
            if self._jobs:
                self._ready.set()
            self._worker = asyncio.create_task(self._work(self._ready))

    async def stop(self) -> None:
        """일꾼을 멈추고 줄을 비운다. 만들던 목록은 저장하지 않는다.

        만들던 것과 남은 줄을 더해 버린 수를 경고로 남긴다. 새 서버를 먼저 띄우고 옛 서버를 내리는 배포에서는
        줄이 대개 비어 있어, 끊기는 것은 주로 만들던 하나다. 이 경고가 배포 때 자주 보이면 내려가기 전에
        만들던 것을 마치게 한다(지금은 하지 않는다).
        """
        dropped = len(self._jobs) + (self._building is not None)
        self._jobs.clear()
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        self._ready = None
        if dropped:
            logger.warning("서버가 내려가 홈 목록 %d개를 만들지 못했다", dropped)

    async def _work(self, ready: asyncio.Event) -> None:
        while True:
            await ready.wait()
            while self._jobs:
                user_id = next(iter(self._jobs))
                job, submitted = self._jobs.pop(user_id)
                logger.info(
                    "홈 목록 만들기 시작(사용자 %s): 줄에서 %.2f초 기다림, 남은 줄 %d",
                    user_id,
                    time.monotonic() - submitted,
                    len(self._jobs),
                )
                self._building = user_id
                try:
                    await self._run(job)
                except Exception:
                    logger.exception(
                        "홈 목록을 뒤에서 만들지 못했다(사용자 %s)", user_id
                    )
                finally:
                    self._building = None
            ready.clear()


async def _run_with_pool(job: Job) -> None:
    async with db.get_pool().acquire() as conn:
        await build_and_save(conn, job)


_QUEUE = Queue(_run_with_pool)


def submit(job: Job) -> None:
    """목록 만들기를 줄에 넣는다. 프로필을 저장한 트랜잭션이 끝난 뒤에 부른다."""
    _QUEUE.submit(job)


def start() -> None:
    _QUEUE.start()


async def stop() -> None:
    await _QUEUE.stop()
