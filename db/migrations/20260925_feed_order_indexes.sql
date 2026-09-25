-- ===========================================================================
-- ④ 개인화를 끈 목록(cold_start)을 색인 순서로 읽기 위한 색인 (이슈 #188, 설계 #186)
--
-- 개인화를 끈 목록은 요청마다 카탈로그 전체를 읽고 정렬했다. 261만 권에서 한 쪽에 1초가
-- 넘는다. 아래 색인이 있으면 정렬 순서대로 앞에서부터 읽기만 한다(0.1–4ms).
--
-- - 신간순: 판매 수가 없는 책(0점)을 신간순으로 이어 붙일 때, 최신순 정렬
-- - 분류 + 신간순: 분류 필터를 건 목록
-- - 가격순: 가격순 정렬. 001 의 v_books_price_idx 는 재고 있는 책만 담아(WHERE in_stock) 못 쓴다
--
-- 261만 권 기준 만드는 데 각 1–5초, 크기 각 60–130MB.
-- 식은 app/feed/cold_start.py 의 ORDER BY 와 같아야 색인을 탄다(NULLS LAST 포함).
--
-- CONCURRENTLY: 복제가 계속 쓰는 테이블이라, 색인을 만드는 동안 쓰기를 막지 않게 한다.
-- 대신 트랜잭션 안에서는 실행할 수 없다. psql -f 로 이 파일만 따로 적용한다.
-- ===========================================================================

CREATE INDEX CONCURRENTLY IF NOT EXISTS v_books_newest_idx
    ON v_books (pub_year DESC NULLS LAST, book_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS v_books_category_newest_idx
    ON v_books (category, pub_year DESC NULLS LAST, book_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS v_books_price_order_idx
    ON v_books (price, book_id);
