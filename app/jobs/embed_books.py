"""책 벡터 채우기 — v_books 의 책을 임베딩해 book_embeddings 에 저장한다.

실행: uv run python -m app.jobs.embed_books

② /embeddings 는 글을 벡터로 바꿔 주기만 하고 저장하지 않는다.
① /search 의 벡터 검색은 book_embeddings 를 읽으므로, 이 작업이 먼저 돌아야 한다.
이미 저장된 책은 건너뛰므로 중간에 끊겨도 다시 실행하면 이어서 채운다.

기능 플래그 embed_doc_hash 를 켜면(#300) 벡터를 만든 글의 해시를 함께 적고, 글이 바뀐 책은 다시 만든다.
소개가 없어진 책의 벡터는 지우고, 끝에 소개가 있는데 벡터가 없는 책이 남았으면 오류로 끝난다.
끄면 지금처럼 벡터가 없는 책만 채운다.
"""

import asyncio
import hashlib
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

# ── 아래는 embed_doc_hash 가 켜졌을 때만 쓴다. 꺼져 있으면 book_embedding_docs 가 조회문에 나오지 않는다.
#
# 해시는 book_embeddings 의 칸이 아니라 따로 둔 표(book_embedding_docs)에 적는다. 벡터 표의 줄을 고치면
# 해시만 바꿔도 벡터 색인(HNSW)에 그 줄이 다시 들어가, 로컬 1만 줄에 10.4초(색인 없으면 0.2초)가 걸렸다.

# 이미 지금 모델로 만든 책과, 그 벡터를 만든 글의 해시(모르면 NULL).
_DONE_HASH = """
SELECT e.book_id, d.doc_sha1
FROM book_embeddings e
LEFT JOIN book_embedding_docs d ON d.book_id = e.book_id
WHERE e.book_id = ANY($1::int[]) AND e.model = $2
"""

_UPSERT_DOC = """
INSERT INTO book_embedding_docs (book_id, doc_sha1, embedded_at)
VALUES ($1, $2, now())
ON CONFLICT (book_id) DO UPDATE
SET doc_sha1 = EXCLUDED.doc_sha1, embedded_at = EXCLUDED.embedded_at
"""

# 해시 표가 생기기 전에 만든 벡터는 어떤 글로 만들었는지 모른다. 지금 글로 만들었다고 보고 해시만 적는다.
# 전부 다시 만들면 운영 서버에서 하루가 넘게 걸리고 그동안 검색이 느려진다(CLOUD #59 에서 정함).
# 이미 어긋난 걸로 아는 책은 이 작업을 켜기 전에 벡터를 지워 두면 새로 만들어진다.
_FILL_HASH = """
INSERT INTO book_embedding_docs (book_id, doc_sha1)
SELECT * FROM unnest($1::int[], $2::text[])
ON CONFLICT (book_id) DO NOTHING
"""

# 소개가 없어진 책의 벡터. 지금 없는 글로 검색에 걸리면 안 된다. 해시는 외래 키로 같이 지워진다.
# 소개가 다시 생기면 다음 실행 때 새로 만든다.
_DROP_NO_DESCRIPTION = """
DELETE FROM book_embeddings e
USING v_books b
WHERE e.book_id = b.book_id AND b.description IS NULL
"""

# 끝 확인: 소개는 있는데 지금 모델의 벡터(해시까지 적힌)가 없는 책. 도서 적재 순서(CLOUD #59)상 이 작업은
# 소개 갱신과 복제가 끝난 뒤에 돌아서, 도는 동안 소개가 새로 생기는 일은 거의 없다. 생기면 다시 돌리면 된다.
# v_books.updated_at 으로 그런 책을 빼지 않는 건, 그 칸이 서버 표에만 있고 마이그레이션에 없어서다(#295).
_MISSING = """
SELECT count(*) FROM v_books b
WHERE b.description IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM book_embeddings e
      JOIN book_embedding_docs d ON d.book_id = e.book_id
      WHERE e.book_id = b.book_id AND e.model = $1
  )
"""


def build_document(title: str, author: str | None, description: str) -> str:
    """임베딩할 글을 만든다. 제목·저자를 소개글 앞에 둔다.

    모델은 512토큰까지만 읽고 뒤는 조용히 버린다. 긴 소개글에서 잘려도 되는 건
    뒷부분이지 제목이 아니므로 제목을 맨 앞에 둔다.
    이 모양을 바꾸면 저장된 벡터를 전부 다시 만들어야 한다.
    """
    parts = [title, author, description]
    return "\n".join(p.strip() for p in parts if p and p.strip())


