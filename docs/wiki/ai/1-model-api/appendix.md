---
wiki: AI-1 모델 API 명세 부록
type: appendix
group: ai-1
owner: 미정
status: 작성중
updated: 2026-09-18
limit_exempt: 원본 4장의 호출 예시 9개 전체를 보존 (변환 문서)
---
**요약** 원본 4장의 API 호출 예시와 예시 응답 9개다.

## 4. API 호출 예시와 예시 응답

서버 구현 전이라 응답은 **명세 기준 예상 응답**이다. 구현 직후 고정 질문 30개 × 3회로 실측해 실제 응답으로 교체한다.

### 예시 1: AI 검색 `POST /search`

```bash
curl -X POST <https://ai.internal.bookjeok.com/search> \
  -H "Authorization: Bearer$SERVICE_TOKEN" -H "Content-Type: application/json" \
  -H "X-Request-Id: req_20260907_0001" \
  -d '{"query":"쓸쓸하고 담담한 위로","filters":{"category":"에세이"},"sort":"relevance","size":15}'
```

```json
{
  "message": "search_success",
  "data": {
    "results": [
      {
        "book_id": 2091,
        "title": "슬픔을 아는 사람",
        "author": "유진목",
        "publisher": "문학동네",
        "price": 14400,
        "in_stock": true,
        "cover_url": "https://…/2091.jpg"
      }
    ],
    "next_cursor": "eyJ…(서명됨)",
    "fallback": null
  }
}
```

### 예시 2: 텍스트 임베딩 `POST /embeddings`

```bash
curl -X POST <https://ai.internal.bookjeok.com/embeddings> \
  -H "Authorization: Bearer$SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"texts":["쓸쓸하고 담담한 위로"],"purpose":"query"}'
```

```json
{
  "message": "embed_success",
  "data": {
    "vectors": [[0.0123, -0.0456, "…"]],
    "dim": 1024,
    "model": "bge-m3-2026q3"
  }
}
```

### 예시 3: 챗봇 도서 추천 `POST /recommendations/chat`

```bash
curl -X POST <https://ai.internal.bookjeok.com/recommendations/chat> \
  -H "Authorization: Bearer$SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id":123,"consented":true,"spec":{"intent":"semantic","exact":{"title":null,"author":null,"publisher":null},"filters":{},"semantic":"퇴근길에 읽을 짧은 소설","anchor_book":null,"exclude":[]},"message":"유머 있으면 좋겠어","recent_turns":[],"exclude_book_ids":[],"image_ref":null}'
```

```json
{
  "message": "recommend_success",
  "data": {
    "reply": "퇴근길에 가볍게 읽히는 쪽으로 골라봤어요.",
    "spec": {
      "intent": "semantic",
      "exact": { "title": null, "author": null, "publisher": null },
      "filters": { "in_stock_only": true },
      "semantic": "퇴근길에 읽을 짧고 유머 있는 소설",
      "anchor_book": null,
      "exclude": []
    },
    "recognition": null,
    "cards": [
      {
        "book_id": 1088,
        "rank": 1,
        "match_score": 87,
        "title": "…",
        "author": "…",
        "price": 12420,
        "cover_url": "https://…/1088.jpg",
        "reason_short": "짧은 호흡에 유머가 섞인 연작이라 퇴근길 한 편씩 읽기 좋아요.",
        "reason_long": "퇴근길에 읽을 짧은 소설을 찾으셨고, 유머가 있으면 좋겠다고 하셨죠. 한 편이 지하철 몇 정거장이면 끝나는 연작이라 끊어 읽기 좋습니다. 웃기려 애쓰지 않는 건조한 유머라 피곤한 저녁에도 부담이 없어요.",
        "match_basis": [
          { "label": "분위기", "detail": "유머" },
          { "label": "분량", "detail": "짧은 호흡" }
        ]
      }
    ],
    "followup": null,
    "buttons": [],
    "degraded": false
  }
}
```

### 예시 4: 표지 사진 인식 (V2). `POST /recommendations/chat` 이미지 턴

```bash
curl -X POST <https://ai.internal.bookjeok.com/recommendations/chat> \
  -H "Authorization: Bearer$SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id":123,"consented":true,"spec":{"intent":"semantic","exact":{"title":null,"author":null,"publisher":null},"filters":{},"semantic":null,"anchor_book":null,"exclude":[]},"recent_turns":[],"exclude_book_ids":[],"image_ref":"<https://s3>.…/up_20260903_abc?X-Amz-Signature=…"}'
```

