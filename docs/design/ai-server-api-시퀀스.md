# 북적북적 AI 서버 — 데이터 소유권과 호출 흐름 (API 명세 버전)

> [ai-server-api-spec-final.md](ai-server-api-spec-final.md)의 §1·§2·§3을 실제 필드명 기준 시퀀스로 옮긴 것이다. **새 규칙은 없다.** 계약면의 단일 소스는 명세 문서이고, 여기와 어긋나면 명세가 맞다.
>
> 쉬운 말 버전은 [ai-server-api-다이어그램.md](ai-server-api-다이어그램.md).

## DB 배치 (2026-09-09 확정)

- BE 원본 = **MySQL**. AI 저장소 = **pgvector를 얹은 PostgreSQL(AI 전용 인스턴스)**.
- 커머스 데이터는 **BE MySQL → AI PostgreSQL 단방향 복제**. `v_*`는 SQL view가 아니라 AI Postgres 안의 실제 복제 테이블(이름 접두사일 뿐). AI 서버엔 BE MySQL 연결이 없다.
- 역방향 복제 없음. AI→BE는 응답 본문(⑤ `extractions[]`·③ `reason_long`)과 V2 tool 호출뿐 — 둘 다 복제가 아니라 BE가 검증 후 자기 테이블에 쓰는 것.

**허용 지연(신선도 예산) — 계약**

| 대상 | 허용 지연 |
| --- | --- |
| v_books 가격·재고 | 5분 |
| v_books 신간 입고(임베딩 포함) | 1시간 |
| v_book_popularity | 24시간 |
| 이력 3종(구매·도서관·리뷰) | 5분 |

- 복제 지연·행 부재는 **오류가 아니다** — 낡은 값 그대로 200, `X-Degraded` 안 붙음. 행이 없으면 그 항 0점 + 200.
- **AI Postgres 자체가 응답 불능이면 전면 500**(부분 축소 없음). BE MySQL 장애는 500이 아니다(복제만 멈추고 사본으로 계속).
- 인기 신호는 **판매 + 리뷰 둘뿐**. 조회 수·BE 랭킹은 복제받지 않는다.

---

## 0. 데이터 소유권 — 누가 뭘 읽고 쓰나

```mermaid
flowchart LR
    subgraph BE["백엔드 소유 · 원본 (MySQL)"]
        O1[(회원 · 온보딩 답변)]
        O2[(카탈로그 books)]
        O3[(구매 · 도서관 · 리뷰)]
        O5[(취향 기억 preference_memory)]
        O6[(인기 집계 · 주기 갱신)]
        O7[(대화 스레드 · V2)]
    end
    subgraph RT["복제 테이블 · AI Postgres 안 · 계약면"]
        W1[v_books]
        W2[v_user_purchases]
        W3[v_user_library]
        W4[v_user_reviews]
        W5["v_book_popularity<br/>sales · rating_avg · rating_count · as_of"]
    end
    subgraph AI["AI 서버 소유 · pgvector Postgres (AI 전용)"]
        A1[("book_embeddings — 도서 문서벡터")]
        A2[("taste_profile — centroid · tag_weights · computed_at")]
        A3["멱등키 기록 24h · 사전 임베딩 자산(작가·카테고리·태그)"]
    end
    O2 ==>|단방향 복제| W1
    O3 ==>|단방향 복제| W2
    O3 ==>|단방향 복제| W3
    O3 ==>|단방향 복제| W4
    O6 ==>|단방향 복제| W5
    RT -. "AI가 자기 DB 사본을 SELECT" .-> AI
    O1 -. "요청 본문으로 전달 (⑥)" .-> AI
    O5 -. "요청 본문으로 전달 (⑤·⑥)" .-> AI
    O7 -. "요청 본문으로 전달 (③·⑤)" .-> AI
    AI ==>|쓰기| A1
    AI ==>|쓰기| A2
    A1 -. "응답 본문 · tool" .-> BE
    A2 -. "응답 본문 · tool" .-> BE
```

