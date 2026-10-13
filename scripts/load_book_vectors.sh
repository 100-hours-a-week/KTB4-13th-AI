#!/usr/bin/env bash
# 미리 만들어 둔 책 벡터 파일을 로컬 DB 의 book_embeddings 에 넣는다(#392).
#
# 무엇에 쓰나: 평가용 로컬 DB(evals/feed/README.md)를 만들 때 쓴다. 책 수십만 권을 서버의 벡터 채우기
# (python -m app.jobs.embed_books)로 만들면 CPU 로 두 시간쯤 걸린다. 다른 곳에서 GPU 로 만들어 둔 벡터가
# 있으면 이 스크립트로 넣는 편이 빠르다(59만 권에 4분).
#
# 서버에서는 쓰지 않는다. 운영과 개발 서버는 벡터 채우기 작업으로 벡터를 만든다.
#
# 사용법: scripts/load_book_vectors.sh <벡터 파일> [DB이름]
#   먼저 scripts/ingest_catalog.sh 로 책을 넣어 둔다. 책을 ISBN 으로 잇기 때문이다.
#
# 벡터 파일: 한 줄에 책 하나, 탭으로 나눈 두 칸.
#   <13자리 ISBN><탭>[0.0123,-0.0456,…]      (숫자 384개. pgvector 가 읽는 글자 모양)
#   서버의 책 벡터와 같은 벡터여야 한다: 같은 모델(intfloat/multilingual-e5-small), 같은 글
#   (app/jobs/embed_books.py 의 build_document), 접두어 "passage: ", 길이 1로 정규화.
#   다르게 만든 벡터를 넣으면 에러 없이 검색과 추천의 결과만 달라진다.
#
# 벡터를 붙이는 책(2026-10-13 에 정한 조건). 파일에 벡터가 있어도 아래에 들지 않으면 붙이지 않는다.
#   - 소개글이 정보나루에서 왔고 표지가 있는 책
#   - 소개글이 국립중앙도서관에서 왔고 납본된 책
#
# 주의: book_embeddings 를 비우고 다시 채운다. 벡터 색인도 지웠다가 다시 만든다. 한 건씩 색인에 끼워
# 넣으면 수십만 건에서 훨씬 느리다.

set -euo pipefail

VECTORS_PATH="${1:?사용법: scripts/load_book_vectors.sh <벡터 파일> [DB이름]}"
DB_NAME="${2:-bookjeok_ai_eval}"

if [ ! -f "$VECTORS_PATH" ]; then
    echo "파일을 찾을 수 없습니다: $VECTORS_PATH" >&2
    exit 1
fi

psql -v ON_ERROR_STOP=1 -d "$DB_NAME" <<SQL
DROP TABLE IF EXISTS stg_book_vectors;
CREATE UNLOGGED TABLE stg_book_vectors (isbn13 text PRIMARY KEY, embedding vector(384) NOT NULL);
\copy stg_book_vectors FROM '$VECTORS_PATH'

DROP INDEX IF EXISTS book_embeddings_hnsw_idx;
TRUNCATE book_embeddings;

INSERT INTO book_embeddings (book_id, embedding, dim, model)
SELECT b.book_id, v.embedding, 384, 'intfloat/multilingual-e5-small'
FROM stg_book_vectors v
JOIN v_books b ON b.isbn13 = v.isbn13
JOIN stg_cat_books s ON s.data->>'isbn13' = v.isbn13
WHERE (s.data->>'description_source' = 'd4l' AND coalesce(s.data->>'cover_url', '') <> '')
   OR (s.data->>'description_source' = 'nl' AND s.data->>'deposit_yn' = 'Y');

-- 색인을 만드는 동안만 메모리와 작업자를 넉넉히 쓴다. 이 세션에만 적용된다.
SET maintenance_work_mem = '4GB';
SET max_parallel_maintenance_workers = 6;
-- 정의는 db/migrations/001_init.sql 과 같다.
CREATE INDEX book_embeddings_hnsw_idx ON book_embeddings USING hnsw (embedding vector_cosine_ops);

ANALYZE book_embeddings;
ANALYZE v_books;
SQL

echo "== 벡터 적재 결과 =="
psql -d "$DB_NAME" -c "
SELECT
    (SELECT count(*) FROM stg_book_vectors) AS in_file,
    (SELECT count(*) FROM book_embeddings) AS loaded,
    (SELECT count(*) FROM v_books) AS books;
"