응답. 한 권으로 특정한 경우:

```json
{
  "message": "recommend_success",
  "data": {
    "reply": "이 책은 《달러구트 꿈 백화점》(이미예)예요. 잠든 사이 꿈을 사고파는 상점 이야기예요. 비슷한 결의 책도 함께 골라봤어요.",
    "spec": {
      "intent": "semantic",
      "exact": { "title": null, "author": null, "publisher": null },
      "filters": {},
      "semantic": null,
      "anchor_book": 1088,
      "exclude": []
    },
    "recognition": { "recognized": true, "book_id": 1088, "candidates": [] },
    "cards": [
      {
        "book_id": 4021,
        "rank": 1,
        "match_score": 79,
        "title": "…",
        "author": "…",
        "price": 13800,
        "cover_url": "https://…/4021.jpg",
        "reason_short": "같은 결의 따뜻한 판타지예요.",
        "reason_long": "찍어 보내신 《달러구트 꿈 백화점》과 같은 결의 따뜻한 판타지예요. 일상에 작은 환상을 하나 얹는 방식이 닮았고, 분량과 호흡도 비슷해서 이어 읽기 좋습니다.",
        "match_basis": [{ "label": "분위기", "detail": "따뜻함" }]
      }
    ],
    "followup": null,
    "buttons": [
      { "label": "네", "action": "library_add", "book_id": 1088 },
      { "label": "아니요", "action": "dismiss" }
    ],
    "degraded": false
  }
}
```

응답. 후보가 여럿이라 확정하지 못한 경우(`spec.anchor_book`은 업데이트하지 않는다):

```json
{
  "message": "recommend_success",
  "data": {
    "reply": "이 책이 맞을까요? 비슷한 표지가 몇 권 있어요.",
    "spec": {
      "intent": "semantic",
      "exact": { "title": null, "author": null, "publisher": null },
      "filters": {},
      "semantic": null,
      "anchor_book": null,
      "exclude": []
    },
    "recognition": {
      "recognized": false,
      "book_id": null,
      "candidates": [
        {
          "book_id": 1088,
          "title": "달러구트 꿈 백화점",
          "author": "이미예",
          "cover_url": "https://…/1088.jpg",
          "confidence": 0.62
        },
        {
          "book_id": 4021,
          "title": "…",
          "author": "…",
          "cover_url": "…",
          "confidence": 0.55
        }
      ]
    },
    "cards": [],
    "followup": null,
    "buttons": [
      {
        "label": "달러구트 꿈 백화점",
        "action": "confirm_book",
        "book_id": 1088
      },
      { "label": "다시 찍기", "action": "retake" }
    ],
    "degraded": false
  }
}
```

### 예시 5: 개인화 추천 피드 `GET /recommendations/feed`

```bash
curl -G <https://ai.internal.bookjeok.com/recommendations/feed> \
  -H "Authorization: Bearer$SERVICE_TOKEN" \
  --data-urlencode "user_id=123" --data-urlencode "surface=home" \
  --data-urlencode "sort=match" --data-urlencode "size=15"
```

```json
{
  "message": "feed_success",
  "data": {
    "items": [
      {
        "book_id": 3310,
        "title": "…",
        "author": "…",
        "price": 9900,
        "cover_url": "…",
        "in_stock": true,
        "match_score": 84
      }
    ],
    "next_cursor": "eyJ…(서명됨)",
    "cold_start": false
  }
}
```

### 예시 6: 취향 기억 추출 (V2) `POST /preferences/extractions`

```bash
curl -X POST <https://ai.internal.bookjeok.com/preferences/extractions> \
  -H "Authorization: Bearer$SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id":123,"consented":true,"conversation":[{"role":"user","text":"비 오는 날 읽을 책 추천해줘"},{"role":"assistant","text":"잔잔한 소설 위주로 골라봤어요."},{"role":"user","text":"이별 후에 위로가 될 만한 걸로"}],"conversation_id":"cv_20260903_a1","existing_preferences":[{"type":"mood","value":"잔잔한 에세이를 선호함"}]}'
```

```json
{
  "message": "extract_success",
  "data": {
    "extractions": [
      {
        "type": "mood",
        "value": "이별 후 위로되는 잔잔한 소설을 찾음",
        "confidence": 0.86,
        "vector": [0.01, -0.04, "…"],
        "dim": 1024,
        "source_conversation_id": "cv_20260903_a1"
      }
    ],
    "nothing_found": false
  }
}
```