**AI가 데이터를 얻는 경로는 둘뿐이다.**

| 경로 | 무엇 | 어떻게 |
| --- | --- | --- |
| 복제 사본 조회 (AI가 자기 DB에서 SELECT) | `v_books`, `v_user_purchases`, `v_user_library`, `v_user_reviews`, `v_book_popularity` | `user_id`·`book_id`로 SELECT. **행 없으면** 그 항 0점 + 200 / **복제 지연**이면 낡은 값 + 200 / **AI Postgres 다운**이면 전면 500 |
| 요청 본문 (BE가 실어 보냄) | 온보딩 답변·`memories`(⑥), 대화(`recent_turns` ③ / `conversation` ⑤), `spec`(③), `context_cards`·`user_context`(⑦), `image_ref`(③ V2) | BE가 원본을 갖고 있으므로 필요한 만큼만 담아 보냄 |

**AI가 쓰는 것 (전부 AI Postgres).**

| 저장소 | 무엇 | 언제 |
| --- | --- | --- |
| `book_embeddings` | 도서 소개 문서벡터 (벡터 인덱스에 적재) | 초기 적재 배치가 ②로 만들어 저장 (복제 완료 후) |
| `taste_profile` | `centroid` · `tag_weights` · `cold_start` · `computed_at` · `profile_version` (유저당 1행 upsert) | ⑥ 호출 시 |
| 멱등키 기록 (24h) | `idempotency_key` → 저장 결과 매핑 | ⑥·⑦ |
| 사전 임베딩 자산 | 작가 대표벡터, 카테고리·태그 벡터 | 미리 만들어 둠(정적) |

---

## ① `POST /search`

```mermaid
sequenceDiagram
    participant BE as 백엔드
    participant S as AI · /search
    participant EMB as 임베딩
    participant KW as 키워드 인덱스 (v_books 사본)
    participant VEC as 벡터 인덱스 (book_embeddings)

    BE->>S: {query, filters, sort, cursor, size}
    S->>EMB: 검색어 임베딩 (purpose=query)
    EMB-->>S: query 벡터
    par 키워드 순위
        S->>KW: 단어 매칭 + filters
        KW-->>S: 후보 A
    and 벡터 순위
        S->>VEC: ANN + filters
        VEC-->>S: 후보 B
    end
    S->>S: RRF 병합 → sort 적용 → next_cursor 서명
    S-->>BE: 200 {results[], next_cursor, fallback}

    Note over S: 결과 0건 → fallback.message 채움 (전환은 BE가 수행)
    Note over S: 벡터 인덱스 down / 임베딩 업스트림 장애 / 초기 적재 중 → 키워드만 + X-Degraded: keyword-only
    Note over S: 키워드·벡터 둘 다 down (AI Postgres 장애 등) → 500. BE MySQL 장애는 아님
    Note over S: 복제 지연으로 값이 낡아도 → 200, X-Degraded 없음
```

- **읽기**: `v_books`(복제 사본), `book_embeddings`, (`sort=popular`) `v_book_popularity` — 판매·리뷰 집계
- **쓰기**: 없음 (커서는 서명 문자열, DB 아님) · **LLM**: 안 씀 · **개인화**: 안 함 (이력 안 읽음)
- `filters.in_stock_only`·가격 구간은 조회 조건이라, 복제 전이면 그 도서가 결과에서 아예 빠진다(재검증으로 복구 안 됨). 누락은 허용 지연 5분을 넘지 않는다.

## ② `POST /embeddings`

```mermaid
sequenceDiagram
    participant C as 호출자 (①③⑤⑥ 내부 · 초기 적재 배치)
    participant E as AI · /embeddings
    participant UP as 임베딩 업스트림

    C->>E: {texts[≤256], purpose}
    E->>UP: 배치 임베딩
    UP-->>E: 벡터[]
    E-->>C: 200 {vectors[], dim, model}

    Note over E: 업스트림 down → 503 (Retry-After)
    Note over E: dim이 인덱스 차원과 다르면 호출자가 저장하지 않음 (확정 차원 = ERD §7)
    Note over C: 초기 적재 = 복제 끝난 뒤 book_id 순 256건씩 · dim 확인 후 upsert · 중단되면 빈 곳부터
```

