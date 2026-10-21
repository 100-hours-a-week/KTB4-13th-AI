-- ===========================================================================
-- 복제 표의 취소·삭제 표시 칸을 표 정의에 넣는다 (이슈 #296 의 선행, #295 의 일부)
--
-- 복제는 BE 에서 취소·삭제된 줄을 지우지 않고 표시만 남긴다(CLOUD #3, #12, 9/28 적용).
-- 서버 표에는 이 칸들이 이미 있는데 여기에는 없어서, 로컬·CI 의 표가 서버와 달랐다.
-- AI 가 이 표시를 읽어 거르려면(#296) 칸이 먼저 있어야 한다.
--
-- 서버: 칸이 이미 있어 전부 건너뛴다(IF NOT EXISTS). 적용하지 않아도 된다.
-- 로컬·CI: 이 파일로 칸이 생긴다. 타입과 기본값은 서버 표(2026-10-07 확인)와 같게 적었다.
--
-- AI 가 읽는 칸만 적었다(20260925_v_products.sql 과 같은 기준). 서버 표에는 주문 품목 번호,
-- 수량, 취소 시각, 리뷰 번호, 수정 시각 같은 칸과 복제용 중복 방지 색인이 더 있다.
-- 그것들과 replication_state 표는 #295 에서 다룬다.
-- ===========================================================================

-- 주문 전체의 상태(BE orders.status: CREATED, PAID, PARTIAL_CANCELED, CANCELED, EXPIRED)와 삭제 시각.
-- 주문 품목의 상태가 아니다. 운영에 품목 상태에는 없는 EXPIRED 가 있다(2026-10-21 확인).
-- 서버에서도 order_status 는 비어 있을 수 있고 기본값이 없다.
ALTER TABLE v_user_purchases
    ADD COLUMN IF NOT EXISTS order_status text,
    ADD COLUMN IF NOT EXISTS deleted_at   timestamptz;

-- 리뷰를 지우면 BE 는 active_flag 를 끄고 deleted_at 을 채운다.
ALTER TABLE v_user_reviews
    ADD COLUMN IF NOT EXISTS active_flag boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS deleted_at  timestamptz;

-- BE 에서 내린 책과 상품. 줄은 남고 deleted_at 만 채워진다.
ALTER TABLE v_books
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE v_products
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
