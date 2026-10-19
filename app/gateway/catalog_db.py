"""⑦ 비교 기능(book.compare)이 읽는 책 소개글을 AI DB 의 책 표(v_books)에서 읽는다(#415).

소개글은 BE 가 아니라 AI 서버가 가진 카탈로그가 주인이다(catalog.py). 같은 ISBN 의 판본이 여러 줄이면 소개글이
있는 줄을 먼저 고른다.
"""

from collections.abc import Sequence

from app.core import db
from app.gateway.catalog import BookText

_QUERY = """
SELECT DISTINCT ON (isbn13) isbn13, title, description
FROM v_books
WHERE isbn13 = ANY($1::text[])
ORDER BY isbn13, (description IS NULL OR description = ''), book_id
"""


class DbCatalog:
    async def get_texts(self, isbns: Sequence[str]) -> list[BookText]:
        wanted = list(dict.fromkeys(isbns))
        if not wanted:
            return []
        async with db.get_pool().acquire() as conn:
            rows = await conn.fetch(_QUERY, wanted)
        by_isbn = {
            row["isbn13"]: BookText(row["isbn13"], row["title"], row["description"])
            for row in rows
        }
        # 받은 순서를 지키고 모르는 책은 뺀다
        return [by_isbn[isbn] for isbn in wanted if isbn in by_isbn]