- **읽기/쓰기**: 없음 (순수 변환) · **LLM**: 안 씀 · 내부 전용

## ③ `POST /recommendations/chat` — 텍스트 턴

```mermaid
sequenceDiagram
    participant BE as 백엔드
    participant C as AI · /recommendations/chat
    participant LLM as LLM
    participant EMB as 임베딩
    participant IDX as 키워드+벡터 인덱스
    participant CP as 복제 사본 (AI Postgres)

    BE->>C: {user_id, spec, message, recent_turns, exclude_book_ids}
    C->>LLM: [1] spec 갱신 (message + spec + recent_turns)
    LLM-->>C: 갱신된 spec (6키)
    C->>EMB: spec.semantic 임베딩
    C->>IDX: 키워드·벡터 후보
    C->>CP: taste_profile + computed_at 이후 이력 + v_book_popularity
    C->>C: [2] 병합 + 취향유사도·인기 채점 → 후보 10<br/>(구매한 책·별점 1~2점 제외)
    C->>LLM: [3] 후보 10 + spec → 3권 선택 + reason_short + reason_long + match_basis (1회)
    LLM-->>C: 카드 3장
    C->>C: [4] reason_short 없는 책 제외 (3장 미만 가능)
    C-->>BE: 200 {reply, spec, cards[≤3], buttons, degraded:false}
    BE->>BE: card.reason_long 보관 → 상세 페이지에서 그대로 사용 (AI 재호출 없음)

    Note over C,LLM: LLM down → [1][3] 스킵 → 요청 spec으로 [2]만 → 점수순 3권<br/>reason_short=규칙, reason_long=null, degraded:true (본문으로 알림)
```

- **읽기**: `taste_profile`, `v_user_purchases`/`library`/`reviews`, `v_book_popularity`, `v_books`, `book_embeddings` — 전부 복제 사본
- **쓰기**: **없음** — `reason_long`은 응답에 실려 나가고 BE가 보관 (AI 쪽 캐시 없음)
- **대화 저장**: ✕ — `spec`·`recent_turns`를 BE가 매 턴 보냄

## ③ `POST /recommendations/chat` — 이미지 턴 (V2)

```mermaid
sequenceDiagram
    participant FE as 프론트
    participant BE as 백엔드
    participant OBJ as 오브젝트 스토리지
    participant C as AI · /recommendations/chat
    participant VLM as 표지 인식 (VLM)
    participant IDX as 벡터 인덱스

    FE->>BE: 표지 사진 (여기서 한 번만 큰 전송)
    BE->>OBJ: 업로드
    OBJ-->>BE: Pre-signed URL (만료 있음)
    BE->>C: {user_id, spec, image_ref} — message 없음
    C->>OBJ: image_ref로 이미지 fetch
    C->>VLM: 표지 → 제목·저자 텍스트·특징
    C->>IDX: 카탈로그 매칭 (confidence)
    alt 1권으로 특정
        C->>C: recognition.recognized=true, spec.anchor_book 갱신
        C-->>BE: 200 책 소개 reply + 유사 도서 카드 + buttons[네/아니요]
    else 후보 여럿 (≤3)
        C-->>BE: 200 recognition.candidates[] + buttons[confirm_book/retake]<br/>(anchor_book은 null 유지)
    else 인식 실패 / 카탈로그에 없음
        C-->>BE: 200 candidates:[] + 안내 reply (두 경우 구분 안 함)
    end

    Note over C: 이미지 저장 안 함 · URL 만료면 404 image_not_found
```

- **읽기**: `v_books`, `book_embeddings`, (`recognized` 시 유사 카드 채점) `taste_profile` 등
- **쓰기**: 없음 · **이미지 저장**: ✕

