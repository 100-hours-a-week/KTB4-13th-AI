-- ===========================================================================
-- 002. v_books 의 NOT NULL 제약을 실제 카탈로그 데이터에 맞춘다 (이슈 #8)
--
-- 국중 서지 카탈로그 실측(2026-09-18, active 2,389,488권):
--   cover_url  빈 값 2,051,272 (85.8%)  — 국중이 표지를 주지 않는 책이 대부분
--   author     빈 값    40,101 ( 1.7%)  — 국중·정보나루 둘 다 저자를 주지 않음
--   publisher  빈 값         4          — 〃
-- NOT NULL 을 유지하면 위 행이 적재되지 않는다. 플레이스홀더로 채우면
-- "표지가 있는 줄 알고 깨진 이미지를 그리는" 문제가 생기므로 NULL 을 허용한다.
--
-- in_stock 은 NOT NULL 로 남긴다. NULL 을 허용하면 in_stock_only 필터와
-- v_books_price_idx 의 WHERE in_stock 에서 "재고 모름"이 조용히 품절로 처리된다.
-- 카탈로그 덤프에는 재고 값이 없으므로(전부 NULL) BE 복제 전 개발용 적재에서는
-- 적재 스크립트가 기본값을 채운다.
-- ===========================================================================

ALTER TABLE v_books ALTER COLUMN author    DROP NOT NULL;
ALTER TABLE v_books ALTER COLUMN publisher DROP NOT NULL;
ALTER TABLE v_books ALTER COLUMN cover_url DROP NOT NULL;

COMMENT ON COLUMN v_books.author    IS '저자. 원천에 값이 없는 책이 있어 NULL 허용 (이슈 #8)';
COMMENT ON COLUMN v_books.publisher IS '출판사. 같은 이유로 NULL 허용 (이슈 #8)';
COMMENT ON COLUMN v_books.cover_url IS '표지 이미지 URL. 없는 책이 대부분이라 NULL 허용. 표시는 프런트가 대체 이미지로 처리 (이슈 #8)';
