"""책 벡터 채우기 — v_books 의 책을 임베딩해 book_embeddings 에 저장한다.

실행: uv run python -m app.jobs.embed_books

② /embeddings 는 글을 벡터로 바꿔 주기만 하고 저장하지 않는다.
① /search 의 벡터 검색은 book_embeddings 를 읽으므로, 이 작업이 먼저 돌아야 한다.
이미 저장된 책은 건너뛰므로 중간에 끊겨도 다시 실행하면 이어서 채운다.
"""

import asyncio
import logging

import asyncpg

from app.core.config import get_settings
from app.core.pgvector import to_vector_literal
from app.gateway import embedding

logger = logging.getLogger(__name__)

# 명세 ② 의 한 번 호출 상한과 같다.
BATCH_SIZE = 256

# 한 번에 훑는 책 수. 번호 순으로 이만큼 가져와 그중 채울 책만 임베딩한다.
SCAN_SIZE = 2000

# 번호 순으로 다음 책을 가져온다. 소개글이 없는 책은 고르지 않는다(ERD: 벡터 검색 대상에서 빠지고
# 키워드로만 찾힌다). book_id > $1 로 앞에서부터 훑어, 실패한 묶음을 끝없이 다시 집는 일을 막는다.
#
# "벡터가 없는 책"을 한 쿼리로 고르지 않는다. book_embeddings 에 통계가 잡히면 PostgreSQL 이 조건에
# 맞는 책을 1권으로 짐작해, 묶음마다 두 표를 통째로 읽어 맞춰 본다(13만 권에서 묶음당 130–210MB,
# #194). 번호 색인으로 앞부분만 읽고, 이미 채운 책은 아래 쿼리로 따로 빼면 그럴 일이 없다.
_SCAN = """
SELECT book_id, title, author, description
FROM v_books
WHERE book_id > $1 AND description IS NOT NULL
ORDER BY book_id
LIMIT $2
"""

# 이미 지금 모델로 만든 책. 저장된 벡터가 다른 모델 것이면 다시 만든다 — 모델이 다르면 서로 비교할 수 없다.
_DONE = """
SELECT book_id FROM book_embeddings
WHERE book_id = ANY($1::int[]) AND model = $2
"""

_UPSERT = """
INSERT INTO book_embeddings (book_id, embedding, dim, model)
VALUES ($1, $2::vector, $3, $4)
ON CONFLICT (book_id) DO UPDATE
SET embedding = EXCLUDED.embedding, dim = EXCLUDED.dim, model = EXCLUDED.model
"""


def build_document(title: str, author: str | None, description: str) -> str:
    """임베딩할 글을 만든다. 제목·저자를 소개글 앞에 둔다.

    모델은 512토큰까지만 읽고 뒤는 조용히 버린다. 긴 소개글에서 잘려도 되는 건
    뒷부분이지 제목이 아니므로 제목을 맨 앞에 둔다.
    이 모양을 바꾸면 저장된 벡터를 전부 다시 만들어야 한다.
    """
    parts = [title, author, description]
    return "\n".join(p.strip() for p in parts if p and p.strip())


async def embed_batch(conn: asyncpg.Connection, rows: list[asyncpg.Record]) -> int:
    """한 묶음을 임베딩해 저장하고, 저장한 권수를 돌려준다."""
    settings = get_settings()
    texts = [build_document(r["title"], r["author"], r["description"]) for r in rows]
    vectors, dim, model = await embedding.embed(texts, "document")

    # 어긋난 채로 저장하면 책과 벡터가 엇갈려도 오류 없이 검색 결과만 이상해진다.
    if len(vectors) != len(rows):
        raise RuntimeError(
            f"벡터 개수({len(vectors)})가 책 권수({len(rows)})와 다릅니다"
        )
    if dim != settings.embedding_dim:
        raise RuntimeError(
            f"벡터 길이({dim})가 설정값({settings.embedding_dim})과 다릅니다"
        )

    await conn.executemany(
        _UPSERT,
        [
            (r["book_id"], to_vector_literal(v), dim, model)
            for r, v in zip(rows, vectors, strict=True)
        ],
    )
    return len(rows)


async def run(
    conn: asyncpg.Connection,
    batch_size: int = BATCH_SIZE,
    scan_size: int = SCAN_SIZE,
) -> int:
    """카탈로그 끝까지 훑으며 채울 책을 묶음 단위로 임베딩한다. 저장한 총 권수를 돌려준다."""
    model = get_settings().embedding_model
    last_id = 0
    total = 0
    while True:
        books = await conn.fetch(_SCAN, last_id, scan_size)
        if not books:
            return total
        done = await conn.fetch(_DONE, [b["book_id"] for b in books], model)
        done_ids = {r["book_id"] for r in done}
        todo = [b for b in books if b["book_id"] not in done_ids]
        for start in range(0, len(todo), batch_size):
            batch = todo[start : start + batch_size]
            total += await embed_batch(conn, batch)
            logger.info(
                "책 벡터 %d권 저장 (마지막 book_id=%d)", total, batch[-1]["book_id"]
            )
        # 훑은 책이 모두 채워져 있어도 다음 구간으로 넘어간다. 빈 결과를 끝으로 보면 안 된다.
        last_id = books[-1]["book_id"]


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # 서버의 연결 풀은 /health 용으로 5초 제한이 걸려 있어 쓰지 않는다.
    conn = await asyncpg.connect(get_settings().database_url)
    try:
        total = await run(conn)
    finally:
        await conn.close()
    logger.info("끝. 이번에 저장한 책: %d권", total)


if __name__ == "__main__":
    asyncio.run(main())
