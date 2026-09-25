#!/usr/bin/env bash
# 카탈로그(cat_books.jsonl)를 v_books로 적재한다.
#
# 파일 출처 주의: cat_books.jsonl은 배송 #5의 델리버리 패키지
# (delivery_0005_full_*.zip)가 아니라 별도 JSON 스냅샷 패키지
# (bookjeok_json_YYYYMMDDThhmmssZ.zip) 안에 있다.
#
# 사용법: scripts/ingest_catalog.sh <cat_books.jsonl 경로> [DB이름]
#
# 두 단계로 나눈다(개발-로그.md "적재 방법" 참고):
#   1. stg_cat_books에 원본을 가공 없이 통째로 붓는다
#   2. 거기서 v_books로 변환한다 (active=1만, price 이상치 제외, book_id 임시 발급)
#
# \copy에 CSV 포맷 + 흔치 않은 구분자/인용부호를 쓰는 이유: 기본 TEXT 포맷은
# 백슬래시를 이스케이프로 해석해 JSON을 깨뜨린다.
#
# ⚠️ ② /embeddings 완성 후 book_embeddings를 채운 뒤에 이 스크립트를 다시
# 돌리면, DELETE FROM v_books가 FK ON DELETE CASCADE로 book_embeddings를
# 전량 날린다. 재적재 전에는 반드시 재임베딩까지 같이 할 것.
#
# book_id는 isbn13 사전순 row_number()라 결정적이지 않다 — 다음 덤프에서
# 책이 한 권만 늘어도 뒤 번호가 전부 밀린다. 지금은 재적재 비용이 낮아
# (임베딩 전) 그냥 두지만, 재적재가 잦아지면 isbn13 → book_id 매핑 테이블을
# 따로 둬서 번호를 고정하는 게 낫다.

set -euo pipefail

JSONL_PATH="${1:?사용법: scripts/ingest_catalog.sh <cat_books.jsonl 경로> [DB이름]}"
DB_NAME="${2:-bookjeok_ai_dev}"

if [ ! -f "$JSONL_PATH" ]; then
    echo "파일을 찾을 수 없습니다: $JSONL_PATH" >&2
    exit 1
fi

echo "== 1단계: stg_cat_books 비우고 다시 붓기 =="
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -c "TRUNCATE stg_cat_books;"
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" -c "\copy stg_cat_books(data) FROM '$JSONL_PATH' WITH (FORMAT csv, DELIMITER E'\x02', QUOTE E'\x01')"

echo "== 적재된 행 수 =="
psql -d "$DB_NAME" -c "SELECT count(*) FROM stg_cat_books;"

echo "== 2단계: v_books로 변환 =="
psql -v ON_ERROR_STOP=1 -d "$DB_NAME" <<'SQL'
BEGIN;

-- book_id는 매번 isbn13 순서로 새로 번호를 매기므로, 재실행 시 이전 결과가
-- 남아있으면 새 번호와 충돌해 ON CONFLICT로 조용히 누락된다. 9,000,000 미만
-- (카탈로그 대역)만 지우고, db/seed/dev_fake_users.sql 의 테스트 책
-- (9,000,001~)은 건드리지 않는다.
DELETE FROM v_books WHERE book_id < 9000000;

-- 안전 검사: 카탈로그가 900만 건을 넘어 book_id가 테스트 대역과 겹치면
-- ON CONFLICT로 그 행들이 "에러 없이 조용히" 빠지는 게 제일 위험하다.
-- 조용히 넘어가지 말고 여기서 크게 실패시킨다.
DO $$
DECLARE
    filtered_count bigint;
BEGIN
    SELECT count(*) INTO filtered_count
    FROM stg_cat_books
    WHERE (data->>'active')::int = 1
      AND data->>'title' IS NOT NULL
      AND (data->>'price')::int BETWEEN 1000 AND 500000;

    IF filtered_count >= 9000000 THEN
        RAISE EXCEPTION '카탈로그가 900만 건(%)을 넘어 book_id가 테스트 대역(9,000,001~)과 겹칩니다. db/seed/dev_fake_users.sql의 테스트 book_id 대역을 올린 뒤 다시 실행하세요.', filtered_count;
    END IF;
END $$;

WITH filtered AS (
    SELECT
        data,
        (data->>'price')::int AS price_int,
        row_number() OVER (ORDER BY data->>'isbn13') AS rn
    FROM stg_cat_books
    WHERE (data->>'active')::int = 1
      AND data->>'title' IS NOT NULL
      AND (data->>'price')::int BETWEEN 1000 AND 500000
      -- 1,000원 미만·50만원 초과는 원본 입력 오류라 거른다 (개발-로그.md)
)
INSERT INTO v_books (book_id, title, author, publisher, price, in_stock, cover_url, category, pub_year, description)
SELECT
    rn AS book_id,                          -- 개발용 임시 ID. BE 실제 ID로 재적재 전제
    data->>'title',
    NULLIF(data->>'author', ''),
    NULLIF(data->>'publisher', ''),
    price_int,
    true,                                    -- 재고는 커머스 소관. 개발용 기본값
    NULLIF(data->>'cover_url', ''),
    NULLIF(data->>'category', ''),
    NULLIF(data->>'pub_year', '')::int,
    NULLIF(data->>'description', '')
FROM filtered
ON CONFLICT (book_id) DO NOTHING;

-- 가격·재고는 상품 표에서 읽는다(#207). 서버처럼 책마다 상품 하나를 만들고,
-- 재고는 BE 더미값(999999)과 같게 채운다. 상품 번호는 책 번호를 그대로 쓴다.
DELETE FROM v_products WHERE book_id < 9000000;
INSERT INTO v_products (id, book_id, discounted_price, stock_quantity)
SELECT book_id, book_id, price, 999999
FROM v_books
WHERE book_id < 9000000;

COMMIT;
SQL

echo "== v_books 적재 결과 =="
psql -d "$DB_NAME" -c "
SELECT
    count(*) AS total,
    count(description) AS with_description,
    count(*) FILTER (WHERE cover_url IS NULL) AS no_cover,
    count(*) FILTER (WHERE author IS NULL) AS no_author
FROM v_books
WHERE book_id < 9000000;  -- 9,000,000대는 db/seed/dev_fake_users.sql 의 테스트 책
"
