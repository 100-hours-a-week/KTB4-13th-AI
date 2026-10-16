"""④ 관심사별 목록 — 관심사마다 책을 뽑되, 목록 하나에 시간 예산 3초를 둔다(#451, 설계 #395).

운영에서 같은 조회가 52ms부터 35초까지 갈렸다(#399). 그래서 몫이 큰 관심사부터 조회하고, 조회마다 남은
예산만큼만 기다린다. 예산을 넘긴 관심사는 그 분류의 인기 있는 책으로 대신하고 따로 알려 준다. 목록에 그렇게
표시해 두면 밤에 그 목록을 다시 뽑고, 대신한 관심사가 줄었을 때만 바꾼다(⑨, #456, #482).
"""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import asyncpg

from app.feed import interest_picks
from app.feed.interest_picks import Pick
from app.profile import interests

# 목록 하나를 뽑는 데 쓰는 시간. ⑥ 은 BE 의 제한 시간(5초) 안에 답해야 하고, 프로필 계산과 저장도 그 안에 든다
BUDGET_SECONDS = 3.0
# 대신할 책을 읽는 조회 하나에 기다리는 시간. 예산을 다 쓴 뒤에 돌아 예산 밖이다. 색인 앞부분만 읽어 몇 ms 다
FALLBACK_SECONDS = 0.5


def _budget(seconds: float) -> Callable[[], float]:
    """남은 시간을 돌려주는 함수. 다 썼으면 TimeoutError 로, 조회가 시간을 넘긴 것과 같게 끝낸다.

    지금 홈 추천의 것(personalized.py)과 같다. 챗봇이 그 파일을 쓰므로 고치지 않고 여기 둔다(#420).
    """
    deadline = time.monotonic() + seconds

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("관심사를 뽑는 시간이 다 됐다")
        return remaining

    return left


@dataclass(frozen=True)
class Job:
    """관심사 하나를 뽑는 일. limit 은 뽑을 권수이고 vector 는 그 관심사의 검색어다.

    검색어는 그 관심사의 책 벡터 평균, 책이 없으면 이름 벡터(by_name)다. 키워드 관심사는 핵심어의 이름
    벡터이고, 작가 관심사는 검색어를 쓰지 않는다.
    """

    name: str
    limit: int
    vector: Sequence[float] | None = None
    by_name: bool = False


@dataclass(frozen=True)
class Fetched:
    """관심사마다 뽑은 책과, 예산을 넘겨 대신한 관심사. 대신할 책이 없는 관심사는 빈 목록이다."""

    picks: dict[str, list[Pick]]
    substituted: tuple[str, ...]


async def fetch(
    conn: asyncpg.Connection,
    jobs: Sequence[Job],
    *,
    seconds: float = BUDGET_SECONDS,
) -> Fetched:
    """jobs 를 넘긴 순서대로 하나의 예산 안에서 뽑는다. 몫이 큰 관심사부터 넘긴다.

    예산을 다 쓰면 남은 관심사는 조회하지 않고 바로 대신한다. 검색어가 없는 관심사(이름 벡터를 못 읽음)도
    대신한다. 관심사마다 트랜잭션(바깥이 있으면 저장점)으로 감싸, 시간을 넘긴 조회가 바깥 트랜잭션을 깨지
    않게 한다.
    """
    left = _budget(seconds)
    picks: dict[str, list[Pick]] = {}
    substituted: list[str] = []
    for job in jobs:
        try:
            async with conn.transaction():
                got = await _pick(conn, job, left)
        except TimeoutError:
            got = None
        if got is None:
            substituted.append(job.name)
            got = await _substitute(conn, job)
        picks[job.name] = got
    return Fetched(picks, tuple(substituted))


async def _pick(
    conn: asyncpg.Connection, job: Job, left: Callable[[], float]
) -> list[Pick] | None:
    """관심사 종류에 맞게 뽑는다. 검색어가 없어 뽑을 수 없으면 None 이다."""
    if job.name.startswith(interests.AUTHOR):
        return await interest_picks.pick_author(
            conn, job.name, limit=job.limit, left=left
        )
    if job.vector is None:
        return None
    if job.name.startswith(interests.KEYWORD):
        return await interest_picks.pick_keyword(
            conn, job.name, job.vector, limit=job.limit, left=left
        )
    return await interest_picks.pick(
        conn, job.name, job.vector, by_name=job.by_name, limit=job.limit, left=left
    )


async def _substitute(conn: asyncpg.Connection, job: Job) -> list[Pick]:
    """그 관심사의 분류에서 인기 있는 책으로 대신한다. 이것마저 시간을 넘기면 비우고 다른 관심사가 채운다."""
    try:
        async with conn.transaction():
            return await interest_picks.pick_popular(
                conn,
                job.name,
                job.vector,
                by_name=job.by_name,
                limit=job.limit,
                timeout=FALLBACK_SECONDS,
            )
    except TimeoutError:
        return []
