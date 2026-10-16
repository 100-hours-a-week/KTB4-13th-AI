-- ===========================================================================
-- 취향 프로필에 관심사 점수와 추천 목록 칸을 더한다 (이슈 #439, 설계 #395)
--
-- 관심사별 홈 추천(#395)이 쓴다. ⑥이 계산해 저장하고, 홈과 더 보기가 추천 목록을 읽는다. 이 칸을 읽고
-- 쓰는 코드는 기능 플래그 뒤에 있어(#440), 이 마이그레이션이 적용되기 전에 머지돼도 깨지지 않는다.
-- 지금 칸(centroid, tag_weights 등)은 챗봇 ③이 계속 쓰므로 그대로 둔다.
--
-- 새 칸은 고정된 기본값이 있어 표를 다시 쓰지 않고 붙는다(PostgreSQL 11 부터). 칸을 붙이는 동안만 잠깐
-- 이 표를 잠근다. 칸이 이미 있으면 건너뛴다(IF NOT EXISTS).
-- ===========================================================================

ALTER TABLE taste_profile
    -- 관심사마다 재료(온보딩, 취향 기억, 책)별 점수. {"추리/스릴러": {"memory": 2, "books": 3}}
    ADD COLUMN IF NOT EXISTS interest_scores  jsonb   NOT NULL DEFAULT '{}'::jsonb,
    -- 관심사마다 그 관심사에 점수를 더한 책들의 벡터 평균(실수 384개). 책이 있는 관심사만
    ADD COLUMN IF NOT EXISTS interest_vectors jsonb   NOT NULL DEFAULT '{}'::jsonb,
    -- 조건 목록. ["300쪽 이하", "2만 원 이하"]
    ADD COLUMN IF NOT EXISTS conditions       jsonb   NOT NULL DEFAULT '[]'::jsonb,
    -- 이력의 책마다 AI 서버가 처음 본 날. {"9788937462788": "2026-10-15"}. 오래된 취향을 약하게 할 때 쓴다
    ADD COLUMN IF NOT EXISTS book_first_seen  jsonb   NOT NULL DEFAULT '{}'::jsonb,
    -- 추천 목록 200권. 놓을 순서대로 {"books": [{"book_id": 123, "interest": "추리/스릴러", "match_score": 72}],
    -- "substituted": ["요리"]}. substituted 는 시간 예산을 넘겨 대신한 관심사로, 밤에 다시 뽑는다
    ADD COLUMN IF NOT EXISTS feed             jsonb,
    -- 추천 목록의 판 번호. 새로 뽑을 때마다 1씩 올린다. 커서에 실어 스크롤하는 중에 바뀌었는지 가린다
    ADD COLUMN IF NOT EXISTS feed_version     integer NOT NULL DEFAULT 0,
    -- 바로 앞 판의 추천 목록(모양은 feed 와 같음). 스크롤하는 중에 목록이 바뀐 사람이 이어 읽을 때 쓴다
    ADD COLUMN IF NOT EXISTS feed_prev        jsonb,
    -- 시간 예산을 넘겨 인기 책으로 채운 자리가 있는지. 있으면 ⑨가 이 사람의 목록만 다시 뽑는다
    ADD COLUMN IF NOT EXISTS feed_partial     boolean NOT NULL DEFAULT false;
