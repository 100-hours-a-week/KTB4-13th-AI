-- 평가 DB 의 AI 책 분류 표(book_classes)를, 적재할 때 남은 원본 표(stg_cat_books)로 채운다(#422).
-- 표는 마이그레이션(db/migrations/20261016_book_classes.sql, #421)으로 먼저 만든다. 개발·운영 서버는 이 파일이
-- 아니라 수집 쪽이 만든 파일을 app/jobs/load_book_classes.py 로 넣는다. 전량을 바꿔 넣으니 여러 번 돌려도 된다.
BEGIN;
TRUNCATE book_classes;
INSERT INTO book_classes (isbn13, class_no, reader_code, form_code, page_count)
SELECT data->>'isbn13',
       nullif(left(data->>'d4l_class_no', 20), ''),
       CASE WHEN data->>'reader_code' ~ '^[0-9]$' THEN data->>'reader_code' END,
       CASE WHEN data->>'form_code' ~ '^[0-9]$' THEN data->>'form_code' END,
       CASE WHEN data->>'page_count' ~ '^[0-9]{1,6}$' AND (data->>'page_count')::int > 0
            THEN (data->>'page_count')::int END
FROM stg_cat_books
WHERE data->>'isbn13' ~ '^[0-9]{13}$';
COMMIT;
