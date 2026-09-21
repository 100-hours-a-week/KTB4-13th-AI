-- ===========================================================================
-- 개발용 가짜 유저 4명 + 가짜 책 5권
--
-- 목적: ⑥ /preferences/profile, ④ /recommendations/feed, ⑦ /agent/act 를
-- 실제 카탈로그 없이도 로컬에서 바로 테스트하기 위한 최소 데이터.
--
-- 책 title이 전부 "[TEST]"로 시작한다 — 실제 카탈로그 적재 후에도 섞이지 않게
-- 구분하기 위함. 정리할 때: DELETE FROM v_books WHERE title LIKE '[TEST]%';
--
-- book_id 는 9000001~9000005, user_id 는 9001~9004 대역을 써서 나중에 실제
-- 카탈로그·BE 유저 ID와 절대 안 겹치게 한다. 900001~ 이 아니라 9000001~ 인
-- 이유: scripts/ingest_catalog.sh 가 book_id를 1부터 순번으로 매기는데,
-- 다음 배송(#6, 164만 권)이 900000을 넘으면 이 대역과 충돌해 ON CONFLICT로
-- 그 행이 조용히 빠진다(#23 리뷰). 카탈로그가 이 대역에 닿으면 스크립트가
-- 실패하도록 막아뒀다.
--
-- 유저 4명의 역할 (왜 이렇게 나눴는지):
--   9001 (A) 이력 전혀 없음            → cold_start: true 가 실제로 나오는지
--   9002 (B) 구매+선호리뷰+담기 각 1권 → 여러 신호(+3,+2,+1)가 합산되는지
--   9003 (C) 비선호 리뷰(1.5) 1권      → 그 책이 후보에서 실제로 빠지는지
--   9004 (D) 담기만 1권                → 최소 신호(+1) 하나로도 개인화가 도는지
--
-- 시각을 전부 다르게(과거로) 넣은 이유: computed_at(프로필이 반영한 이력의
-- 최대 시각) 로직을 나중에 테스트할 때 "이 시각 이전/이후"를 구분해야 하는데
-- 전부 같은 시각이면 그 테스트를 할 수 없다.
-- ===========================================================================

BEGIN;

-- 재실행 안전: 이전 실행분을 먼저 지운다 (v_books는 ON CONFLICT로 이미 안전)
DELETE FROM v_user_purchases WHERE user_id BETWEEN 9001 AND 9004;
DELETE FROM v_user_reviews   WHERE user_id BETWEEN 9001 AND 9004;
DELETE FROM v_user_library   WHERE user_id BETWEEN 9001 AND 9004;

-- ── 가짜 책 5권 ────────────────────────────────────────────────────────────
INSERT INTO v_books (book_id, title, author, publisher, price, in_stock, cover_url, category, pub_year, description)
VALUES
    (9000001, '[TEST] 비 오는 날의 산책', '테스트작가1', '테스트출판사', 13500, true, NULL, '에세이', 2023, '비 오는 날 읽기 좋은 잔잔한 에세이.'),
    (9000002, '[TEST] 이별 후의 위로', '테스트작가2', '테스트출판사', 12800, true, NULL, '에세이', 2022, '이별 후 마음을 다독이는 짧은 소설.'),
    (9000003, '[TEST] 성장의 기록', '테스트작가3', '테스트출판사', 14000, true, NULL, '한국소설', 2024, '한 사람의 성장을 담은 잔잔한 이야기.'),
    (9000004, '[TEST] 힐링 에세이', '테스트작가4', '테스트출판사', 11000, true, NULL, '에세이', 2021, '지친 하루를 위로하는 힐링 에세이.'),
    (9000005, '[TEST] 재미없는 책', '테스트작가5', '테스트출판사', 9900, true, NULL, '기타', 2020, '비선호 리뷰 테스트용 책.')
ON CONFLICT (book_id) DO NOTHING;

-- ── 유저 9001 (A) — 이력 없음 ────────────────────────────────────────────────
-- 아무것도 넣지 않는다. cold_start 테스트가 목적이므로 비워두는 것 자체가 데이터다.

-- ── 유저 9002 (B) — 구매 + 선호 리뷰 + 담기 ─────────────────────────────────
INSERT INTO v_user_purchases (user_id, book_id, purchased_at)
VALUES (9002, 9000001, now() - interval '20 days');

INSERT INTO v_user_reviews (user_id, book_id, rating, created_at)
VALUES (9002, 9000002, 4.5, now() - interval '15 days');

INSERT INTO v_user_library (user_id, book_id, added_at)
VALUES (9002, 9000003, now() - interval '10 days');

-- ── 유저 9003 (C) — 비선호 리뷰 ──────────────────────────────────────────────
INSERT INTO v_user_reviews (user_id, book_id, rating, created_at)
VALUES (9003, 9000005, 1.5, now() - interval '8 days');

-- ── 유저 9004 (D) — 담기만 ──────────────────────────────────────────────────
INSERT INTO v_user_library (user_id, book_id, added_at)
VALUES (9004, 9000004, now() - interval '5 days');

COMMIT;