## ④ `GET /recommendations/feed`

```mermaid
sequenceDiagram
    participant BE as 백엔드
    participant F as AI · /recommendations/feed
    participant VEC as 벡터 인덱스
    participant CP as 복제 사본 (AI Postgres)

    BE->>F: GET 쿼리 파라미터 {user_id, surface, sort, category·pub_year·match_score_min, cursor}
    F->>CP: taste_profile (centroid, tag_weights, profile_version)
    alt centroid 없음 / 정보 부족
        F->>CP: v_book_popularity + 신간
        F-->>BE: 200 {items[], cold_start:true} (match_score=0)
    else 정상
        F->>VEC: centroid 유사 도서
        F->>CP: v_books(작가·카테고리·신간) + 이력(v_user_*) + v_book_popularity
        F->>F: 규칙점수(작가·카테고리·태그·이력·인기) + 벡터유사도 → 가중합<br/>이미 산/담은 책·별점 1~2점 제외 → 필터·match_score_min → next_cursor 서명
        F-->>BE: 200 {items[], next_cursor, cold_start:false}
    end

    Note over F: 벡터 인덱스 이상·모델 교체 구간 → 규칙점수만 + X-Degraded: rule-only<br/>centroid 없으면 rule-only 아니라 cold_start
    Note over F: 결과 목록 저장 안 함 · 커서 발급 후 생긴 이력은 제외 대상에서 뺌
    Note over F: 카탈로그에서 빠진 도서는 조인 탈락 → 한 페이지가 size보다 짧을 수 있음 (끝은 next_cursor:null로만)
```

- **읽기**: `taste_profile`, `v_books`, `v_user_purchases`/`library`/`reviews`, `v_book_popularity`, `book_embeddings` — 전부 복제 사본
- **쓰기**: 없음 · **LLM**: 안 씀 · **이유 문구**: 없음 (순서 + `match_score`만, 긴 이유는 ③에서만)
- **요청**: 요청 본문 없이 쿼리 파라미터만 받는다 — `user_id`·`surface`·`sort`·`category`·`pub_year_from`·`pub_year_to`·`match_score_min`·`size`·`cursor`가 허용 목록 전부이고 그 밖의 키는 400 · **응답 헤더**: `Cache-Control: private, no-store`

## ⑤ `POST /preferences/extractions` (V2 · 야간 배치)

```mermaid
sequenceDiagram
    participant BAT as 야간 배치 (BE)
    participant X as AI · /preferences/extractions
    participant LLM as LLM
    participant EMB as 임베딩

    BAT->>BAT: 그날 종료된 대화 세션 수집
    loop 세션마다
        BAT->>X: {user_id, consented, conversation, conversation_id, existing_preferences}
        alt consented=false
            X-->>BAT: 200 {extractions:[], nothing_found:true}
        else
            X->>LLM: 세션 전체 → 취향 사실 추출 (existing과 중복 제거)
            LLM-->>X: {type, value, confidence}[]
            X->>X: confidence < 0.5 버림
            X->>EMB: 각 value 임베딩
            EMB-->>X: 벡터[]
            X-->>BAT: 200 {extractions[]{type, value, confidence, vector, dim, source_conversation_id}, nothing_found}
        end
        BAT->>BAT: preference_memory에 한 행씩 저장 (conversation_id로 이전 결과 교체)
    end

    Note over X: AI는 입력·결과 저장 안 함 (AI DB에도 안 남김) · 업스트림 down → 503, 다음 날 밤 재시도
    Note over X: 뽑은 기억이 AI로 돌아오는 길은 ⑥ 요청 본문 하나뿐
```

- **읽기**: 없음 (대화는 요청 본문)
- **쓰기**: **없음** — 반환만. `preference_memory` 저장은 BE 몫

## ⑥ `POST /preferences/profile`