### 예시 7: 취향 프로필 생성 및 재계산 `POST /preferences/profile`

온보딩 완료. 응답만으로 프로필을 만들고 200 동기로 즉시 끝난다. 작가 취향은 `memories`(`type: author`)로만 들어온다. **구매, 도서관, 리뷰는 본문에 없다** — 서버가 `user_id`로 복제 테이블에서 읽는다. 가입 직후라 읽어도 비어 있다.

```bash
curl -X POST <https://ai.internal.bookjeok.com/preferences/profile> \
  -H "Authorization: Bearer$SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id":123,"idempotency_key":"prof_20260904_a1b2","onboarding":{"reading_times":["밤"],"criteria":["베스트셀러"],"categories":["에세이","한국소설"],"tags":["힐링","성장"],"liked_book_ids":[1088,3310]},"memories":[{"type":"author","value":"김영하의 문장을 좋아함","vector":[0.02,-0.01,"…"],"dim":1024}]}'
```

```json
{
  "message": "profile_success",
  "data": { "cold_start": false, "profile_version": 1 }
}
```

한 달 뒤. 취향 기억이 늘어 다시 부른다. 본문은 여전히 온보딩과 기억뿐이고, 그 사이의 구매·리뷰는 서버가 복제 테이블에서 읽어 centroid에 넣는다.

```bash
curl -X POST <https://ai.internal.bookjeok.com/preferences/profile> \
  -H "Authorization: Bearer$SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id":123,"idempotency_key":"prof_20261004_c3d4","onboarding":{"reading_times":["밤"],"criteria":["베스트셀러"],"categories":["에세이","한국소설"],"tags":["힐링","성장"],"liked_book_ids":[1088,3310]},"memories":[{"type":"author","value":"김영하의 문장을 좋아함","vector":[0.02,-0.01,"…"],"dim":1024},{"type":"mood","value":"이별 후 위로되는 잔잔한 소설을 찾음","vector":[0.01,-0.04,"…"],"dim":1024}]}'
```

```json
{
  "message": "profile_success",
  "data": { "cold_start": false, "profile_version": 4 }
}
```

이 사용자가 그 사이 2077을 사고 1502에 별점 2점을 줬다면, 2077은 가중치 3으로 centroid에 들어가고 1502는 centroid에서 빠진 채 추천에서도 제외된다. **호출자는 그 사실을 몰라도 된다.** 이 호출 이후에 생긴 구매·리뷰는 프로필을 다시 만들지 않아도 ③, ④가 채점할 때 반영한다.

### 예시 8: 쇼핑 에이전트 턴 (V2) `POST /agent/act`

```bash
curl -X POST <https://ai.internal.bookjeok.com/agent/act> \
  -H "Authorization: Bearer$SERVICE_TOKEN" -H "Content-Type: application/json" \
  -d '{"user_id":123,"conversation_id":"cv_20260904_x1","message":"이거 장바구니에 담아줘","context_cards":[{"book_id":1088,"rank":1,"price":13500,"match_score":92,"in_stock":true},{"book_id":4021,"rank":2,"price":14000,"match_score":87,"in_stock":true}],"focused_book_id":1088,"user_context":{"logged_in":true,"has_default_address":true},"allow_tools":null,"idempotency_key":"idem_a1b2c3"}'
```

```json
{
  "message": "agent_success",
  "data": {
    "reply": "담았어요. 장바구니에서 확인해 보세요.",
    "tool_calls": [
      {
        "name": "cart.add",
        "arguments": { "book_id": 1088, "qty": 1 },
        "result": { "cart_count": 3 },
        "grounded": true
      }
    ],
    "resolved_reference": {
      "ref": "이거",
      "book_ids": [1088],
      "confidence": 0.96
    },
    "selection": null,
    "buttons": [
      { "label": "장바구니 보기", "action": "navigate", "target": "CART-001" }
    ]
  }
}
```

### 예시 9: 서버 상태 점검 `GET /health`

```bash
curl <https://ai.internal.bookjeok.com/health>
```

```json
{
  "status": "ok",
  "version": "a1b2c3d",
  "replication_lag_seconds": 2,
  "components": {
    "gateway": "ok",
    "database": "ok",
    "vector_index": "ok",
    "llm": "ok",
    "embedding": "ok"
  }
}
```
