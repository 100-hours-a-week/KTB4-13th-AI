# 검색 시간 측정 도구

[#556](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/556)의 시간 측정에 쓰는 도구입니다. 앱 코드 밖이라 운영 동작은 바뀌지 않습니다. 지금은 검색어 고정([#591](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/591))과 한 회차 재기([#593](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/593))까지 있고, 회차 이어 돌리기·결과 정리는 다음 PR에서 더합니다.

| 파일 | 내용 | 저장소 |
| --- | --- | --- |
| `queries.py` | 잴 검색어와 데우기 검색어를 고른다 | 둔다 |
| `queries.json` | 고른 결과. 한 줄에 검색어 하나 | 둔다 |
| `vectors.py` | 검색어 벡터를 미리 만든다 | 둔다 |
| `out/vectors.json` | 만든 벡터(약 5MB) | 두지 않음 |
| `measure.py` | 한 회차를 잰다 | 둔다 |
| `out/<label>.jsonl` | 잰 기록 | 두지 않음 |

## 잴 검색어 170건

평가 묶음(`evals/search/cases.json`)에서 종류별로 고릅니다. 수는 #556 「측정 방법 › 캐시 상태 셋」 그대로입니다.

| 종류 | 수 | 고르는 법 |
| --- | --- | --- |
| 제목 | 60 | 시드 순서로 앞에서부터 |
| 표기 변형 | 20 | 평가 묶음의 비율대로: 띄어쓰기 없앰 6, 부제 뺌 4, 로마 숫자 3, 동그라미 숫자 2, 대소문자 5 |
| 저자 | 15 | 시드 순서로 |
| 필터 | 15 | 시드 순서로. 조건은 평가 묶음 그대로 |
| 서술형 | 40 | 전부 |
| 정렬 | 20 | 위에서 고르고 남은 제목 8·저자 6과, 서술형 6을 정렬만 바꿔서. 인기순 7, 최신순 7, 가격 낮은순 3, 높은순 3 |

- 순서는 `md5(시드:사례 번호)`로 정합니다. 묶음 안에서 사례 위치가 바뀌어도 같은 검색어가 나옵니다.
- 정렬은 후보를 다 모은 뒤 DB를 한 번 더 불러 따로 잽니다.

## 데우기 검색어 500건

빈 캐시에서 바로 재면 운영보다 나쁜 상태를 잽니다. 그래서 측정과 겹치지 않는 검색어로 먼저 데웁니다.

- 평가 DB에서 평가 묶음과 같은 규칙(`evals/search/build.py`)으로, 시드만 바꿔(`556-warmup`) 제목 450건·저자 50명을 뽑습니다.
- 평가 묶음에 든 책(ISBN), 검색어, 정답 제목, 저자는 쓰지 않습니다. 데우기가 측정 검색어를 미리 데우지 않게 하려는 것입니다.
- 서술형은 사람이 쓴 40건뿐이라 데우기에 쓰지 않습니다.

## 검색어 벡터

측정은 앱 서버 없이 SQL만 보냅니다. 그래서 검색어 벡터를 앱과 같은 함수(`app.gateway.embedding.embed`)로, 앱처럼 한 검색어씩 미리 만듭니다.

- 같은 검색어는 한 번만 만듭니다(654개).
- 파일에 `queries.json`의 지문을 같이 적습니다. 검색어가 바뀌면 측정 도구가 옛 벡터를 읽지 않습니다.

## 한 회차 재기

앱의 검색 함수(`app.search.service.search`)를 그대로 부르고, 검색어 벡터만 미리 만든 것으로 바꿉니다. 그래서 SQL, `SET LOCAL`, 준비된 문장이 앱과 같습니다. 캐시 비우기(재부팅)와 회차 순서는 이 도구 밖(다음 PR)에서 정합니다.

| 기록 | 어떻게 |
| --- | --- |
| 검색 전체 시간 | 검색 함수 한 번. 임베딩 시간은 들어가지 않습니다(실험 AI 서버에서 따로 잼) |
| 단계별 시간 | 키워드(`keyword`, 후보 질의 한 번마다 `keyword_query`), 벡터, 제목 일치, 정렬, 책 조회 함수를 감싸 잽니다 |
| 문장별 읽은 블록·시간 | `--pgss`를 주면 검색마다 `pg_stat_statements`를 비우고 읽습니다. DB 캐시 안(`shared_blks_hit`)·밖(`shared_blks_read`), 실행·계획 시간, 판이 18이면 일반·맞춤 계획 횟수 |
| 결과 | 위 20권의 책 번호. 설정끼리, 실험 DB와 운영끼리 일치율을 봅니다 |
| 회차 머리 | 설정, 코드 커밋, 검색어 파일 지문, PostgreSQL·확장 판, 주요 설정 값, 큰 표·색인 크기, DB가 켜진 시각(재부팅 확인) |

- 바꿔 보는 설정: 탐색 폭(`--ef-search`)은 앱이 `SET LOCAL`로 거는 상수를 바꾸고, `--random-page-cost`·`--effective-io-concurrency`는 앱이 바꾸지 않는 값이라 연결의 세션 설정으로 겁니다.
- 연결은 하나만 씁니다. 준비된 문장은 연결마다 따로라, 연결이 여럿이면 같은 문장이 일반 계획으로 바뀌는 때가 회차마다 달라집니다. 운영 앱은 연결이 10개라 바뀌는 때가 더 늦습니다.
- 연결은 읽기 전용(`default_transaction_read_only`)입니다.
- 다음 페이지 커서는 서명 키가 있어야 만들어서, 재는 동안은 고정 값으로 바꿉니다(수 마이크로초라 시간에 닿지 않음).
- 이름이 `_eval`로 끝나는 DB에서만 돕니다. 다른 DB(개발 서버 예비 측정)는 `--any-db`를 붙입니다.

## 주의

- 필터 사례는 같은 낱말("사랑", "여행" 등)을 조건만 바꿔 쓰고, 정렬 사례 6건은 서술형 검색어를 다시 씁니다. 한 회차에서 앞 사례가 뒤 사례의 캐시를 데우므로, 처음 조회는 종류별로 나눠 보고 순서를 회차마다 섞습니다.
- 데우기 검색어는 평가 DB(10/13 판)에서 뽑았습니다. 실험 서버(운영 사본)에 그 책이 없어도 검색어로는 그대로 씁니다.

## 돌리기

```bash
# 검색어 고르기(평가 DB를 읽기만 함). 결과는 queries.json
DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.searchlab.queries
# 벡터 만들기(DB를 쓰지 않음, 맥에서 약 40초). 결과는 out/vectors.json
DATABASE_URL=postgresql://localhost/bookjeok_ai_eval uv run python -m evals.searchlab.vectors
```

```bash
# 한 회차: 데우기 500건 → 잴 검색어 170건을 한 번씩(처음 조회). 순서는 회차마다 다른 값으로 섞는다
DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.measure --label base --state first --warmup --order-seed r1 --pgss
# 반복 조회: 같은 검색어를 7번씩(첫 번은 판정에 쓰지 않음). 탐색 폭만 바꿔서
DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.measure --label ef1000 --state repeat --ef-search 1000 --pgss
```

`--pgss`는 실험 DB에 `CREATE EXTENSION pg_stat_statements`를 한 번 해 둬야 합니다(RDS 기본 설정에서 미리 읽혀 있음).

벡터 파일은 저장소에 없으니, 부하·측정 EC2에서는 저장소를 받은 뒤(#556 「시험 환경」의 준비 명령) 두 번째 명령을 한 번 돌립니다. `DATABASE_URL`은 설정 검사 때문에만 필요합니다.
