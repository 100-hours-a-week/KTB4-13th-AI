-- ===========================================================================
-- 쓰지 않게 된 v_books 가격 색인 지우기 (이슈 #232)
--
-- 가격·재고를 상품 표(v_products)에서 읽게 되면서(#208) 아래 두 색인을 어떤 쿼리도 쓰지 않는다.
-- v_books.price 는 복제가 채우지 않는 칸이라(ERD 3.4) 색인도 빈 값만 담고, 책이 바뀔 때마다
-- 갱신 비용만 붙는다. 가격순은 v_products_price_order_idx 가 맡는다(20260925_v_products.sql).
--
-- - v_books_price_idx: 001_init.sql
-- - v_books_price_order_idx: 20260925_feed_order_indexes.sql
--
-- CONCURRENTLY: 복제가 계속 쓰는 테이블이라, 지우는 동안 읽기·쓰기를 막지 않게 한다.
-- 대신 트랜잭션 안에서는 실행할 수 없다. psql -f 로 이 파일만 따로 적용한다.
-- ===========================================================================

DROP INDEX CONCURRENTLY IF EXISTS v_books_price_idx;

DROP INDEX CONCURRENTLY IF EXISTS v_books_price_order_idx;
