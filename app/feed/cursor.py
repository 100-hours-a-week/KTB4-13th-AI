"""④ 피드의 페이지 커서 — ① 검색과 같은 커서 모듈(`app/core/cursor.py`)을 쓴다.

커서에 담는 것은 넷이다.

- 이어 붙일 위치
- 요청 조건 지문: 정렬·필터가 바뀌면 같은 위치가 다른 책을 가리키므로 410 으로 끊는다
- 응답 모드: 개인화한 목록과 개인화를 끈 목록은 순서가 아예 다르다. 페이지 사이에 모드가
  바뀌면 역시 410 이다
- 첫 페이지를 받은 시각: 이 시각 뒤에 생긴 이력(구매·담기·리뷰)은 제외 대상에서 뺀다.
  카드를 보고 돌아와 산 책이 다음 페이지에서 사라지면 목록이 한 칸씩 밀린다(명세 ④)

프로필 판 번호도 함께 싣지만 기록용이다. 프로필이나 인기 집계가 바뀌어도 끊지 않고 지금 값으로
이어 붙인다(명세: 같은 순서의 재현을 보장하지 않는다).
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core import cursor
from app.feed.schemas import FeedRequest


class CursorExpired(Exception):
    """커서를 쓸 수 없다 — 만료·위조·조건 변경. 클라이언트가 할 일은 셋 다 같다."""


@dataclass
class Page:
    """커서에서 꺼낸 값. 커서가 없으면 첫 페이지다."""

    offset: int = 0
    mode: str | None = None
    # 첫 페이지를 받은 시각. 첫 페이지면 지금 시각을 쓴다.
    issued_at: datetime | None = None


def fingerprint(req: FeedRequest) -> str:
    """정렬·필터의 지문. size 는 넣지 않는다 — 페이지마다 개수를 바꿔도 순서는 같다."""
    conditions = {
        "s": req.surface,
        "o": req.sort,
        "c": req.category,
        "y": [req.pub_year_from, req.pub_year_to],
        "m": req.match_score_min,
    }
    raw = json.dumps(conditions, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def read(req: FeedRequest, now: datetime | None = None) -> Page:
    """요청의 커서를 읽는다. 커서가 없으면 첫 페이지, 못 쓰는 커서면 CursorExpired."""
    if req.cursor is None:
        return Page(issued_at=now or datetime.now(UTC))
    try:
        data = cursor.decode(req.cursor)
        offset, mode, issued_for = data["o"], data["m"], data["f"]
        issued_at = datetime.fromtimestamp(data["i"], UTC)
    except (cursor.CursorError, KeyError, TypeError, ValueError) as exc:
        raise CursorExpired from exc
    if issued_for != fingerprint(req) or not isinstance(offset, int) or offset < 0:
        raise CursorExpired
    return Page(offset=offset, mode=mode, issued_at=issued_at)


def check_mode(page: Page, mode: str) -> None:
    """앞 페이지와 응답 모드가 다르면 끊는다. 목록을 만드는 방법이 아예 달라 이어 붙일 수 없다."""
    if page.mode is not None and page.mode != mode:
        raise CursorExpired


def issue(
    page: Page, req: FeedRequest, mode: str, profile_version: int | None
) -> str | None:
    """다음 페이지 커서. 더 볼 것이 없으면 None(명세: 끝은 next_cursor 가 null 인 것으로만 판정)."""
    return cursor.encode(
        {
            "o": page.offset + req.size,
            "f": fingerprint(req),
            "m": mode,
            "i": int(page.issued_at.timestamp()),
            "v": profile_version,
        }
    )