```mermaid
sequenceDiagram
    participant BE as 백엔드
    participant P as AI · /preferences/profile
    participant CP as 복제 사본 (AI Postgres)
    participant PRE as 사전 임베딩 자산
    participant TP as taste_profile

    BE->>P: {user_id, idempotency_key, onboarding, memories[]}
    P->>P: 멱등키 확인 (같은 키+같은 본문 → 재계산 없이 저장 결과 반환)
    P->>CP: v_user_purchases ∪ v_user_library ∪ v_user_reviews (user_id)
    P->>CP: liked·이력 책들의 문서벡터 (book_embeddings)
    P->>PRE: memories(type:author) 작가벡터 · 카테고리·태그 벡터
    P->>P: centroid = 가중평균(책·작가·태그·기억 벡터)<br/>구매 +3 · 리뷰 4~5점 +2 · 담기 +1 · 리뷰 1~2점 −2(재료 제외·추천 제외)<br/>tag_weights = 온보딩 태그 + 기억 type 집계 + 이력 카테고리 점수
    P->>TP: upsert {centroid, tag_weights, cold_start, computed_at, profile_version}
    P-->>BE: 200 {cold_start, profile_version}

    Note over P: LLM·임베딩 안 부름 → 503·504 없음
    Note over P: 같은 user_id 동시 호출은 직렬 처리, 나중에 끝난 것이 남음
    Note over P: computed_at = 실제로 반영한 이력 행들의 최대 시각 (계산 시각 아님)<br/>계산 시각으로 하면 복제 늦게 온 이력이 프로필에도 ③④ 가산에도 빠져 영구 누락
```

- **읽기**: `v_user_purchases`/`library`/`reviews`(복제 사본), `book_embeddings`, 사전 임베딩 자산, (요청 본문) `onboarding`·`memories`
- **쓰기**: `taste_profile` (upsert), 멱등키 기록
- **트리거**: 온보딩 완료 · 취향 기억 변경 — 그 외엔 호출 안 함 (이후 이력은 ③④가 `computed_at` 기준으로 그 자리에서 더함)

## ⑦ `POST /agent/act` (V2)

```mermaid
sequenceDiagram
    participant BE as 백엔드
    participant A as AI · /agent/act
    participant LLM as LLM
    participant CAND as recommendations.candidates (AI 내부)
    participant T as BE tools

    BE->>A: {user_id, message, context_cards, focused_book_id, user_context, idempotency_key}
    A->>A: [1] "N번/이거" 해석 → confidence
    alt confidence < 0.6
        A-->>BE: 200 되물음 (tool_calls:[], resolved_reference.book_ids:[])
    else 해석됨
        A->>LLM: [2] 요청 분류
        alt 골라 담기
            A->>CAND: 후보 N (홈 피드 채점 재사용 → taste_profile·복제 사본)
            A->>T: inventory.check / book.detail (가격·재고 재검증)
            A->>A: 예산·재고 필터 → match_score 합 최대 조합
        else 담기 / 수량 변경
            A->>T: cart.add / cart.update (파생 멱등키 {key}:{idx})
        else 비교
            A->>T: book.detail × N
        else 범위 밖 (취소·환불·배송추적·결제)
            A->>A: tool 없이 딥링크 안내
        end
        T-->>A: 결과
        A->>A: [3] grounding — 수치·상태는 tool 결과에서만 주입
        alt 핵심 주장이 근거 미확보
            A-->>BE: 422 grounding_failed
        else
            A-->>BE: 200 {reply, tool_calls[], resolved_reference, selection?, buttons}
        end
    end

    Note over A: 대화 저장 ✕ · 주문 생성·결제 ✕ · BE tool 실패도 200 (tool_calls에 기록)
```

- **읽기**: `taste_profile` (candidates 경유, 복제 사본), BE tool로 장바구니·재고·도서 메타
- **쓰기**: 멱등키 기록 — 장바구니 변경은 AI가 직접 안 함, **BE tool 호출로만** (BE가 `user_id`로 권한·재고 재확인)

## ⑧ `GET /health`

입력 없음. 각 구성요소(`gateway`·`database`·`vector_index`·`llm`·`embedding`) 상태를 취합해 `200 {status, version, replication_lag_seconds, components}` 반환.