def doc_sha1(document: str) -> str:
    """벡터를 만든 글의 해시. build_document 결과를 UTF-8 로 해시한다(모델 접두어는 넣지 않는다).

    도서 파이프라인의 로컬 벡터(cat_book_embeddings.doc_sha1)와 같은 정의다.
    build_document 의 모양을 바꾸면 모든 해시가 달라져, 다음 실행이 전부 다시 만든다.
    """
    return hashlib.sha1(document.encode("utf-8")).hexdigest()


async def embed_batch(
    conn: asyncpg.Connection, rows: list[asyncpg.Record], with_hash: bool = False
) -> int:
    """한 묶음을 임베딩해 저장하고, 저장한 권수를 돌려준다. with_hash 면 글의 해시도 적는다."""
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

    vector_rows = [
        (r["book_id"], to_vector_literal(v), dim, model)
        for r, v in zip(rows, vectors, strict=True)
    ]
    if not with_hash:
        await conn.executemany(_UPSERT, vector_rows)
        return len(rows)
    # 벡터와 해시를 같이 저장한다. 한쪽만 남으면 다음 실행이 그 책을 잘못 판단한다.
    async with conn.transaction():
        await conn.executemany(_UPSERT, vector_rows)
        await conn.executemany(
            _UPSERT_DOC,
            [(r["book_id"], doc_sha1(t)) for r, t in zip(rows, texts, strict=True)],
        )
    return len(rows)


def _split_by_hash(
    books: list[asyncpg.Record], stored: dict[int, str | None]
) -> tuple[list[asyncpg.Record], list[tuple[int, str]], int]:
    """(다시 만들 책, 해시만 채울 (book_id, 해시), 그중 글이 바뀌어 다시 만드는 권수)."""
    todo: list[asyncpg.Record] = []
    fill: list[tuple[int, str]] = []
    changed = 0
    for b in books:
        if b["book_id"] not in stored:
            todo.append(b)
            continue
        current = doc_sha1(build_document(b["title"], b["author"], b["description"]))
        old = stored[b["book_id"]]
        if old is None:
            fill.append((b["book_id"], current))
        elif old != current:
            todo.append(b)
            changed += 1
    return todo, fill, changed


async def run(
    conn: asyncpg.Connection,
    batch_size: int = BATCH_SIZE,
    scan_size: int = SCAN_SIZE,
) -> int:
    """카탈로그 끝까지 훑으며 채울 책을 묶음 단위로 임베딩한다. 저장한 총 권수를 돌려준다."""
    settings = get_settings()
    model = settings.embedding_model
    with_hash = settings.embed_doc_hash
    last_id = 0
    total = changed = filled = 0
    while True:
        books = await conn.fetch(_SCAN, last_id, scan_size)
        if not books:
            break
        ids = [b["book_id"] for b in books]
        if with_hash:
            rows = await conn.fetch(_DONE_HASH, ids, model)
            todo, fill, n_changed = _split_by_hash(
                books, {r["book_id"]: r["doc_sha1"] for r in rows}
            )
            if fill:
                await conn.execute(
                    _FILL_HASH, [i for i, _ in fill], [h for _, h in fill]
                )
            changed += n_changed
            filled += len(fill)
        else:
            done_ids = {r["book_id"] for r in await conn.fetch(_DONE, ids, model)}
            todo = [b for b in books if b["book_id"] not in done_ids]
        for start in range(0, len(todo), batch_size):
            batch = todo[start : start + batch_size]
            total += await embed_batch(conn, batch, with_hash)
            logger.info(
                "책 벡터 %d권 저장 (마지막 book_id=%d)", total, batch[-1]["book_id"]
            )
        # 훑은 책이 모두 채워져 있어도 다음 구간으로 넘어간다. 빈 결과를 끝으로 보면 안 된다.
        last_id = books[-1]["book_id"]

    if with_hash:
        status = await conn.execute(_DROP_NO_DESCRIPTION)
        dropped = int(status.split()[-1])
        missing = await conn.fetchval(_MISSING, model)
        logger.info(
            "저장 %d권(그중 글이 바뀐 책 %d권) · 해시만 채움 %d권 · 소개가 없어져 지운 벡터 %d개 · 빠진 책 %d권",
            total,
            changed,
            filled,
            dropped,
            missing,
        )
        if missing:
            raise RuntimeError(
                f"소개가 있는데 벡터가 없는 책이 {missing}권 남았습니다. 다시 실행하세요"
            )
    return total


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
