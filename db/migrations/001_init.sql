-- AI 전용 PostgreSQL 초기 스키마
-- 출처: "AI 데이터 ERD 및 테이블 필드 명세서" §2·§3
--
-- 테이블은 두 갈래다.
--   v_* (5종) : BE MySQL에서 단방향 복제되는 사본. AI는 읽기만 한다.
--   그 외 (3종) : AI 소유. AI만 쓴다.
--
-- 복제 방식 전제 (§3):
--   1. v_books 복제는 행 단위 upsert/delete여야 한다. TRUNCATE 후 재적재나
--      DROP 후 재생성을 하면 book_embeddings가 ON DELETE CASCADE로 전량
--      날아가고 아래 키워드 인덱스도 함께 사라진다.
--   2. 아래 tsvector·pg_trgm 인덱스를 복제가 보존해야 한다.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;


-- ===========================================================================
-- §3. BE 복제 사본 5종 (읽기 전용)
-- ===========================================================================

-- §3-1. 도서 카탈로그
-- book_embeddings가 FK로 참조하므로 반드시 먼저 만든다.
CREATE TABLE v_books (
    book_id     integer PRIMARY KEY,
    title       text    NOT NULL,
    author      text    NOT NULL,
    publisher   text    NOT NULL,
    price       integer NOT NULL,   -- 할인 적용 후 판매가(원)
    in_stock    boolean NOT NULL,
    cover_url   text    NOT NULL,
    category    text,               -- 미분류 도서는 NULL
    pub_year    integer,            -- 출간연도 미상은 NULL
    description text                -- 없으면 벡터 검색 대상에서 빠진다
);

-- ① 검색 필터: category / price_min·price_max + in_stock_only / pub_year_from·to
CREATE INDEX v_books_category_idx ON v_books (category);
CREATE INDEX v_books_price_idx ON v_books (price) WHERE in_stock;
CREATE INDEX v_books_pub_year_idx ON v_books (pub_year DESC);

-- ① 키워드 검색. 한국어 형태소 분석기가 없으므로 'simple' + pg_trgm 조합으로 간다.
-- 조회 쿼리도 아래와 똑같은 식을 써야 인덱스를 탄다.
CREATE INDEX v_books_tsv_idx ON v_books USING gin (
    to_tsvector('simple',
        coalesce(title, '') || ' ' ||
        coalesce(author, '') || ' ' ||
        coalesce(description, ''))
);
CREATE INDEX v_books_title_trgm_idx ON v_books USING gin (title gin_trgm_ops);
CREATE INDEX v_books_author_trgm_idx ON v_books USING gin (author gin_trgm_ops);


-- §3-2. 인기 집계
-- 신호는 판매와 리뷰 둘뿐. 조회수·BE 랭킹은 계약에서 빠졌다.
-- 행이 없는 도서는 인기 항 0점으로 계산한다(오류 아님).
CREATE TABLE v_book_popularity (
    book_id      integer PRIMARY KEY,
    sales        integer NOT NULL,          -- 최근 판매 수. 없으면 0
    rating_avg   double precision,          -- 리뷰가 없으면 NULL
    rating_count integer NOT NULL,          -- 없으면 0
    as_of        timestamptz NOT NULL       -- BE가 채운 집계 기준 시각
);
CREATE INDEX v_book_popularity_sales_idx ON v_book_popularity (sales DESC);


-- §3-3. 이력 3종
-- ERD가 PK를 명시하지 않아 여기서도 걸지 않는다.
-- (user_id, book_id) 유니크를 임의로 걸면 재구매 행이 조용히 유실된다.
-- → BE 확인 항목.
CREATE TABLE v_user_purchases (
    user_id      integer NOT NULL,
    book_id      integer NOT NULL,
    purchased_at timestamptz NOT NULL
);
CREATE INDEX v_user_purchases_user_idx ON v_user_purchases (user_id, purchased_at DESC);

