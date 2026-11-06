# 검색 시간 측정 도구

[#556](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/556)의 시간 측정에 쓰는 도구입니다. 앱 코드 밖이라 운영 동작은 바뀌지 않습니다. 지금은 검색어 고정([#591](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/591))과 한 회차 재기([#593](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/593)), 회차 이어 돌리기([#595](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/595))까지 있고, 결과 정리는 다음 PR에서 더합니다.

| 파일 | 내용 | 저장소 |
| --- | --- | --- |
| `queries.py` | 잴 검색어와 데우기 검색어를 고른다 | 둔다 |
| `queries.json` | 고른 결과. 한 줄에 검색어 하나 | 둔다 |
| `vectors.py` | 검색어 벡터를 미리 만든다 | 둔다 |
| `out/vectors.json` | 만든 벡터(약 5MB) | 두지 않음 |
| `measure.py` | 한 회차를 잰다 | 둔다 |
| `out/<label>.jsonl` | 잰 기록 | 두지 않음 |
| `rounds.py` | 회차를 재부팅부터 이어 돌린다 | 둔다 |
| `plans/*.json` | 회차 계획(순서·설정) | 둔다 |
| `out/<plan>/` | 회차 요약(`rounds.jsonl`)과 회차마다 잰 기록 | 두지 않음 |

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
| 단계별 DB 쪽 값 | `--server-stats`를 주면 바깥 단계마다 `pg_stat_statements`의 앞뒤 차이로 DB 쪽 실행 시간, 문장 수, 캐시 안·밖 블록을 남깁니다(`server`). 통계를 비우지 않아 운영에서도 씁니다. 이 계정이 돌린 문장만 읽고, 읽는 문장 자신은 뺍니다 |
| 표·색인별 블록 | 회차 끝에 표·색인마다 캐시 안·밖 블록의 차이와 벡터 색인(hnsw) 캐시 적중률을 남깁니다(`statio`). 같은 DB의 다른 요청도 들어갑니다 |
| 결과 | 위 20권의 책 번호. 설정끼리, 실험 DB와 운영끼리 일치율을 봅니다 |
| 회차 머리 | 설정, 코드 커밋, 검색어 파일 지문, PostgreSQL·확장 판, 주요 설정 값, 큰 표·색인 크기, DB가 켜진 시각(재부팅 확인) |

- 바꿔 보는 설정: 탐색 폭(`--ef-search`)은 앱이 `SET LOCAL`로 거는 상수를 바꾸고, `--random-page-cost`·`--effective-io-concurrency`는 앱이 바꾸지 않는 값이라 연결의 세션 설정으로 겁니다.
- 벡터 검색 한도: 앱은 벡터 검색을 2초에서 끊고 키워드만으로 답합니다. 처음 조회는 이 한도에 자주 걸려(11/5 개발 DB에서 10건 중 9건) 그대로 재면 시간이 2초에서 잘립니다. 측정 회차는 `--vector-timeout 30`으로 풀고 재고, 앱이라면 끊겼을 검색은 잰 벡터 시간이 2초를 넘은 것으로 셉니다. 풀의 문장 한도(앱 5초)도 같이 늘립니다.
- 연결은 하나만 씁니다. 준비된 문장은 연결마다 따로라, 연결이 여럿이면 같은 문장이 일반 계획으로 바뀌는 때가 회차마다 달라집니다. 운영 앱은 연결이 10개라 바뀌는 때가 더 늦습니다.
- 연결은 읽기 전용(`default_transaction_read_only`)입니다.
- 다음 페이지 커서는 서명 키가 있어야 만들어서, 재는 동안은 고정 값으로 바꿉니다(수 마이크로초라 시간에 닿지 않음).
- 이름이 `_eval`로 끝나는 DB에서만 돕니다. 다른 DB(개발 서버 예비 측정, 운영 시험대 값)는 `--any-db`를 붙입니다. 검색마다 통계를 비우는 `--pgss`는 그래도 `_eval` DB에서만 돌고, 다른 DB는 `--server-stats`를 씁니다.
- 단계별 시간은 재는 쪽에서 잰 값이라 DB까지 오가는 시간이 들어 있습니다. 맥에서 터널로 운영을 재면 이 왕복이 커서, 실험 서버와 견줄 때는 `server` 값을 씁니다. `--server-stats`의 통계 읽기는 단계 시간에는 안 들어가고 검색 전체 시간에는 들어갑니다.

## 회차 이어 돌리기

부하·측정 EC2에서 돌립니다. 맥 연결이 끊겨도 회차가 이어지게 하려는 것입니다(`nohup`이나 `tmux` 안에서).

회차마다 이렇게 갑니다.

1. 짧은 CPU 계산(같은 파이썬 계산을 세 번 돌린 가장 빠른 값)
2. 실험 RDS 재부팅(`clear: "stop-start"`면 정지·시작). 켜질 때까지 기다린 뒤, DB가 켜진 시각이 바뀌었는지 봅니다. 그대로면 캐시가 안 빈 것이라 그 자리에서 멈춥니다
3. `measure.py`로 데우기 → 처음 조회(순서는 회차마다 다르게 섞음) → 반복 조회
4. 다시 짧은 CPU 계산. 앞뒤가 크게 다르면 그 회차는 버립니다(결과 정리 단계)
5. 재부팅 10분 전부터 회차 끝까지의 CloudWatch 지표(1분 간격): RDS 읽기 IOPS·지연·처리량, CPU, 남은 메모리, CPU 크레딧, 디스크 버스트 잔고. 이 EC2의 CPU·크레딧도

| 계획 | 무엇 | 회차 |
| --- | --- | --- |
| `testbench` | 시험대: 빈 캐시 확인, 운영과 맞춰 보기 | 지금 앱 값 두 번 |
| `a1-ef` | 기준선과 A1(탐색 폭) | 100·200·400·1000·1000·400·200·100 |
| `a5-eic` | A5(`effective_io_concurrency`), 키워드 쪽 검색어만 | 16·64·64·16 |
| `smoke` | 도구 확인용, 재부팅 없이 3건 | 2 |

- `--db-instance`의 이름에 `exp`가 없으면 재부팅하지 않습니다. 운영·개발 DB를 실수로 건드리지 않게 하려는 것입니다. 이름을 주지 않으면 재부팅 없이 잽니다(로컬 시험, 개발 서버 예비 측정).
- `--yes` 없이 돌리면 할 일만 보여 줍니다.
- 측정 계획(`testbench`·`a1-ef`·`a5-eic`)은 `"vector_timeout": 30`으로 벡터 검색 한도를 풀고 잽니다(「한 회차 재기」). 계획 전체의 값은 회차마다 넘기고, 회차에 같은 칸이 있으면 그 값이 앞섭니다.
- 재부팅만으로 OS 캐시까지 비는지는 첫 회차 뒤 향상된 모니터링(RDS 콘솔의 OS 지표)으로 손으로 봅니다. 안 비면 계획에 `"clear": "stop-start"`를 넣습니다. 정지·시작은 몇 분 더 걸립니다.
- AWS는 aws CLI로 부릅니다. EC2에서는 서버 역할의 권한을, 맥에서는 `--aws-profile`을 씁니다.

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

```bash
# 회차 이어 돌리기(부하·측정 EC2). 먼저 --yes 없이 할 일을 본다
DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.rounds plans/a1-ef.json --db-instance <실험 RDS 이름>
DATABASE_URL=postgresql://<실험 DB> nohup uv run python -m evals.searchlab.rounds plans/a1-ef.json --db-instance <실험 RDS 이름> --yes > a1.log 2>&1 &
```

벡터 파일은 저장소에 없으니, 부하·측정 EC2에서는 저장소를 받은 뒤(#556 「시험 환경」의 준비 명령) 두 번째 명령을 한 번 돌립니다. `DATABASE_URL`은 설정 검사 때문에만 필요합니다.

## 시험대(운영과 맞춰 보기)

실험 서버 숫자가 운영과 맞는지 같은 검색어·같은 방법으로 견줍니다(#556 「시험대 검증」). 두 쪽 모두 단계별 DB 쪽 값(`--server-stats`)으로 잽니다. 운영은 읽기 전용이고 통계를 비우지 않습니다.

```bash
# 실험 서버: 재부팅 → 데우기 → 처음 조회 → 반복 조회, 두 회차
DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.rounds plans/testbench.json --db-instance <실험 RDS 이름> --yes
# 그 뒤 재부팅 없이 탐색 폭 1000/400 반복 조회
DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.measure --label testbench/ef1000 --state repeat --ef-search 1000 --server-stats --vector-timeout 30
DATABASE_URL=postgresql://<실험 DB> uv run python -m evals.searchlab.measure --label testbench/ef400 --state repeat --ef-search 400 --server-stats --vector-timeout 30
```

```bash
# 운영(읽기 전용, 맥에서 터널): 데우기 없이 처음 조회. 순서는 실험 서버 첫 회차와 같게
DATABASE_URL=postgresql://<운영 DB> uv run python -m evals.searchlab.measure --label prod/first --state first --order-seed testbench-1 --server-stats --vector-timeout 30 --any-db
DATABASE_URL=postgresql://<운영 DB> uv run python -m evals.searchlab.measure --label prod/ef1000 --state repeat --ef-search 1000 --server-stats --vector-timeout 30 --any-db
DATABASE_URL=postgresql://<운영 DB> uv run python -m evals.searchlab.measure --label prod/ef400 --state repeat --ef-search 400 --server-stats --vector-timeout 30 --any-db
```

- 운영은 늘 데워진 채라 데우기를 하지 않습니다. 실험 서버의 데우기 500건이 운영의 이 상태를 흉내 냅니다.
- 운영 앱과 같은 계정으로 붙으면 그 시간의 앱 요청이 `server` 값에 섞일 수 있습니다. 회차 머리에 계정을 남기지는 않으니, 잰 날 `pg_stat_activity`로 앱 계정을 한 번 봐 둡니다.
