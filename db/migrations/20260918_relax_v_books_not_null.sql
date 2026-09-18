-- v_books 일부 컬럼의 NOT NULL 해제 (이슈 #8)
--
-- 카탈로그 원천(국립중앙도서관 서지 API + 도서관정보나루)에 값이 없는 책이 있다.
-- 표지가 없는 책이 대부분이고, 저자·출판사가 빈 책도 일부 있다.
-- 001_init.sql 그대로면 그런 책은 복제·적재 단계에서 통째로 실패한다.
--
-- 이 세 컬럼은 화면 표시용이라 없어도 검색·추천은 동작한다.
--   cover_url 없음 → BE/FE가 플레이스홀더 이미지로 대체
--   author·publisher 없음 → 키워드 인덱스에서 coalesce(..., '')로 이미 처리된다
--
-- 반대로 price·in_stock 은 NOT NULL 을 유지한다. 둘 다 BE 가 계산해 채우는
-- 커머스 값이고, ① 검색의 가격 구간·재고 필터가 이 값을 직접 조건으로 쓴다.
-- 값이 없다는 것은 원천이 아니라 복제가 잘못됐다는 뜻이므로 막는 편이 낫다.
--
-- 전제: BE 원본(books)도 같은 컬럼을 NULL 허용해야 복제가 성립한다. → BE 확인 항목

BEGIN;

ALTER TABLE v_books ALTER COLUMN author DROP NOT NULL;
ALTER TABLE v_books ALTER COLUMN publisher DROP NOT NULL;
ALTER TABLE v_books ALTER COLUMN cover_url DROP NOT NULL;

COMMIT;