- 일부 장애면 `status: degraded`, 서버 전체 응답 불능이면 `503 {"status":"down"}` (envelope 없음). 인증 없이 내부망에서만 노출.
- `replication_lag_seconds`(BE MySQL → AI Postgres 복제 지연, 초, 측정 불가면 null)는 `components` 밖에 있고 **`status`를 바꾸지 않는다** — 복제가 밀렸다고 인스턴스가 로드밸런서에서 빠지면 안 되므로. `lag > N`으로 지연을, `null이 계속됨`으로 복제 중단을 잡는다.
- `components.database`는 **AI Postgres**이지 BE MySQL 상태가 아니다.

---

## 시나리오 흐름

### 시나리오 A — 검색 0건 → 대화형 추천 전환

```mermaid
sequenceDiagram
    participant U as 사용자
    participant BE
    participant AI
    participant CP as 복제 사본
    U->>BE: "쓸쓸하고 담담한 위로" 검색
    BE->>AI: POST /search
    AI->>AI: 키워드 검색 + 벡터 검색 → RRF 병합
    AI->>CP: (인기순이면) v_book_popularity
    AI-->>BE: results:[], fallback:{"AI 추천에게 물어볼까요?"}
    BE-->>U: 0건 화면 + 전환 배너
    U->>BE: "네" (배너 클릭)
    BE->>AI: POST /recommendations/chat (초기 spec, message=검색어)
    AI-->>BE: 카드 3장 + reply
    BE-->>U: 챗봇 추천 화면
```

### 시나리오 B — 온보딩 완료 → 프로필 생성 → 홈 피드

```mermaid
sequenceDiagram
    participant U as 사용자
    participant BE
    participant AI
    participant CP as 복제 사본
    U->>BE: 온보딩 응답 제출
    BE->>AI: POST /preferences/profile (onboarding, memories, idempotency_key)
    AI->>CP: v_user_purchases/library/reviews (가입 직후 → 비어 있음)
    AI->>AI: centroid + 태그 가중치 계산 → taste_profile upsert (computed_at 저장)
    AI-->>BE: cold_start:false, profile_version:1
    U->>BE: 홈 진입
    BE->>AI: GET /recommendations/feed (surface=home, sort=match)
    AI->>AI: 규칙 점수 + 취향 벡터 유사도 가중합 (LLM 없음)
    AI->>CP: 이력·인기 읽기
    AI-->>BE: items[] + next_cursor + cold_start:false
    BE-->>U: 홈 피드 (순서 + match_score만)
```

### 시나리오 C — 대화형 추천 한 턴 (정상 / LLM 장애)

```mermaid
sequenceDiagram
    participant U as 사용자
    participant BE
    participant AI
    participant LLM
    participant CP as 복제 사본
    U->>BE: "너무 무겁지 않은 걸로"
    BE->>AI: POST /recommendations/chat (spec, message, recent_turns)
    alt 정상
        AI->>LLM: ① spec 갱신
        AI->>AI: ② 후보 10권 (키워드+벡터+취향+인기)
        AI->>CP: 이력·인기 읽기 → 산 책·별점1~2 제외
        AI->>LLM: ③ 3권 선택 + reason_short + reason_long + match_basis (1회)
        AI->>AI: ④ 한 줄 이유 없는 책 제외
        AI-->>BE: cards[≤3], spec, degraded:false
        BE->>BE: reason_long 보관
        BE-->>U: 카드 3장
        U->>BE: 카드 클릭 → 상세
        BE-->>U: 보관한 reason_long 표시 (AI 재호출 없음)
    else LLM 장애
        AI->>AI: ①③ 건너뜀, 요청 spec으로 ②만 → 상위 3권
        AI-->>BE: cards (reason_short=규칙, reason_long:null), degraded:true
        BE-->>U: 카드 3장 (상세 이유 영역 숨김)
    end
```

### 시나리오 D — 표지 사진 인식 (V2)

