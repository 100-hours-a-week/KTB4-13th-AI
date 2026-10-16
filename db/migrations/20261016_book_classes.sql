-- ===========================================================================
-- AI 책 분류 표 (이슈 #421, 설계 #395)
--
-- 세부 분류 번호(정보나루 KDC 세분류), 쪽수, 독자 대상, 형태는 수집 덤프에만 있고 BE 책 표와
-- 복제(v_books)에는 없다. #395 개편이 산 책을 관심사로 가르고(세부 분류 번호) 쪽수 조건을 매기는 데
-- 쓴다. 수집 쪽이 덤프에서 만든 파일을 app/jobs/load_book_classes.py 로 넣는다(매번 전량 교체).
--
-- v_ 앞머리를 쓰지 않는다. BE 원본의 사본이 아니라 AI 가 직접 채우는 표다.
-- 키: v_books.isbn13 과 잇는다. 같은 형(text)과 같은 검사(13자리 숫자)를 써서 조인할 때 형을 바꾸지
--     않게 한다. 덤프에는 있고 책 표에는 없는 책, 그 반대도 있을 수 있어 외래 키는 걸지 않는다.
-- 값이 없는 칸은 NULL 이다. 세부 분류 번호는 덤프의 절반쯤에만 있다(10/15 기준 126만 권 중 68만 권).
-- ===========================================================================

CREATE TABLE IF NOT EXISTS book_classes (
    isbn13      text        PRIMARY KEY CHECK (isbn13 ~ '^[0-9]{13}$'),
    class_no    text        CHECK (char_length(class_no) BETWEEN 1 AND 20),  -- 받은 글자 그대로(예: 813.7)
    reader_code char(1)     CHECK (reader_code ~ '^[0-9]$'),                  -- 부가기호 첫 자리(독자 대상)
    form_code   char(1)     CHECK (form_code ~ '^[0-9]$'),                    -- 부가기호 둘째 자리(형태)
    page_count  integer     CHECK (page_count > 0),
    loaded_at   timestamptz NOT NULL DEFAULT now()
);
