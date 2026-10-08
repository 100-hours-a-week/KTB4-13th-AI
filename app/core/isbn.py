"""BE 와 책을 주고받는 키, ISBN(#308).

BE 와는 ISBN 으로 주고받고 AI 안에서는 book_id 로 잇는다. book_id 는 BE 가 책을 넣는 순서로
매겨서 다시 넣으면 바뀌는데, 어긋나도 에러 없이 엉뚱한 책이 이어진다(#276).

요청으로 받은 ISBN 을 들어오자마자 book_id 로 바꾸는 곳이 여기다. 응답 쪽은 책을 읽는 쿼리가
isbn13 칸을 같이 읽으면 되어서 반대 방향은 두지 않는다.
"""

import logging
from collections.abc import Sequence
from typing import Annotated

import asyncpg
from pydantic import Field

logger = logging.getLogger(__name__)

# 요청의 ISBN 칸에 쓰는 타입. 13자리 숫자가 아니면 400 이다.
# 형식이 틀린 값을 받아 주면 책을 못 찾아 "모르는 ISBN"으로 건너뛰게 된다. 그러면 BE 가 하이픈을
# 넣어 보내는 식으로 형식을 틀려도 에러 없이 책이 전부 빠진다.
# v_books.isbn13 의 제약과 같은 식이다(db/migrations/20261008_v_books_isbn13.sql).
ISBN13_PATTERN = r"^[0-9]{13}$"
Isbn13 = Annotated[str, Field(pattern=ISBN13_PATTERN)]

_SQL = "SELECT isbn13, book_id FROM v_books WHERE isbn13 = ANY($1::text[])"


async def to_book_ids(conn: asyncpg.Connection, isbns: Sequence[str]) -> list[int]:
    """ISBN 들을 book_id 로 바꾼다. 받은 순서를 지키고, 책 표에 없는 ISBN 은 건너뛴다.

    없는 ISBN 을 에러로 막지 않는다. BE 에는 있는데 AI 에 아직 안 들어온 책일 수 있고, 한 권
    때문에 요청 전체를 거절하면 프로필이나 추천이 통째로 실패한다. 대신 몇 권인지 남긴다.
    어느 책인지는 남기지 않는다. 산 책·담은 책 목록이 요청 번호와 함께 로그에 쌓인다.
    """
    if not isbns:
        return []
    rows = await conn.fetch(_SQL, list(isbns))
    found = {row["isbn13"]: row["book_id"] for row in rows}
    unknown = {isbn for isbn in isbns if isbn not in found}
    if unknown:
        logger.warning(
            "책 표에 없는 ISBN %d개를 건너뛴다(받은 것 %d개)",
            len(unknown),
            len(set(isbns)),
        )
    return [found[isbn] for isbn in isbns if isbn in found]