```mermaid
sequenceDiagram
    participant U as 사용자
    participant BE
    participant AI
    U->>BE: 표지 사진 첨부
    BE->>BE: 이미지 업로드 → Pre-signed URL 발급
    BE->>AI: POST /recommendations/chat (image_ref, message 없음)
    AI->>AI: 표지 인식
    alt 한 권 특정
        AI-->>BE: recognition{recognized:true, book_id}, spec.anchor_book 갱신,<br/>유사 도서 카드, buttons[네/아니요]
    else 후보 여럿
        AI-->>BE: recognition{recognized:false, candidates[≤3]},<br/>anchor_book 갱신 안 함, buttons[후보들/다시 찍기]
    else 인식 실패
        AI-->>BE: candidates:[], 안내 reply
    end
    BE-->>U: 결과 화면
```

### 시나리오 E — 야간 배치 취향 추출 (V2)

```mermaid
sequenceDiagram
    participant Batch as 야간 배치(BE)
    participant AI
    participant LLM
    Batch->>Batch: 그날 종료된 대화 세션 수집
    loop 세션마다
        Batch->>AI: POST /preferences/extractions (conversation, existing_preferences)
        AI->>LLM: 세션 전체 → 취향 사실 추출
        AI->>AI: 각 사실 임베딩, confidence<0.5 버림
        AI-->>Batch: extractions[] (value, confidence, vector) / nothing_found
        Batch->>Batch: preference_memory에 한 행씩 저장
    end
    Note over Batch,AI: 취향 기억이 바뀌었으므로<br/>이후 POST /preferences/profile 재호출
```

### 시나리오 F — 쇼핑 에이전트 "3만원 안에서 2권 골라 담아줘" (V2)

```mermaid
sequenceDiagram
    participant U as 사용자
    participant BE
    participant AI
    participant Cand as recommendations.candidates(내부)
    participant Tool as BE tool
    U->>BE: "3만원 안에서 2권 골라 담아줘"
    BE->>AI: POST /agent/act (message, context_cards, idempotency_key)
    AI->>AI: 1. 지시 표현 해석  2. 분류 → 골라 담기
    AI->>Cand: 후보 요청
    AI->>Tool: inventory.check / book.detail (가격·재고 재검증, 병렬)
    AI->>AI: 예산·재고 필터 → match_score 합 최대 조합
    AI->>AI: grounding 검사 (수치는 tool 결과에서만)
    AI-->>BE: reply, selection{chosen, total_price:26300}, buttons[담기/다른 조합]
    BE-->>U: "달러구트+아무튼,산 = 26,300원. 담을까요?"
    U->>BE: [담기] 클릭
    BE->>AI: POST /agent/act (confirm_selection, 같은 idempotency_key)
    AI->>Tool: cart.add × 2 (파생 멱등키 key:0, key:1)
    AI-->>BE: "담았어요", tool_calls 기록
    BE-->>U: 장바구니 반영
```

### 시나리오 G — 프로필 이후 생긴 이력이 다음 추천에 반영되는 원리

```mermaid
flowchart LR
    P["프로필 생성<br/>computed_at = 반영한 이력의 최대 시각 저장"] --> B1["이후 2077 구매<br/>1502에 별점 2점<br/>(복제로 넘어옴, 최대 5분)"]
    B1 --> C["다음 ③/④ 요청"]
    C --> R["채점 시 복제 사본을 읽어<br/>computed_at 이후 이력만 추가<br/>2077: 가중치 +3<br/>1502: centroid 제외 + 추천 제외"]
    R --> N["프로필 재호출 없이<br/>최신 취향 반영, 이중 반영 없음"]
```

- `computed_at`이 **계산 시각**이 아니라 **반영한 이력 행의 최대 시각**인 이유가 여기 있다 — 복제가 계산 시점 이후에 도착한 이력도, 그 행의 실제 시각이 `computed_at`보다 뒤이므로 다음 채점에서 빠짐없이 더해진다.
