-- ===========================================================================
-- ①④ 가격·재고를 상품 표(v_products)에서 읽는다 (이슈 #205, 설계 #208)
--
-- BE 는 가격·재고를 책(books)이 아니라 상품(products)에 두고, 복제가 이 표를 v_products 로
-- 옮긴다. v_books.price·in_stock 은 복제가 채우지 않아 서버에서는 전부 0원·품절이다.
--
-- 표: 서버에는 복제가 만든 표가 이미 있어 건너뛴다(IF NOT EXISTS). 로컬·CI 에서 쓰는 정의이고
--     AI 가 읽는 칸만 적었다(서버 표에는 상품명·원가 등 칸이 더 있다). 칸 이름은 BE 상품 표를 따른다.
-- 색인: 책마다 상품을 찾을 때(book_id), ④ 가격순 목록을 색인 순서로 읽을 때(가격, book_id).
--       식은 app/feed/cold_start.py 의 ORDER BY 와 같아야 색인을 탄다.
--
-- CONCURRENTLY: 복제가 계속 쓰는 표라, 색인을 만드는 동안 쓰기를 막지 않게 한다.
-- 대신 트랜잭션 안에서는 실행할 수 없다. psql -f 로 이 파일만 따로 적용한다.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS v_products (
    id               bigint        PRIMARY KEY,
    book_id          integer       NOT NULL,
    discounted_price numeric(19,2) NOT NULL,   -- 할인 적용 후 판매가(원)
    stock_quantity   integer       NOT NULL
);

CREATE INDEX CONCURRENTLY IF NOT EXISTS v_products_book_id_idx
    ON v_products (book_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS v_products_price_order_idx
    ON v_products (discounted_price, book_id);
