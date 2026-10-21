# ③ 대화로 좁혀 가기 평가

③ 챗봇이 대화를 이어 가며 사용자가 원하는 책을 함께 찾아 주는지 잽니다([#530](https://github.com/100-hours-a-week/KTB4-13th-AI/issues/530)). `evals/chat`의 질문 묶음은 정해진 말을 순서대로 보내고 턴마다 카드를 채점합니다. 이 평가는 챗봇의 답에 따라 다음 말이 바뀌는 대화 전체를 봅니다.

## 어떻게 재나

1. **가짜 사용자(LLM)** 가 `scenarios.json`의 첫마디("위로되는 책 추천해줘")로 시작합니다. 원하는 것(`want`)은 가짜 사용자만 압니다.
2. 챗봇이 물으면 그 물음에 맞는 것만 답하고, 카드가 안 맞으면 안 맞는 점을 하나만 말합니다. 원하는 것을 한꺼번에 털어놓지 않습니다.
3. 카드 중 원하는 것에 맞는 책이 있으면 골라 끝냅니다. 챗봇 응답은 5번까지 받습니다.
4. **채점 LLM**(가짜 사용자와 다른 모델)이 고른 책의 제목·소개를 보고 `must` 조건을 하나씩 판정합니다. 모두 맞아야 성공입니다.

요청은 BE 대화방 경로처럼 보냅니다(BE main 07b2bdd 기준). 최근 대화에 챗봇 답(reply + followup, 뒤쪽 200자)을 싣고, 쇼핑 칸(멱등 키, 마지막으로 카드가 보인 묶음, 가격)을 실어 추천·쇼핑 문지기도 켭니다.

`ask_about_card`가 있는 시나리오(2, 6번)는 맞아 보이는 카드가 처음 나오면 고르기 전에 그 카드에 대해 한 번 묻습니다. 질문에 새 카드를 내지 않고 답하는지 봅니다.

## 돌리는 법

`evals/chat`과 같은 평가 DB·서버를 씁니다(그쪽 README의 "읽을 때 주의"도 그대로 적용). 실행기는 모델 키(`EXTERNAL_AI_API_KEY`, `LLM_BASE_URL`)로 가짜 사용자와 채점 모델을 직접 부릅니다.

```bash
DATABASE_URL=postgresql://…/평가DB EXTERNAL_AI_API_KEY=… \
  uv run python -m evals.chat_dialog.run --base-url http://127.0.0.1:8001 --runs 3 \
  --note "main abc123" --out evals/chat/results/dialog-이름.json

uv run python -m evals.chat_dialog.grade evals/chat/results/dialog-이름.json
uv run python -m evals.chat_dialog.grade --compare 전.json 후.json
```

10개 × 3회면 대화 30개, 챗봇 요청 150번 이하입니다. 동시 4개로 5분 안팎입니다.

## 재는 것

| 지표 | 뜻 |
| --- | --- |
| `success_rate` | 5번 안에 고른 책이 `must`를 모두 맞음 |
| `turns_to_success_mean` | 성공한 대화에서 챗봇 응답을 몇 번 받았나 |
| `picked_but_wrong` | 가짜 사용자는 골랐는데 채점 LLM 이 조건이 안 맞다고 봄 |
| `convs_where_bot_asked` | 챗봇이 한 번이라도 물은 대화 |
| `repeated_questions` | 한 대화에서 같은 질문을 다시 물은 횟수 |
| `reply_lists_titles_pct` | 카드가 있는 턴에서 말풍선이 카드 제목을 다시 늘어놓은 비율 |
| `reply_ends_recommend_pct` | "추천합니다"류로 끝난 비율 |
| `card_questions_answered_without_new_cards` | 카드에 대한 질문에 새 카드 없이 답한 수 |
| `shop_route_turns` | 쇼핑(⑦)으로 간 턴 |

## 읽을 때 주의

- **가짜 사용자도 LLM 이라 사람과 다릅니다.** "원하는 것을 조금씩 말한다"는 규칙은 실제 챗봇 입력의 대부분이 짧은 주제 요청이라는 조사(2026-10-13 WildChat 분석)에 맞춘 가정입니다. 묻는 챗봇에 유리한 가정이라는 점을 같이 적습니다.
- **채점 LLM 의 판정은 사람이 확인하지 않았습니다.** 고른 책 몇 개는 사람이 원본을 열어 판정이 맞는지 봅니다.
- **시나리오 10개 × 3회는 작습니다.** 성공 수가 2~3개 차이면 오차 안으로 봅니다. 회차별 성공 수(`success_by_run`)의 범위로 봅니다.
