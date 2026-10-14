# ③ 챗봇 평가용 질문 묶음

`POST /recommendations/chat`이 추천을 제대로 하는지 재는 데 쓰는 고정 질문 묶음입니다. ③의 프롬프트나 모델을 바꾸는 PR은 이 묶음으로 잰 결과를 "고정 Dataset 검증 결과" 칸에 적습니다. 기준은 [#322](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/322), 질문의 출처는 [#385](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/385), 전체 방향은 [#384](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/384)에 있습니다.

추천에는 정답 책이 하나로 정해지지 않아서, 정답 대신 **지켜야 할 기준**을 통과/실패로 채점합니다. 지금 채점기는 코드로 볼 수 있는 것만 봅니다. LLM 채점은 아직 없습니다.

## 돌리는 법

실제 모델을 부르므로 비용이 듭니다(103개 × 5번이면 요청 600번 남짓, 동시 4개로 20분 안팎). CI에서는 돌리지 않습니다.

필요한 것은 셋입니다.

- **평가 전용 DB.** 운영 AI DB의 책 표 4개를 복사해 고정한 로컬 DB입니다([#322 댓글](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/322)). 책 데이터가 바뀌면 전후를 견줄 수 없어서 하나로 고정합니다.
- **그 DB를 쓰는 서버.** 평가에 쓸 모델로 띄웁니다. 환경변수는 `.env.example`을 따릅니다(`DATABASE_URL`, `LLM_BASE_URL`, `LLM_MODEL_ID`, `EXTERNAL_AI_API_KEY`).
- **모델 키.**

```bash
# 1) 서버. 로그를 파일로 남깁니다(벡터 검색이 끊겼는지 세는 데 씁니다).
#    LLM_PROVIDER 를 openrouter 가 아닌 값으로 두어 ZDR 을 끕니다(아래 "읽을 때 주의")
LLM_PROVIDER=eval-no-zdr DATABASE_URL=postgresql://…/평가DB \
  uv run uvicorn app.main:app --port 8001 2>&1 | tee /tmp/chat-eval-server.log

# 2) 돌리기. 원본은 evals/chat/results/ 에 저장합니다(올리지 않습니다)
DATABASE_URL=postgresql://…/평가DB uv run python -m evals.chat.run \
  --runs 5 --model openai/gpt-4o-mini --server-log /tmp/chat-eval-server.log \
  --out evals/chat/results/이름-원본.json

# 일부만(번호로)
… --only 8,43 --runs 3

# 3) 채점. 원본을 다시 채점하는 것은 LLM을 부르지 않으므로 돈이 들지 않습니다
uv run python -m evals.chat.grade evals/chat/results/이름-원본.json \
  --save evals/chat/results/이름.json
uv run python -m evals.chat.grade 원본.json --skip-runs 4,5     # 버릴 회차

# 전후 비교
uv run python -m evals.chat.grade --compare 전.json 후.json
```

**재는 것:** 종류별·출처별 통과율, 실패 사유별 수, 회차별 통과한 질문 수, 카드 0장 비율, 말하지 않은 분위기가 조건 문장에 붙은 비율, 응답 시간(중앙, p90), 가장 자주 보인 책.

## 읽을 때 주의

- **코드로 볼 수 있는 것만 채점합니다.** 카드가 나왔는지, 조건을 지켰는지, 겹치지 않는지입니다. 추천한 책이 질문에 맞는지, 이유가 책소개에 근거하는지는 보지 않습니다(`expect.ungraded`에 적힌 21개). 그래서 통과율은 실제 품질보다 높게 나옵니다.
- **회차 사이 차이가 1~2문항이면 오차 안입니다.** 같은 질문도 돌릴 때마다 결과가 갈립니다. 5번 돌려 범위로 봅니다. 이보다 작은 변화는 개선으로 보지 않습니다.
- **벡터 검색이 끊긴 실행은 버립니다.** PC 부하가 높으면 벡터 검색이 제한 시간에 걸려 키워드로만 답합니다. `--server-log`를 주면 끊긴 횟수를 세고, 0이 아니면 그 실행은 코드 탓인지 환경 탓인지 가를 수 없습니다.
- **평가 서버는 ZDR 을 끄고 돌립니다.** 게이트웨이는 `LLM_PROVIDER=openrouter`일 때 요청에 `provider.zdr`을 실어 ZDR 엔드포인트로만 라우팅하는데, 그러면 `gpt-4o-mini`·`gpt-4o` 모두 Azure 한 곳만 남습니다. 2026-10-14에 이 경로로 기준선을 돌리자 LLM 호출의 7%가 408(제공자 시간 초과)로 실패해 회차마다 13~19%의 턴이 `degraded`가 됐습니다. ZDR 을 끄면 같은 호출이 24번 중 0번 실패했습니다. 평가 질문은 개인정보가 아니라서 ZDR 이 필요 없습니다. `LLM_PROVIDER`를 다른 값으로 두면 앱 코드를 바꾸지 않고 ZDR 만 꺼집니다.
- **`degraded`가 0이 아닌 회차는 버립니다.** 규칙 기반 카드가 섞여 LLM 이 한 일을 잴 수 없습니다. 요약의 `degraded_turns_per_run`을 봅니다.
- **같은 질문으로 고치고 같은 질문으로 재면 낙관적인 숫자가 됩니다.** 고칠 때는 어느 질문을 보고 고쳤는지 PR에 적습니다.
- **평가 모델과 운영 모델이 다를 수 있습니다.** 지금까지는 `openai/gpt-4o-mini`로 쟀고 운영은 `openai/gpt-4o`입니다. PR의 "Model ID" 칸에 어느 모델로 쟀는지 적습니다.
- **출처별로 따로 봅니다.** `bug` 질문은 우리가 고친 버그를 다시 일으키는 것이라 실사용 품질처럼 읽으면 안 됩니다. 43개(`research`)는 논문의 유형에 맞춰 직접 쓴 문장이라 문장 자체의 근거는 없습니다.

## 무엇을 통과로 보나

기준 5개 중 코드로 보는 것이 이 묶음의 채점입니다.

| # | 기준 | 채점 |
| --- | --- | --- |
| 1 | 추천할 질문에 추천하고, 모호한 질문에 되묻는다 | 코드 (`expect.action`) |
| 2 | 요청한 분야·가격·연도를 지킨다 | 코드 (`expect.cards`, `expect.spec`) |
| 3 | 같은 책이 겹치지 않고, 이미 보여 준 책이 또 나오지 않는다 | 코드 (`distinct_titles`, `not_shown_before`) |
| 4 | 추천 이유가 책소개에 있는 내용이다 | 아직 없음 (LLM 채점 예정) |
| 5 | 질문에 맞는 책이다 | 허용 책을 정한 질문만 코드(`any_isbn`), 나머지는 아직 없음 |

## 파일

| 파일 | 뜻 |
| --- | --- |
| `cases.json` | 질문 묶음 |
| `run.py` | 떠 있는 서버에 질문을 보내 응답 원본을 저장합니다. 화면(FE)과 같은 방식으로 직전 응답의 `spec`을 되돌려 보내고, 최근 대화와 이미 보여 준 책을 쌓아 보냅니다. 개인화는 넣지 않습니다(이력이 없는 사용자) |
| `grade.py` | 원본을 `cases.json`의 기대 동작으로 채점하고 요약·비교를 냅니다. DB·LLM 없이 돕니다 |
| `baselines/` | 고치기 전후에 잰 요약(아래) |

## cases.json

| 칸 | 뜻 |
| --- | --- |
| `version` | 묶음의 버전. 질문이나 기대 동작을 고치면 올리고 결과에 같이 적습니다 |
| `kinds`, `sources` | 종류와 출처의 설명 |
| `cases[].no` | 질문 번호. 1부터 빠짐없이 이어집니다(이슈와 PR이 번호로 가리킵니다) |
| `cases[].kind` | 종류(아래 표) |
| `cases[].source` | 출처(아래 표). `community`, `chatlog`, `study`는 무엇을 본떴는지 `evidence`에 적습니다 |
| `cases[].about` | 이 질문으로 보려는 것 한 줄 |
| `cases[].turns` | 사용자 말. 여러 개면 여러 턴입니다 |
| `expect.action` | `recommend`(카드가 있어야 함), `clarify`(카드 없이 되물어야 함), `recommend_and_ask`, `observe`(기대 동작을 정하지 않아 응답만 남기고 통과율에서 뺌) |
| `expect.intent` | 첫 턴의 의도(`exact`/`semantic`) |
| `expect.cards` | 마지막 턴의 카드 조건. 아래 표 |
| `expect.reply` | 말풍선·되묻기 문장에 `has`(있어야 하는 말), `lacks`(없어야 하는 말) |
| `expect.spec[]` | 턴별 조건. `{turn, path, equals}` |
| `expect.semantic` | 조건 문장(`spec.semantic`)이 `changes_at`턴에 바뀌었는지, `has`/`lacks` 낱말 |
| `expect.ungraded[]` | 코드로 볼 수 없어 채점하지 않는 기대. LLM 채점이 생기면 여기서 옮깁니다 |

`expect.cards`의 칸:

| 칸 | 뜻 |
| --- | --- |
| `any_isbn` | 카드 중 하나는 이 ISBN이어야 함(허용 책) |
| `title_all`, `title_any`, `author_all`, `publisher_all` | 카드 제목·저자·출판사가 이 말을 포함해야 함 |
| `title_none`, `title_none_exact`, `author_none` | 이 책·저자가 나오면 실패(기준 책이 다시 나오는지) |
| `distinct_titles` | 같은 제목이 겹치면 실패 |
| `max_cards`, `price_max`, `year_from` | 권수·가격·출간연도 한도 |
| `not_shown_before`, `only_shown_before` | 앞 턴에 보여 준 책이 다시 나오면 실패 / 보여 준 책 밖에서 고르면 실패 |
| `reason_long_needs_description` | 책소개가 없는 책에 긴 이유가 있으면 실패 |

## 종류와 출처

103개, 125턴입니다.

| 종류 | 개수 | 뜻 |
| --- | --- | --- |
| `situation` | 27 | 주제와 자기 상황을 말한다 |
| `multi` | 19 | 여러 턴에 걸쳐 조건을 보태거나 바꾼다 |
| `condition` | 16 | 가격, 출간연도, 분량, 빼 달라는 것 같은 조건을 붙인다 |
| `like` | 13 | 읽은 책이나 작가를 들어 비슷한 책을 찾는다 |
| `topic` | 11 | 주제만 짧게 말한다 |
| `edge` | 9 | 되물어야 하는 말, 추천이 아닌 말, 거친 말투 |
| `lookup` | 8 | 제목, 저자, 출판사로 책을 찾는다 |

| 출처 | 개수 | 뜻 |
| --- | --- | --- |
| `research` | 43 | 조사한 유형에 맞춰 직접 쓴 질문 |
| `chatlog` | 31 | 실제 챗봇 기록(WildChat-1M 책 추천 요청 201건)을 본떠 주제만 바꾼 질문 |
| `bug` | 16 | 고친 버그나 운영에서 직접 넣어 본 문장을 다시 일으키는 질문 |
| `community` | 12 | 국내 커뮤니티의 실제 추천 요청 글을 본떠 다시 쓴 질문 |
| `study` | 1 | LLM 영화 추천 챗봇 실사용 연구에서 사용자가 한 행동 |

## 기준선 (`baselines/`)

10/12에 [#359](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/359)을 고치면서 잰 요약입니다. 모델은 모두 `openai/gpt-4o-mini`, 5회(기준선만 9회)입니다. **질문 60개(묶음 v2)로 잰 값이라, 지금 묶음(v3, 103개)의 숫자와 직접 견줄 수 없습니다.** 지금 묶음의 기준선은 이 PR 뒤에 새로 잽니다.

| 파일 | 코드 | 통과율 | 카드 0장 |
| --- | --- | --- | --- |
| `main-403aca1` | 고치기 전 | 76.7% | 45/504 (8.9%) |
| `exp-rules-only` | 규칙만 | 84.3% | 6/280 |
| `exp-prompt-only` | 프롬프트만 | 82.3% | 13/280 |
| `exp-rules-and-prompt` | 규칙 + 프롬프트 전부 | 82.3% | 4/280 |
| `pr361` | 규칙 + 예시 한 줄(채택) | 83.7% | 4/280 (1.4%) |
| `exp-card-example` | 채택안 + 카드 예시 낱말 뺌 | 81.3% | 6/280 |
| `exp-first-turn-tone` | 채택안 + 첫 질문에 톤 지시문 뺌 | 83.7% | 13/280 |
