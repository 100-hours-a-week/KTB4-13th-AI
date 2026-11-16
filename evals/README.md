# LangSmith 태스크 평가

각 태스크가 쓰는 평가 사례와 채점 규칙을 재사용합니다. LangSmith에는 사례 번호, 수치, 적용한 프롬프트 버전이 올라갑니다. 챗봇·쇼핑 오프라인 실험에는 평가 사례의 질문과 받은 답, 실패 사유도 같이 올라가서 실험 비교 화면에서 프롬프트 버전별 답을 나란히 봅니다. 운영 요청은 기본으로 숫자만 보내고, `LANGSMITH_TRACE=true`이면 표본 요청을 원문 트레이스로 보냅니다([원문 트레이스와 LangSmith 규칙](#원문-트레이스와-langsmith-규칙)).

| 태스크 | 오프라인 | 온라인 |
| --- | --- | --- |
| `agent` 쇼핑 | 가짜 장바구니의 기대 결과와 답변 규칙 통과율 | 응답·근거 검사·쓰기 가드 통과율 |
| `chat` 도서 추천 챗봇 | 고정 대화의 추천·되묻기·조건·중복 규칙 통과율 | 축소 여부, 빈 답변, 중복 카드, 답변 필터로 확인한 통과율 |
| `extraction` 취향 추출 | 기대 취향, 금지 항목, 출력 형식 통과율 | 출력 형식과 스크리닝 탈락 후보로 확인한 통과율 |
| `search` 검색 | 제목 Hit@K, 저자 비율, 필터 통과율, 서술형 nDCG@10 | 축소 여부와 ISBN 중복으로 확인한 통과율 |
| `feed` 홈 피드 | 기대 관심사 노출률, 가진 책 제외율 | 축소 여부와 ISBN 중복으로 확인한 통과율 |
| `photo` 사진 책 찾기 | 표지·책장 파싱, 매칭, API 회귀 테스트 | 요청별 카탈로그 매칭률. 처리 오류는 0점 |
| `embedding` 임베딩 | 모델 게이트웨이와 API 회귀 테스트 | 요청한 개수와 벡터 차원이 일치한 비율 |
| `profile` 취향 프로필 | 계산, 저장, HTTP, Kafka 회귀 테스트 | HTTP·Kafka 계산 성공률 |

온라인 점수는 실제 응답에서 확인 가능한 규칙에 한정합니다. 오프라인에서 다른 기준을 쓰는 태스크는 지표별 평균과 표본 수를 비교합니다. 사진에서 읽어낸 항목이 0개면 건수만 기록합니다. 회귀 테스트의 건너뛴 사례는 점수에서 빠집니다.

## 오프라인 실행

```bash
uv run python -m evals.langsmith run agent evals/results/agent.json --label baseline -- --split heldout
uv run python -m evals.langsmith run chat evals/results/chat.json --label candidate -- --only 8,43
uv run python -m evals.langsmith run extraction evals/results/extraction.json -- --ids request-01
uv run python -m evals.langsmith run search evals/results/search.json
uv run python -m evals.langsmith run feed evals/results/feed.json
uv run python -m evals.langsmith run photo evals/results/photo.xml --local
```

`--` 뒤의 옵션은 기존 실행기에 전달합니다. 검색·피드는 평가 DB와 `CURSOR_SIGNING_KEY`가 필요합니다. 피드의 평가 DB에는 `book_classes`를 적재하고, 관심사 피드를 평가할 때는 `taste_profile.feed`가 포함된 스키마를 사용합니다. 챗봇은 평가 DB를 연결한 AI 서버를 먼저 띄우고, 실행기의 `DATABASE_URL`도 그 DB를 가리켜야 합니다. 사례·DB·모델 조건은 각 실행기 안내를 따릅니다. [챗봇](chat/README.md), [취향 추출](extraction/README.md), [검색](search/README.md), [홈 피드](feed/README.md), [쇼핑](agent/README.md)

`--local`이면 키 없이 점수를 콘솔에 냅니다. 기존 원본을 다시 올릴 때는 `run` 대신 `publish`를 사용합니다. 쇼핑의 별도 사례 묶음은 `--case-file`에 지정합니다. LangSmith의 **Datasets & Experiments**에서 같은 데이터셋의 실험과 지표를 비교합니다.

## 운영 점수와 프롬프트

```dotenv
LANGSMITH_API_KEY=
LANGSMITH_ENDPOINT=https://apac.api.smith.langchain.com
LANGSMITH_PROJECT=bookjeok-ai
LANGSMITH_PROMPTS={"agent":"bookjeok-agent","chat.spec":"bookjeok-chat-spec","chat.pick":"bookjeok-chat-pick","extraction":"bookjeok-extraction","photo.read":"bookjeok-photo-read"}
```

팀 계정은 APAC 리전이라 기본 주소가 `apac.api.smith.langchain.com`이고 웹 화면은 <https://apac.smith.langchain.com>입니다. 키도 APAC 계정에서 만듭니다. 다른 리전 계정의 키를 쓰면 응답은 정상이지만 점수가 올라가지 않고, LangSmith에서 고친 프롬프트 대신 코드의 프롬프트로 답합니다. 경고 로그에만 `403, 주소 …`가 남습니다.

프롬프트 하나를 처음 올릴 때 `uv run python -m evals.langsmith seed chat.pick`처럼 실행합니다. `seed --help`에서 연결할 수 있는 전체 프롬프트 이름을 확인합니다. LangSmith에서 내용을 고칠 때 코드와 같은 메시지 역할과 입력 변수를 유지합니다.

후보를 평가할 때는 평가 서버에 `LANGSMITH_EVAL_MODE=true`를 설정하고, 매핑 값을 `bookjeok-chat-pick:<커밋 해시>`처럼 지정합니다. 서버가 해당 프롬프트를 처음 읽은 뒤 평가 내내 고정하며, 조회가 실패하면 평가에서 오류로 드러납니다. 챗봇과 취향 추출은 실행 원본에 실제 사용한 커밋을 기록합니다. 쇼핑 실행기는 `--prompt-ref`로 평가할 태그나 해시를 고릅니다. [쇼핑 프롬프트 평가](agent/README.md#langsmith에서-점수-비교와-프롬프트-반영)

운영 매핑에 프롬프트 이름만 지정하면 `production`을 읽습니다. 후보 커밋을 **Production**으로 승격하면 기본 5초 갱신 주기 이후 요청에 반영됩니다. 운영 점수는 **Tracing Projects**에서 `task`와 `prompt_versions` 메타데이터로 조회할 수 있습니다. [LangSmith 프롬프트 버전 관리](https://docs.langchain.com/langsmith/manage-prompts)

LangSmith 프롬프트를 고쳐 Production으로 올렸으면 같은 글을 코드 프롬프트에도 PR로 옮겨 둡니다. LangSmith를 읽지 못하면(키·리전 오류, 장애) 서버는 코드 프롬프트로 답하기 때문에, 둘이 갈라지면 조용히 옛 프롬프트로 돌아갑니다.

## 원문 트레이스와 LangSmith 규칙

```dotenv
LANGSMITH_TRACE=true
LANGSMITH_SAMPLE_RATE=0.2
```

켜면 표본으로 뽑힌 챗봇·쇼핑·취향 추출·사진 요청 하나가 트레이스 하나로 갑니다. 검색·피드·임베딩·프로필은 LLM이 없고 요청이 많아 계속 숫자만 보냅니다.

- 루트: 요청 본문(사용자 번호 `user_id`와 사진 주소의 서명은 뺌), 응답 본문, 메타데이터 `task`·`prompt_versions`·`model`·`release_sha`·`request_id`·`online_eval`(코드 규칙 채점 숫자)
- 자식: LLM 호출마다 프롬프트 이름으로 찍힙니다(`chat.spec`·`chat.relation`·`chat.scope`·`chat.recall`·`chat.pick`·`chat.cards`·`chat.lane`·`chat.answer`·`agent.graph`·`agent.compare`·`agent.reviews`·`extraction`·`photo.read`). 검색·DB 단계는 따로 찍히지 않습니다.
- 점수: 코드 규칙 점수 `<태스크>_online_score`가 그 루트에 붙습니다. 끄면 예전처럼 입력이 빈 실행을 따로 만들어 붙입니다.
- `request_id`는 BE·AI 로그를 잇는 `X-Request-Id`입니다. 낮은 점수 턴을 CloudWatch 로그에서 찾을 때 씁니다.

코드 규칙 점수는 응답 형식과 장애만 봅니다(엉뚱한 책을 내도 100점). 추천이 맞는지는 LangSmith 화면의 프로젝트 **규칙(Rules)**으로 붙입니다. 코드는 바꾸지 않습니다.

1. **판정 LLM 채점**: 원본은 [langsmith_judges/chat_card_fit.md](langsmith_judges/chat_card_fit.md)입니다. 카드가 있는 ③ 턴은 트레이스 출력에 `judge_books`(카드 책의 제목·저자·소개 앞 300자)가 실리고 꼬리표 `has_cards`가 붙습니다. 규칙은 이 꼬리표로 턴을 고르고, 판정 LLM은 요청과 `judge_books`만 읽어 0·1·2(검색 평가의 점수 기준)를 매깁니다. 추천 이유 문장은 우리 시스템이 쓴 글이라 판정에 넣지 않습니다. 판정 모델 키와 모델 설정은 LangSmith 작업 공간에 등록합니다. 판정 점수를 믿으려면 사람이 매긴 0·1·2와 몇 % 맞는지 먼저 잽니다.
2. **실패 모으기**: 필터 `chat_online_score = 0`(또는 판정 점수가 낮은 턴)에 검토 대기열(annotation queue)이나 데이터셋에 넣기를 겁니다. 사람이 대기열에서 턴을 보고 기대 동작을 적은 뒤 `evals/chat/cases.json`에 사례로 옮깁니다. 오프라인 실행기는 이 파일을 읽으므로 LangSmith 데이터셋에 쌓인 것만으로는 다음 실험에 들어가지 않습니다.

무료 요금제는 1석(팀원이 같이 못 봄)·월 5천 트레이스·14일 보관입니다.