-- "나의 도서관". 온보딩의 liked_book_ids와는 별개다.
CREATE TABLE v_user_library (
    user_id  integer NOT NULL,
    book_id  integer NOT NULL,
    added_at timestamptz NOT NULL
);
CREATE INDEX v_user_library_user_idx ON v_user_library (user_id, added_at DESC);

-- rating은 ERD의 int 1-5가 아니라 numeric(2,1)이다.
-- BE 원본이 DECIMAL(3,1) 0.5단위(0.5~5.0)라 정수로 받으면 3.5점이 뭉개진다.
-- 취향 계산 경계: 4.0 이상 선호(+2) / 2.5~3.5 중립(0) / 2.0 이하 비선호(-2).
-- CHECK는 범위만 건다. 0.5 단위까지 강제하면 원본 예외값 하나에 복제가 통째로 거부된다.
CREATE TABLE v_user_reviews (
    user_id    integer NOT NULL,
    book_id    integer NOT NULL,
    rating     numeric(2, 1) NOT NULL CHECK (rating BETWEEN 0.5 AND 5.0),
    created_at timestamptz NOT NULL
);
CREATE INDEX v_user_reviews_user_idx ON v_user_reviews (user_id, created_at DESC);


-- ===========================================================================
-- §2. AI 소유 3종 (AI만 쓴다)
-- ===========================================================================

-- §2-1. 도서 임베딩
-- 차원 384 = multilingual-e5-small. 모델을 바꿔 차원이 달라지면
-- 이 테이블과 taste_profile을 전량 재생성해야 한다(pgvector는 dim이 DDL에 박힌다).
-- BE에서 도서가 삭제돼 v_books 행이 지워지면 이 행도 cascade로 함께 지워진다.
CREATE TABLE book_embeddings (
    book_id   integer PRIMARY KEY REFERENCES v_books (book_id) ON DELETE CASCADE,
    embedding vector(384) NOT NULL,
    dim       integer NOT NULL,   -- 저장 전 인덱스 차원 일치 검증의 기준값
    model     text    NOT NULL    -- 모델 교체 시 재생성 대상 구분
);
CREATE INDEX book_embeddings_hnsw_idx ON book_embeddings
    USING hnsw (embedding vector_cosine_ops);


-- §2-2. 취향 프로필
-- ⑥이 upsert하고 ③④⑦이 조회한다. 사용자당 한 행, 호출마다 전체 재계산.
-- 탈퇴한 사용자의 행은 삭제해야 한다(복제로는 사라지지 않는다).
-- 단 비즈니스 정책상 7일 복구 기간이 있으므로 삭제 시점은 "탈퇴 완료" 후다.
CREATE TABLE taste_profile (
    user_id         integer PRIMARY KEY,
    centroid        vector(384),                       -- cold_start면 생성 불가라 NULL 허용
    tag_weights     jsonb   NOT NULL DEFAULT '{}'::jsonb,
    cold_start      boolean NOT NULL,
    profile_version integer NOT NULL,                  -- 순위 영향 값 변경 시 증가. 피드 커서에 실린다
    -- 계산 시각이 아니라 이 프로필이 반영한 이력 행들의 최대 시각.
    -- ③④가 이 값보다 나중의 이력만 가산해 이중 반영을 막는다.
    -- 계산 시각으로 두면 복제가 늦은 이력이 프로필에도 가산에도 빠져 영구 누락된다.
    -- 반영한 이력이 없으면 NULL이고, 이때는 이력 전부를 가산한다.
    computed_at     timestamptz
);


-- §2-3. 멱등 기록
-- ⑥⑦의 멱등 키. 같은 키·같은 본문은 저장 응답을 200으로 재생,
-- 같은 키·다른 본문은 409. 최소 24시간 보관.
CREATE TABLE idempotency_records (
    idempotency_key text  PRIMARY KEY,
    body_hash       text  NOT NULL,   -- 같은 키에 다른 본문이 오면 409로 거절하기 위한 대조값
    stored_response jsonb NOT NULL,   -- 재도착 시 재계산 없이 그대로 반환
    created_at      timestamptz NOT NULL
);
CREATE INDEX idempotency_records_created_at_idx ON idempotency_records (created_at);

COMMIT;
