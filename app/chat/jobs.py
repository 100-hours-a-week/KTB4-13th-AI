"""③ 폴링 전송 — 요청을 작업으로 맡기고 결과는 따로 묻는다.

BE 는 POST /recommendations/chat/jobs 로 맡겨 job_id 를 받고, GET /recommendations/chat/jobs/{job_id} 로 정해 둔
간격마다 묻는다. 아직이면 202 와 먼저 보여 줄 말풍선(아직 못 썼으면 빈 글)을, 끝났으면 /chat 과 같은 응답(상태
코드·envelope)을 받는다.
BE 가 읽기 시간 제한이 짧은 동기 호출만 쓸 수 있거나 SSE 를 중계하기 어려울 때 쓰는 길이다.

- 작업은 이 프로세스 메모리에만 있다. 맡은 프로세스와 묻는 프로세스가 다르면(uvicorn 워커 여럿, 작업 여럿, 배포 중
  옛·새 작업이 함께 뜬 때) 404 다. 받는 쪽은 404 를 "다시 맡기거나 /chat 으로 부른다"로 다룬다. 여러 프로세스가
  함께 보려면 바깥 저장소(DB·Redis)가 있어야 한다.
- 결과는 맡긴 뒤 KEEP_SECONDS 동안 몇 번이든 같은 것을 돌려준다(GET 은 부수효과가 없어야 다시 물어도 안전하다).
  지난 작업은 다음 맡기기·묻기 때 지운다. 아직 안 끝났으면 취소한다.
- 작업 안의 로그도 맡긴 요청의 request_id 를 단다(작업이 맡길 때의 contextvars 를 복사해 돈다).
"""

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi.responses import JSONResponse

from app.chat.streaming import ReplySink

logger = logging.getLogger(__name__)

# 한 턴은 길어야 30초 남짓이다. BE 가 늦게 묻거나 응답을 놓쳐 다시 묻는 시간까지 넉넉히 둔다.
KEEP_SECONDS = 300


@dataclass
class Job:
    task: asyncio.Task[JSONResponse]
    created: float
    pieces: list[str]

    @property
    def reply(self) -> str:
        """카드보다 먼저 보여 줄 말풍선. 아직 못 썼으면 빈 글이다. 최종 reply 는 결과 본문의 것이다(app/chat/streaming.py)."""
        return "".join(self.pieces)


_jobs: dict[str, Job] = {}


def submit(answer: Callable[[ReplySink], Awaitable[JSONResponse]]) -> str:
    """answer(sink) 를 작업으로 돌리고 job_id 를 돌려준다. sink 로 온 말풍선 조각은 Job.pieces 에 쌓인다."""
    _sweep()
    pieces: list[str] = []

    async def append(piece: str) -> None:
        pieces.append(piece)

    task = asyncio.ensure_future(answer(append))
    task.add_done_callback(_log_failure)
    job_id = uuid.uuid4().hex
    _jobs[job_id] = Job(task, time.monotonic(), pieces)
    return job_id


def get(job_id: str) -> Job | None:
    """맡긴 작업이다. 모르거나 KEEP_SECONDS 가 지났으면 None 이다."""
    _sweep()
    return _jobs.get(job_id)


def _sweep() -> None:
    now = time.monotonic()
    for job_id, job in list(_jobs.items()):
        if now - job.created > KEEP_SECONDS:
            job.task.cancel()
            del _jobs[job_id]


def _log_failure(task: asyncio.Task[JSONResponse]) -> None:
    """아무도 결과를 묻지 않아도 실패는 남긴다. 묻는 쪽은 500 만 받는다."""
    if not task.cancelled() and task.exception() is not None:
        logger.error("폴링 작업 실패", exc_info=task.exception())
