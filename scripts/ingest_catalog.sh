#!/usr/bin/env bash
# 카탈로그(cat_books.jsonl)를 v_books로 적재한다.
#
# 사용법: scripts/ingest_catalog.sh <cat_books.jsonl 경로> [DB이름]
#
# 두 단계로 나눈다(개발-로그.md "적재 방법" 참고):
#   1. stg_cat_books에 원본을 가공 없이 통째로 붓는다
#   2. 거기서 v_books로 변환한다 (active=1만, price 이상치 제외, book_id 임시 발급)
#
# \copy에 CSV 포맷 + 흔치 않은 구분자/인용부호를 쓰는 이유: 기본 TEXT 포맷은
# 백슬래시를 이스케이프로 해석해 JSON을 깨뜨린다.

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
-- 남아있으면 새 번호와 충돌해 ON CONFLICT로 조용히 누락된다. 900000 미만
-- (카탈로그 대역)만 지우고, db/seed/dev_fake_users.sql 의 테스트 책(900001~)은
-- 건드리지 않는다.
DELETE FROM v_books WHERE book_id < 900000;

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
WHERE book_id < 900000;  -- 900000대는 db/seed/dev_fake_users.sql 의 테스트 책
"
