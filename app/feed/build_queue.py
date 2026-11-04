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

import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import asyncpg

from app.feed import interest_feed
from app.profile import service as profile_service

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
