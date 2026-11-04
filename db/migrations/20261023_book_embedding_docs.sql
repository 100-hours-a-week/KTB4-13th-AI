-- 책 벡터가 어떤 글로 만들어졌는지 적어 두는 표 (#300)
--
-- 임베딩 작업은 벡터가 있는 책을 건너뛰어서, 소개가 고쳐져도 옛 벡터가 남았다.
-- 벡터를 만든 글(제목·저자·소개)의 해시를 적어 두고, 지금 글과 다르면 다시 만든다.
--
-- book_embeddings 에 칸을 더하지 않고 표를 따로 둔다. 벡터 표의 줄을 고치면 해시 칸만 바꿔도 벡터 색인(HNSW)에
-- 그 줄이 다시 들어간다. 로컬에서 1만 줄 해시를 채우는 데 색인이 있으면 10.4초, 없으면 0.2초였다.
-- 운영 82만 권이면 몇 시간이 걸리고 그동안 벡터 색인이 부풀어 검색이 느려진다.
--
-- 처음에는 비어 있다. 기능 플래그(embed_doc_hash)를 켠 첫 실행이 기존 벡터의 해시를 지금 글로 채운다
-- (벡터는 다시 만들지 않는다). 벡터가 지워지면 해시도 같이 지워진다.
--
-- 적용(AI 서버): docker exec -i bookjeok-postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < db/migrations/20261023_book_embedding_docs.sql
-- 확인: SELECT count(*) FROM book_embedding_docs;   → 0 (첫 실행 뒤에는 book_embeddings 와 같은 수)

-- 서비스 중에 돌려도 된다. 새 표라 기존 표를 다시 쓰지 않고, 외래 키를 걸 때 book_embeddings 에 쓰기만 잠깐
-- 막는다(읽기는 안 막아 검색·추천은 그대로). 그 순간 임베딩 작업이 쓰고 있으면 기다리는데, 5초가 넘으면 아무것도
-- 바꾸지 않고 포기한다. 그때는 다시 돌리면 된다.
SET lock_timeout = '5s';

CREATE TABLE IF NOT EXISTS book_embedding_docs (
    book_id     integer PRIMARY KEY REFERENCES book_embeddings (book_id) ON DELETE CASCADE,
    doc_sha1    char(40) NOT NULL,  -- 벡터를 만든 글의 sha1(16진수)
    embedded_at timestamptz         -- 벡터를 만든 시각. 비어 있으면 이 표가 생기기 전에 만든 벡터
);
