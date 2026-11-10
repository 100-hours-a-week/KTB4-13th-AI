-- ===========================================================================
-- 개발용 가짜 책 5권
--
-- 목적: ⑥ /preferences/profile, ④ /recommendations/feed, ⑦ /agent/act 를
-- 실제 카탈로그 없이도 로컬에서 바로 테스트하기 위한 최소 데이터.
--
-- 사용자 이력(구매·나의 도서관·리뷰)은 넣지 않는다. 복제한 사용자 표를 지웠고(#307), 이력은
-- 요청으로 보낸다(⑥ 의 history, ③④ 의 purchased_isbns·library_isbns).
--
-- 책 title이 전부 "[TEST]"로 시작한다 — 실제 카탈로그 적재 후에도 섞이지 않게
-- 구분하기 위함. 정리할 때: DELETE FROM v_books WHERE title LIKE '[TEST]%';
--
-- book_id 는 9000001–9000005 대역을 써서 나중에 실제 카탈로그와 절대 안 겹치게 한다.
-- 900001~ 이 아니라 9000001~ 인 이유: scripts/ingest_catalog.sh 가 book_id를 1부터 순번으로 매기는데,
-- 다음 배송(#6, 164만 권)이 900000을 넘으면 이 대역과 충돌해 ON CONFLICT로
-- 그 행이 조용히 빠진다(#23 리뷰). 카탈로그가 이 대역에 닿으면 스크립트가
-- 실패하도록 막아뒀다.
-- ===========================================================================

BEGIN;

-- ── 가짜 책 5권 ────────────────────────────────────────────────────────────
INSERT INTO v_books (book_id, title, author, publisher, price, in_stock, cover_url, category, pub_year, description)
VALUES
    (9000001, '[TEST] 비 오는 날의 산책', '테스트작가1', '테스트출판사', 13500, true, NULL, '에세이', 2023, '비 오는 날 읽기 좋은 잔잔한 에세이.'),
    (9000002, '[TEST] 이별 후의 위로', '테스트작가2', '테스트출판사', 12800, true, NULL, '에세이', 2022, '이별 후 마음을 다독이는 짧은 소설.'),
    (9000003, '[TEST] 성장의 기록', '테스트작가3', '테스트출판사', 14000, true, NULL, '한국소설', 2024, '한 사람의 성장을 담은 잔잔한 이야기.'),
    (9000004, '[TEST] 힐링 에세이', '테스트작가4', '테스트출판사', 11000, true, NULL, '에세이', 2021, '지친 하루를 위로하는 힐링 에세이.'),
    (9000005, '[TEST] 재미없는 책', '테스트작가5', '테스트출판사', 9900, true, NULL, '기타', 2020, '비선호 리뷰 테스트용 책.')
ON CONFLICT (book_id) DO NOTHING;

-- 가격·재고는 상품 표에서 읽는다(#207). 책마다 상품 하나, 상품 번호는 책 번호와 같게 둔다.
INSERT INTO v_products (id, book_id, discounted_price, stock_quantity)
SELECT book_id, book_id, price, 999999
FROM v_books
WHERE book_id BETWEEN 9000001 AND 9000005
ON CONFLICT (id) DO NOTHING;

COMMIT;
