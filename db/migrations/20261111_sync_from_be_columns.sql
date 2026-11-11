-- ===========================================================================
-- BE → AI 반영 배치(app/jobs/sync_from_be.py, #710 · #640 6단계)가 쓰는 칸과 진행 기록 표를 표 정의에 넣는다
--
-- 서버(개발·운영): 클라우드가 5분 복제를 만들 때 이미 생겨 있어 전부 건너뛴다(IF NOT EXISTS). 적용하지 않아도 된다.
-- 로컬·CI: 이 파일로 생긴다. 타입과 기본값은 개발 AI DB(2026-11-11 확인)와 같다.
--
-- deleted_at 은 20261021_replica_state_columns.sql 과 겹친다. 그 파일은 지운 사용자 복제 표도 고쳐서, 사용자 표를
-- 지운 DB 에서는 그대로 다시 못 돌린다. 배치가 쓰는 칸을 이 파일만으로 맞출 수 있게 다시 적는다.
-- ===========================================================================

ALTER TABLE v_books
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

ALTER TABLE v_products
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS deleted_at timestamptz;

-- 배치가 어디까지 옮겼는지(be_books·be_products 줄). 옛 복제의 books·products 줄은 건드리지 않는다
CREATE TABLE IF NOT EXISTS replication_state (
    name         text PRIMARY KEY,
    cursor_value timestamptz NOT NULL
);
