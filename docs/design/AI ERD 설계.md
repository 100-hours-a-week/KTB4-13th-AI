# AI 데이터 ERD 및 테이블 필드 명세서

AI 서버가 소유하는 테이블, BE에서 복제받는 커머스 테이블, 요청 본문으로 받는 데이터, 그리고 AI가 만들어 BE로 내보내는 것을 정리한다.

**BE는 MySQL, AI 서버는 pgvector를 얹은 PostgreSQL을 쓴다. 복제는 BE MySQL → AI Postgres 단방향뿐이고 역방향 복제는 없다.**

| 구분 | 대상 | 원본 주인 | AI Postgres 안 | AI의 쓰기 |
| --- | --- | --- | --- | --- |
| AI 소유 | book_embeddings, taste_profile, idempotency_records | AI | 원본이 여기 있다 | 쓴다 |
| BE 복제 | v_books, v_book_popularity, v_user_purchases, v_user_library, v_user_reviews | BE(MySQL) | 사본이 여기 있다. 단방향 복제로 채워진다 | 쓰지 않는다. 써도 다음 복제에 덮인다 |
| 요청 본문 통과 | users, onboarding_responses, user_memories | BE(MySQL) | 없다 | 저장하지 않는다 |
| 응답 본문 산출 | extractions[], reason_long 등 (§5) | 생성은 AI, 보관은 BE | 없다 | 저장하지 않는다 |

`v_` 접두사는 BE 원본의 사본이라는 표시다. view가 아니다.

온보딩 카테고리·태그의 사전 벡터는 테이블이 아니다. 서버 기동 시 만들어 메모리에 두는 자산이며 §7 결정 기록에 있다.

## 0. API 목록

필드 명세의 '필요한 이유'에서 아래 이름으로 부른다.

| 이름 | 경로 | 기능 | 버전 |
| --- | --- | --- | --- |
| AI 검색 | POST /search | 키워드 검색과 벡터 검색 순위를 합쳐 도서를 조회 | V1 |
| 텍스트 임베딩 | POST /embeddings | 텍스트를 벡터로 변환 | V1 |
| 챗봇 추천 | POST /recommendations/chat | 대화로 추천 카드 최대 3장 생성 | V1 |
| 홈 피드 | GET /recommendations/feed | 취향 프로필 기반 개인화 목록 | V1 |
| 취향 기억 추출 | POST /preferences/extractions | 야간 배치로 대화에서 취향 추출 | V2 |
| 취향 프로필 생성 | POST /preferences/profile | 온보딩·기억·이력으로 취향 프로필 생성 | V1 |
| 쇼핑 에이전트 | POST /agent/act | 쇼핑 의도 해석 후 BE tool 실행 | V2 |
| 서버 상태 점검 | GET /health | 구성 요소 가용 상태 반환 | 공통 |

## 1. ERD

**배치도.** 무엇이 어느 DB 안에 있는지, 무엇이 저장되지 않고 지나가는지를 먼저 본다.

```mermaid
flowchart LR
    BE["BE 애플리케이션"]
    AI["AI 서버"]
    subgraph BEDB["BE MySQL · 원본"]
        SRC[("도서 카탈로그 · 인기 집계<br/>구매 · 나의 도서관 · 리뷰<br/>회원 · 온보딩 · 취향 기억")]
    end
    subgraph AIDB["AI PostgreSQL + pgvector"]
        REP[("BE 복제 · 읽기만<br/>v_books · v_book_popularity<br/>v_user_purchases<br/>v_user_library · v_user_reviews")]
        OWN[("AI 소유 · 쓰기<br/>book_embeddings<br/>taste_profile<br/>idempotency_records")]
    end
    BE --- SRC
    AI --- REP
    AI --- OWN
    SRC ==>|"단방향 복제 · 역방향 없음"| REP
    REP -.-|"같은 DB라 필터와 벡터 정렬이 한 SQL<br/>book_embeddings → v_books FK"| OWN
    BE -->|"요청 본문<br/>users · onboarding_responses · user_memories<br/>(AI DB에 저장하지 않음)"| AI
    AI -.->|"응답 본문 · tool<br/>extractions · reason_long · cart.add<br/>(BE가 저장. AI DB에 원본 없음)"| BE
```

**관계도.** 아래 ER 다이어그램은 조인 축과 컬럼을 그린다. 관계선 중 DB FK인 것은 라벨에 FK라고 적었고 나머지는 조인 축이다. `USERS`, `ONBOARDING_RESPONSES`, `USER_MEMORIES`는 AI DB에 없고 요청 본문으로만 오지만, `user_id` 조인 축의 출처를 보이기 위해 함께 그린다.

```mermaid
erDiagram
    USERS ||--o| TASTE_PROFILE : "챗봇, 피드, 에이전트 후보 채점이 user_id로 조회"
    USERS ||--o| ONBOARDING_RESPONSES : "요청으로 전달"
    USERS ||--o{ USER_MEMORIES : "요청으로 전달"
    USERS ||--o{ V_USER_PURCHASES : "user_id"
    USERS ||--o{ V_USER_LIBRARY : "user_id"
    USERS ||--o{ V_USER_REVIEWS : "user_id"
    V_BOOKS ||--o| BOOK_EMBEDDINGS : "book_id (FK, cascade)"
    V_BOOKS ||--o| V_BOOK_POPULARITY : "book_id"
    V_BOOKS ||--o{ V_USER_PURCHASES : "book_id"
    V_BOOKS ||--o{ V_USER_LIBRARY : "book_id"
    V_BOOKS ||--o{ V_USER_REVIEWS : "book_id"

    BOOK_EMBEDDINGS {
        int book_id PK "AI 소유. v_books FK"
        vector embedding "차원 N은 §7에서 확정"
        int dim "벡터 길이"
        string model "생성 모델"
    }
    TASTE_PROFILE {
        int user_id PK "AI 소유"
        vector centroid "null 허용. 차원 N"
        jsonb tag_weights "태그 가중치"
        bool cold_start "개인화 비활성 여부"
        int profile_version "순위 영향 값 변경 시 증가"
        timestamptz computed_at "반영한 이력의 최대 시각. null 허용"
    }
    IDEMPOTENCY_RECORDS {
        string idempotency_key PK "AI 소유"
        string body_hash "본문 대조"
        jsonb stored_response "저장 응답"
        timestamptz created_at "최소 24시간 보관"
    }
    V_BOOKS {
        int book_id PK "BE 복제"
        string title ""
        string author "null 허용"
        string publisher "null 허용"
        int price "판매가 원"
        bool in_stock ""
        string cover_url "null 허용"
        string category "null 허용"
        int pub_year "null 허용"
        text description "null 허용"
    }
    V_BOOK_POPULARITY {
        int book_id PK "BE 복제"
        int sales "최근 판매 수"
        float rating_avg "null 허용"
        int rating_count "리뷰 수"
        timestamptz as_of "집계 시각"
    }
    V_USER_PURCHASES {
        int user_id "BE 복제"
        int book_id ""
        timestamptz purchased_at "computed_at·커서 발급 시각 비교 축"
    }
    V_USER_LIBRARY {
        int user_id "BE 복제"
        int book_id ""
        timestamptz added_at "computed_at·커서 발급 시각 비교 축"
    }
    V_USER_REVIEWS {
        int user_id "BE 복제"
        int book_id ""
        int rating "1-5"
        timestamptz created_at "computed_at 비교 축"
    }
    USERS {
        int user_id PK "요청 본문. AI DB에 없음"
        bool consented "취향 수집 동의"
    }
    ONBOARDING_RESPONSES {
        int user_id PK "요청 본문. AI DB에 없음"
        string[] reading_times "최대 5"
        string[] criteria "최대 3"
        string[] categories "최대 3"
        string[] tags "최대 9"
        int[] liked_book_ids "앞 50권 사용"
    }
    USER_MEMORIES {
        int user_id "요청 본문. AI DB에 없음"
        string type "mood topic author condition"
        string value "한두 문장"
        float confidence "0-1"
        float[] vector "value 문장 벡터. AI DB에 저장하지 않음"
        int dim "벡터 길이"
        string source_conversation_id "출처 세션"
    }
```

- **AI 소유 테이블이 `v_books`를 참조하는 관계선은 실제 FK다.** `book_embeddings.book_id`는 `v_books(book_id)`를 참조하며 `ON DELETE CASCADE`다. 도서가 사본에서 지워지면 그 책의 임베딩도 함께 지워진다. 이 때문에 복제 방식에 전제가 붙는다(§3).
- **복제 테이블끼리의 관계선**(이력 3종·인기 → `v_books`)과 **`user_id` 축**은 조인 축이며 FK가 아니다. 복제 테이블 사이의 무결성은 BE 원본이 보장하고, AI DB에는 `users`가 없다.
- **모든 시각 컬럼은 UTC `timestamptz`**다. MySQL `DATETIME`은 시간대가 없으므로 복제 시 UTC로 해석해 적재한다. `taste_profile.computed_at`을 복제된 이력 시각과 직접 비교하는 것이 이중 반영 차단의 유일한 장치라, 어긋나면 예외도 로그도 없이 **점수만 조용히 틀린다.**

## 2. AI 서버 소유 테이블

### 2-1. book_embeddings

도서 적재 시 생성한 벡터를 보관한다. 도서 한 권에 벡터 하나다. 검색의 벡터 검색, 챗봇·피드의 취향 유사도, 취향 벡터 계산이 조회한다. `v_books`가 복제된 뒤 `description`이 있는 행에 대해 생성한다(적재 절차는 API 명세 ②).

BE에서 도서가 삭제되어 복제로 `v_books` 행이 지워지면 이 행도 FK cascade로 함께 지워진다. AI 내부 정리 배치는 두지 않는다. 비공개 후 다시 공개된 도서는 `v_books` 행이 다시 들어온 뒤 신간과 같은 적재 절차로 임베딩을 다시 만든다. 복제 지연 창(행이 아직 없거나 방금 지워진 사이)에 대비해 **벡터 검색 결과는 항상 `v_books`와 조인하고 조인에 실패한 `book_id`는 제외**하는 규칙(API 명세 §5 카탈로그 조인)은 안전망으로 유지한다. BE에 삭제 통지를 따로 요구하지 않는다 — 복제가 delete를 전달하면 된다(§7 증분 복제 가능 조건).

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| book_id | int PK, FK | N | 커머스 도서 ID. `v_books(book_id)` 참조, ON DELETE CASCADE | v_books, v_book_popularity, 이력 3종과 조인하는 축. 응답의 도서 식별자와 같은 값 | BE가 발급하는 커머스 정수 ID를 그대로 사용 |
| embedding | vector(N) | N | 도서 소개 문장의 임베딩 벡터 | 벡터 검색과 취향 유사도 계산의 피연산자 | pgvector 인덱스(HNSW) 대상이라 vector 타입 필수. 차원 N은 §7에서 확정 |
| dim | int | N | 벡터 길이 | 임베딩 API가 저장 전 인덱스 차원 일치 검증을 요구. 검증 기준값을 행에 함께 보관 | 차원 수 비교만 하므로 정수 |
| model | string | N | 벡터 생성 모델 식별자 | 모델 교체 시 재생성 대상 행을 구분 | 모델명 문자열. 예 bge-m3-2026q3 |

### 2-2. taste_profile

취향 프로필 생성이 upsert하고, 챗봇 추천·홈 피드·쇼핑 에이전트의 후보 채점(`recommendations.candidates`, 피드와 같은 채점)이 조회한다. 사용자당 한 행이며 호출마다 전체를 재계산한다. 이름은 기능정의를 따른다. 4단계·5단계와 결정 노트 일부가 `user_profiles`라 부르는 것과 같은 표이며, 그쪽 문구를 이 이름으로 맞춘다.

**탈퇴한 사용자의 행은 삭제한다.** 사용자 취향에서 뽑아낸 개인 데이터이고 AI 서버가 소유하므로 복제로는 사라지지 않는다. AI 서버는 탈퇴 사실을 스스로 알 수 없어 BE가 알려 줘야 하며, 경로는 §7에서 정한다.

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| user_id | int PK | N | 커머스 사용자 ID | 챗봇·피드·프로필 생성 요청이 모두 user_id로 조회. 별도 프로필 키를 두지 않음 | BE 발급 정수 ID. 사용자당 한 행이라 그대로 PK |
| centroid | vector(N) | Y | 사용자 취향 대표 벡터 | 도서 임베딩과 유사도를 계산해 개인화 점수를 산출 | 도서 벡터와 같은 공간에서 연산하므로 동일 차원(N은 §7). 재료가 없는 cold_start 사용자는 생성 불가라 Null 허용 |
| tag_weights | jsonb | N | 태그별 가중치 맵 | 피드 규칙 점수의 태그 항 입력값 | 키 집합이 사용자마다 달라 고정 컬럼으로 전개 불가. 값이 없으면 빈 객체 |
| cold_start | bool | N | 개인화 비활성 여부 | true면 피드가 인기·신간 목록으로 대체하고 매칭 점수를 0으로 반환 | 두 값 분기라 bool |
| profile_version | int | N | 프로필 판 번호 | 피드 커서에 실려 어느 프로필 기준의 목록인지 식별 | 순위 영향 값 변경 시에만 증가하는 단조 증가 정수 |
| computed_at | timestamptz | Y | 이 프로필이 **반영한 이력 행들의 최대 시각**. 계산 시각이 아니다 | 챗봇·피드가 이 시각 이후의 이력만 가산해 이중 반영을 차단. 계산 시각으로 두면 복제가 늦은 이력이 프로필에도 가산에도 빠져 영구 누락된다 | 이력 테이블의 시각 컬럼과 직접 비교하므로 동일 타입. 반영한 이력이 없으면 Null이며 이때는 이력 전부를 가산한다 |

**tag_weights JSON 예시**

```json
{ "힐링": 0.8, "성장": 0.5, "에세이": 0.3 }
```

### 2-3. idempotency_records

취향 프로필 생성과 쇼핑 에이전트의 멱등 키를 보관한다. 같은 키·같은 본문은 저장 응답을 200으로 재생하고, 같은 키·다른 본문은 409를 반환한다.

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| idempotency_key | string PK | N | 요청 멱등 키 | 재시도로 프로필이 두 번 갱신되거나 장바구니에 두 번 담기는 것을 차단 | BE가 생성한 문자열을 그대로 수신. 예 prof_20260904_a1b2 |
| body_hash | string | N | 요청 본문 해시 | 같은 키에 다른 본문이 오면 409로 거절하기 위한 대조값 | 동일성 판정만 필요하므로 원문 대신 해시 |
| stored_response | jsonb | N | 최초 처리 결과 응답 | 재도착 시 재계산 없이 그대로 반환 | 응답 envelope 구조를 그대로 저장하므로 JSON |
| created_at | timestamptz | N | 저장 시각 | 최소 24시간 보관 요건의 만료 판정 기준 | 시각 비교. UTC 기준 |

**stored_response JSON 예시**

```json
{ "message": "profile_success", "data": { "cold_start": false, "profile_version": 3 } }
```

## 3. BE에서 복제받는 커머스 테이블

BE MySQL의 커머스 데이터를 AI PostgreSQL로 단방향 복제한 사본이다. AI는 조회만 한다.

**계약면은 아래 다섯 이름과 그 컬럼 집합이다.** BE가 내부 스키마를 바꿔도 이 컬럼들이 같은 이름·같은 뜻으로 복제되면 AI는 그대로 돈다. 컬럼 **추가**는 통보 없이 해도 되지만, **컬럼명·타입·의미 변경과 삭제는 복제를 끊으므로 BE가 사전에 알린다.**

**복제 방식에는 두 가지 전제가 붙는다.**

1. AI 소유 `book_embeddings`가 `v_books`를 FK로 참조하므로 **복제는 행 단위 upsert·delete여야 하며, `v_books`를 TRUNCATE 후 재적재하는 방식은 쓸 수 없다.** 재적재 순간 임베딩이 cascade로 전량 지워지거나 삭제가 거절된다. 신간의 임베딩은 `v_books` 행이 도착한 뒤에만 만든다(초기 적재 절차가 이미 복제 완료 후로 되어 있어 새 제약은 아니다).
2. AI가 `v_books`에 얹은 보조 인덱스(키워드 검색용 tsvector·pg_trgm, §3-1)를 복제가 보존해야 한다. 테이블을 DROP 후 다시 만드는 방식이면 인덱스가 사라진다.

실패는 셋으로 갈린다.

- **복제가 늦어 값이 낡은 것** — 오류가 아니다. 낡은 값 그대로 200으로 응답하고 `X-Degraded`도 붙지 않는다. 허용 지연은 아래 신선도 예산 표를 따른다.
- **행이 아직 없는 것** — 오류가 아니다. 해당 항을 0점 처리하고 200으로 응답한다.
- **AI Postgres가 응답하지 않는 것** — 전면 500이다. 위 0점 규약을 적용하지 않는다.

복제 지연은 `/health`의 `replication_lag_seconds`로 관찰한다.

**복제 대상 다섯.** 각 컬럼의 상세는 3-1～3-3에 있다.

| 복제 테이블 | 담긴 것 | 쓰는 곳 |
| --- | --- | --- |
| v_books | book_id, title, author, publisher, price, in_stock, cover_url, category, pub_year, description | ①③④의 응답 항목, 벡터 적재, 키워드 검색 인덱스, ⑥ 이력 책의 카테고리 점수, ③ 이미지 턴(V2)의 제목·저자 텍스트 검색 |
| v_user_purchases | user_id, book_id, purchased_at | ⑥ 취향 벡터, ③④ 채점과 중복 제외 |
| v_user_library | user_id, book_id, added_at | 같음 |
| v_user_reviews | user_id, book_id, rating(1–5), created_at | 같음 |
| v_book_popularity | book_id, sales, rating_avg, rating_count, as_of | ① 인기순, ③ 후보 채점, ④ 인기 항과 cold_start 목록 |

**허용 지연(신선도 예산).** 사본이라 실시간이 아니다. 이 값을 넘기면 복제 문제로 보고 조치한다. **복제 수단과 주기 자체는 계약이 아니며** BE·AI·클라우드가 함께 정한다(§7). 계약이 되는 것은 위 컬럼 집합·아래 허용 지연·스키마 변경 통보 의무·위 두 전제다.

| 대상 | 허용 지연 | 늦으면 |
| --- | --- | --- |
| v_books — 가격·재고 | 5분 | 검색·카드·피드가 옛 가격과 재고를 보여준다. 담기·주문에서 BE가 원본으로 재확인하므로 잘못된 거래는 성립하지 않는다 |
| v_books — 신간 입고 | 1시간(임베딩 생성 포함) | 새 책이 검색·추천에 뜨지 않는다. 카탈로그에는 이미 있어 "검색에서만 안 나오는 책"이 된다 |
| v_book_popularity | 24시간 | 인기순 정렬과 추천의 인기 항이 어제 값으로 계산된다 |
| 이력 3종 | 5분 | 방금 산 책이 홈 피드와 챗봇 카드에 다시 뜬다. 사용자가 즉시 알아채는 유일한 지연이다 |
- 표시 값과 결제 값이 다르면 **BE 값이 최종**이기 때문에, `price`·`in_stock`이 낡아도 잘못된 거래는 성립하지 않는다.
- **다만 `filters.in_stock_only`와 가격 구간은 조회 조건이라 재검증으로 복구되지 않는다.** 복제 전이면 그 도서가 결과에서 빠지고 사용자는 아예 보지 못한다. 누락은 위 허용 지연을 넘지 않는다.

### 3-1. v_books

검색 결과, 챗봇 카드, 피드 항목의 응답 필드가 여기서 나온다. **키워드 검색 인덱스(tsvector + pg_trgm)도 이 테이블의 title·author·description에 건다** — ① 검색, ③ 후보 검색, (V2) 표지 인식의 제목·저자 텍스트 검색이 쓴다. 벡터 검색은 `book_embeddings`와 조인한다.

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| book_id | int PK | N | 도서 ID | 응답의 도서 식별자이자 임베딩·인기·이력 테이블의 조인 축. `book_embeddings`가 FK로 참조 | 커머스 정수 ID |
| title | string | N | 제목 | 검색 결과와 카드 응답 필드. 키워드 인덱스 대상 | 응답 스키마가 string |
| author | string | **Y** | 저자 | 같음. 키워드 인덱스 대상 | 응답 스키마가 string. 원천(국중·정보나루)이 저자를 주지 않는 책이 카탈로그 실측 4.0만 건(1.7%) 있어 2026-09-18에 Null 허용으로 바꿨다(#8) |
| publisher | string | **Y** | 출판사 | 검색 결과 응답 필드 | 응답 스키마가 string. 빈 값 실측 4건이라 같은 결정으로 Null 허용(#8) |
| price | int | N | 판매가(원) | 응답 필드이자 검색의 가격 구간 필터 조건 | 할인 적용 후 원 단위라 소수점 불필요. 원본이 `DECIMAL`이면 복제 시 정수로 맞춘다(§7) **NOT NULL 유지**(#8) — 판매가는 BE가 산출하는 값이라 복제 시점에 비어 있을 이유가 없고, Null을 허용하면 가격 구간 필터에서 그 책이 조용히 빠진다. 카탈로그 덤프가 주는 값은 **정가**이며 결측은 0건이다 |
| in_stock | bool | N | 재고 여부 | 응답 필드이자 검색의 품절 제외 필터 조건 | 재고 유무만 판정하므로 bool. MySQL `TINYINT(1)`은 복제 시 boolean으로 변환. **NOT NULL 유지**(#8) — Null을 허용하면 `in_stock_only` 필터와 부분 인덱스에서 "재고 모름"이 조용히 품절로 처리된다. 카탈로그 덤프에는 이 값이 없으므로 개발용 적재에서는 적재 스크립트가 기본값을 채운다 |
| cover_url | string | **Y** | 표지 이미지 URL | 응답 필드 | URL 문자열. 국중 표지가 없는 책이 카탈로그 실측 85.8%라 NOT NULL이면 대부분 적재할 수 없다. 플레이스홀더로 채우면 깨진 이미지를 그리게 되므로 Null 허용으로 바꿨다(2026-09-18, #8). 표시는 프런트가 대체 이미지로 처리 |
| category | string | Y | 카테고리명 | 검색·피드의 카테고리 필터 조건, ⑥의 이력 책 카테고리 점수. 온보딩 관심 대분류와 값 집합이 동일해야 매칭됨 | 문자열 정확 일치로 동작하므로 복제 시 NFC 정규화하고 후행 공백을 다듬는다(§7). 미분류 도서는 필터에서 제외되므로 Null 허용 |
| pub_year | int | Y | 출간연도 | 검색·피드의 출간연도 구간 필터, newest 정렬 기준 | 연 단위 비교라 정수. 미상 도서는 필터·정렬에서 제외되므로 Null 허용 |
| description | text | Y | 도서 소개 | 문서 임베딩(purpose: document)의 입력 텍스트. 키워드 인덱스 대상. ③이 이유 문장을 쓸 때 LLM에 주는 도서 소개 | 길이 제한이 큰 본문이라 text. 없으면 임베딩을 생성하지 않아 벡터 검색 대상에서 빠짐 |

### 3-2. v_book_popularity

검색의 인기순 정렬, 챗봇의 후보 채점, 피드의 인기 항과 cold_start 목록이 모두 이 테이블을 읽는다. **신호는 판매와 리뷰 둘뿐이다** — 조회 수와 BE가 미리 매긴 랭킹은 받지 않는다. 행이 없는 도서는 인기 항 0점으로 계산하며, 도서 행과 인기 행의 도착 시점이 달라 갓 등록된 도서는 한동안 이 상태다.

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| book_id | int PK | N | 도서 ID | 도서 단위 집계라 도서당 한 행 | 커머스 정수 ID |
| sales | int | N | 최근 판매 수 | 검색 인기순, 챗봇 후보 채점, 피드 인기 항의 입력값 | 건수라 정수. 판매 없으면 0. 집계 창은 컬럼명에 박지 않는다 |
| rating_avg | float | Y | 평균 별점 | 인기 항 계산 입력값 | 1-5 구간의 평균이라 실수. 리뷰가 없으면 산출 불가라 Null 허용 |
| rating_count | int | N | 리뷰 수 | `rating_avg` 단독으로는 리뷰 1건 5점이 100건 4.5점을 이기므로 신뢰도 보정에 함께 쓴다 | 건수라 정수. 리뷰 없으면 0 |
| as_of | timestamptz | N | 집계 기준 시각. BE가 채운 값이 그대로 복제된다 | 값의 신선도 판별. 인기 값의 실제 나이는 BE 집계 주기 + 복제 지연이다 | 시각 비교. UTC 기준 |

### 3-3. 이력 3종 (v_user_purchases, v_user_library, v_user_reviews)

세 테이블이 같은 형태이며 시각 컬럼명만 다르다. 취향 벡터 계산, 챗봇·피드 채점, 추천 제외에 함께 사용한다.

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| user_id | int | N | 사용자 ID | 세 테이블 모두 user_id로 조회 | 커머스 정수 ID |
| book_id | int | N | 도서 ID | 해당 도서의 임베딩을 취향 벡터 재료로 사용하고, 추천 제외 판정에도 대조. 카테고리 점수는 `v_books.category`를 조인해 구한다 | 커머스 정수 ID |
| purchased_at | timestamptz | N | 구매 시각. v_user_purchases | computed_at 이후 발생분만 채점에 가산하기 위한 비교 축. ④ 피드는 **커서 발급 시각**과도 비교해 그 이후 생긴 이력을 제외 대상에서 뺀다(스크롤이 밀리지 않게) | taste_profile.computed_at과 직접 비교하므로 동일 타입·동일 기준 시간대(UTC) |
| added_at | timestamptz | N | 도서관 담기 시각. v_user_library | 같음(커서 발급 시각 비교 포함) | 같음 |
| created_at | timestamptz | N | 리뷰 작성 시각. v_user_reviews | computed_at 이후 발생분만 채점에 가산하기 위한 비교 축 | 같음 |
| rating | int | N | 별점. v_user_reviews | 4-5점은 선호로 가산하고, 1-2점은 비선호로 카테고리 점수를 깎으며 그 도서를 추천에서 제외 | 1-5 정수 구간이며 구간 분기만 하므로 정수 |

## 4. 요청 본문으로 받는 BE 데이터

AI가 조회하지 않는다. BE가 요청에 실어 보내고 AI는 계산에만 사용한다.

### 4-1. users

**사용자 테이블은 복제하지 않는다.** 요청마다 user_id와 consented를 받으며, AI DB에 사용자 테이블이 없으므로 존재 검증도 FK도 두지 않는다.

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| user_id | int | N | 사용자 ID | 모든 요청의 필수 필드. 취향 프로필과 이력 테이블의 조회 축 | 커머스 정수 ID |
| consented | bool | N | 취향 수집 동의 여부 | 취향 기억 추출에서 false면 추출을 수행하지 않고 nothing_found로 응답 | 동의 여부 분기라 bool. 요청 JSON은 `1`/`0`이 아니라 `true`/`false` |

### 4-2. onboarding_responses

취향 프로필 생성 요청에 전체가 실려 온다. 매번 처음부터 재계산하므로 기억만 변경돼도 전체를 다시 보내야 한다. AI는 원본을 저장하지 않는다.

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| user_id | int | N | 사용자 ID | 프로필 생성 요청이 해당 사용자의 응답 전체를 전달 | 커머스 정수 ID |
| reading_times | string[] | Y | 읽는 시간대. 최대 5개 | 벡터화하지 않고 태그 신호로만 사용 | 다중 선택이라 배열. 미응답 시 생략 |
| criteria | string[] | Y | 책 고르는 기준. 최대 3개 | 같음 | 다중 선택이라 배열. 미응답 시 생략 |
| categories | string[] | Y | 관심 대분류. 최대 3개 | 기동 시 만들어 둔 사전 벡터(§7 결정 기록)로 centroid 재료. v_books.category와 값 집합이 같아야 함 | 다중 선택이라 배열. 미응답 시 생략 |
| tags | string[] | Y | 세부 태그. 최대 9개 | 사전 벡터가 centroid 재료이자 tag_weights의 초기값 | 다중 선택이라 배열. 미응답 시 생략 |
| liked_book_ids | int[] | Y | 선호 도서 ID 목록 | 해당 도서의 임베딩이 centroid 재료 | 도서 ID 배열. 상한 없이 수신하되 앞 50개만 사용 |

**onboarding JSON 예시**

```json
{
  "reading_times": ["밤"],
  "criteria": ["베스트셀러"],
  "categories": ["에세이", "한국소설"],
  "tags": ["힐링", "성장"],
  "liked_book_ids": [1088, 3310]
}
```

### 4-3. user_memories

취향 기억 추출 결과를 BE가 저장한다. 취향 프로필 생성 요청에 최대 500건까지 전달하며, 초과 시 최근 500건만 사용한다. 요청에는 type, value, vector, dim만 싣는다.

**이 테이블만 방향이 왕복이다.** AI가 만들어(⑤) BE가 저장하고 다시 요청 본문으로 되돌아온다. 원본은 BE에만 있고 AI DB에는 남지 않는다(§7). `user_memories`는 **요청 본문의 모양을 부르는 이름**이며 BE 쪽 실제 테이블명은 다를 수 있다(기능정의는 `preference_memory`).

| 필드명 | 타입 | Null 허용 | 설명 | 필요한 이유 | 타입 근거 |
| --- | --- | --- | --- | --- | --- |
| user_id | int | N | 사용자 ID | 프로필 생성 요청이 해당 사용자의 기억 전체를 전달 | 커머스 정수 ID. AI DB에 users가 없어 FK가 아니다 |
| type | enum | N | mood, topic, author, condition | tag_weights의 type 집계에 사용. 작가 취향은 이 경로로만 유입 | 값이 넷으로 고정된 분류라 enum |
| value | string | N | 취향 내용 한두 문장 | 추출 요청에 기존 취향으로 전달해 중복 추출을 차단 | 짧은 자연어 문장 |
| confidence | float | N | 신뢰도 0-1 | 호출자의 임계 판정용. 0.5 미만은 반환하지 않음 | 0-1 연속값이라 실수 |
| vector | float[] | N | value 문장의 임베딩 벡터 | centroid 재료. 프로필 생성 시 재임베딩 없이 그대로 사용 | 임베딩 API 출력을 그대로 보관. 요청 본문에는 JSON 실수 배열로 실린다 |
| dim | int | N | 벡터 길이 | AI 서버의 인덱스 차원과 불일치하면 400으로 거절 | 차원 수 비교라 정수 |
| source_conversation_id | string | N | 출처 대화 세션 ID | 같은 세션을 재처리할 때 이 ID로 이전 결과를 교체 | 세션 식별 문자열 |

업로드 이미지는 테이블이 아니다. 챗봇 이미지 턴이 Pre-signed URL로 접근만 하며 AI 서버는 저장하지 않는다. 장바구니, 주문, 재고, 배송은 테이블이 아니라 tool 호출로만 오간다. **도서 정보도 tool(`book.detail`)로 조회한다** — 순위 계산은 복제본 `v_books`를 읽고, 쇼핑 에이전트가 가격·재고를 수치 근거로 쓸 때는 tool로 원본을 확인한다.

## 5. AI가 만들어 BE로 내보내는 것

**복제는 BE MySQL → AI Postgres 한 방향뿐이다.** 반대 방향으로도 데이터는 흐르지만 그것은 복제가 아니라 **응답 본문**과 **tool 호출**이며, 둘 다 BE가 검증한 뒤 자기 테이블에 쓴다. AI DB의 행이 BE DB로 넘어가는 채널은 없다.

| 무엇 | 어느 API | 나가는 경로 | BE가 저장하나 | AI DB에 남나 |
| --- | --- | --- | --- | --- |
| 취향 기억 `extractions[]` (type, value, confidence, vector, dim, source_conversation_id) | ⑤ | 응답 본문 | 저장한다. 취향 테이블에 한 행씩 | 남지 않는다 |
| 카드와 긴 이유 `reason_long` | ③ | 응답 본문 | 저장한다. 상세 페이지에서 그대로 쓴다 | 남지 않는다 |
| 답변 문장 `reply`와 갱신된 `spec` | ③ | 응답 본문 | (V2) 대화 스레드로 보관한다. V1은 클라이언트가 왕복시킨다 | 남지 않는다 |
| 장바구니 변경 `cart.add`, `cart.update` | ⑦ | tool 호출 | 저장한다. 재고와 권한을 다시 확인한 뒤 | 남지 않는다 |
| tool 파생 멱등 키 `{idempotency_key}:{tool_call_index}` | ⑦ | tool 호출 인자 | 저장한다. 중복 실행 차단에 쓴다 | 턴 전체의 본문 키만 idempotency_records에 남는다 |
| 커서 `next_cursor` | ①④ | 응답 본문 | 저장하지 않는다. 다음 요청에 그대로 돌아온다 | 남지 않는다. 서명으로 검증한다 |
| 순위와 점수 `rank`, `match_score`, `reason_short`, `match_basis` | ③(전부) · ④(`match_score`만) | 응답 본문 | 화면에 쓰고 지나간다. ③은 카드와 함께 보관한다 | 남지 않는다 |
| `recognition`, `resolved_reference`, `selection`, `tool_calls[]` | ③⑦ | 응답 본문 | 저장하지 않는다. 그 턴의 화면과 로그에만 쓴다 | 남지 않는다 |
| 프로필 상태 `cold_start`, `profile_version` | ⑥ | 응답 본문 | 저장하지 않는다 | taste_profile에 원본이 남는다 |
| 벡터 `vectors[]`, `dim`, `model` | ② | 내부 전용이라 BE로 나가지 않는다 | 해당 없음 | 적재 파이프라인이 book_embeddings에 저장한다. 사전 벡터는 메모리에만 둔다 |

**역방향 복제를 두지 않는 이유**

- **테이블마다 writer가 하나다.** 양쪽이 같은 행을 쓰면 충돌 해결 규칙이 필요해지고, 그 규칙이 틀리면 주문·재고가 조용히 덮인다.
- **AI가 만든 값이 BE 검증을 우회하지 않는다.** ⑦이 장바구니를 DB에 직접 쓰지 않고 tool을 부르는 이유와 같다 — 재고와 권한은 BE가 다시 확인해야 한다.

**V2 대화 스레드는 복제 대상이 아니다.** 복제하면 대화 원문이 AI DB에 상주해 "AI 서버는 대화를 저장하지 않는다"가 깨진다. 복제 대상은 §3의 다섯 개뿐이다.

## 6. API별 접근 테이블

권한 부여 범위와 장애 전파 범위 확인용이다. 조회 대상은 **전부 AI PostgreSQL 안에 있다** — BE 장애가 조회 실패로 오지 않고 복제가 멈추면 값이 갱신되지 않을 뿐이다. "AI DB 기록"은 AI Postgres에 행이 남는 것만 뜻하며, BE로 나가 BE가 저장하는 것은 §5에 있다.

| API | 조회 | AI DB 기록 | 미저장 |
| --- | --- | --- | --- |
| AI 검색 | book_embeddings, v_books(키워드 인덱스 포함), v_book_popularity | 없음 | 검색어, 커서(서명 문자열로 검증) |
| 텍스트 임베딩 | 없음 | 없음. 응답의 벡터를 적재 파이프라인이 book_embeddings에 upsert한다 | 입력 텍스트 |
| 챗봇 추천 | taste_profile, book_embeddings, v_books, v_book_popularity, 이력 3종(computed_at 이후분) | 없음 | 대화 원문, 추천 조건, 카드와 이유 문장, 이미지 |
| 홈 피드 | taste_profile, book_embeddings, v_books, v_book_popularity, 이력 3종(computed_at 이후분) | 없음 | 결과 목록, 커서 |
| 취향 기억 추출 | 요청의 대화와 기존 취향 | 없음. 저장은 BE | 대화 |
| 취향 프로필 생성 | book_embeddings, v_books(이력 책의 카테고리), 이력 3종, 메모리의 카테고리·태그 사전 벡터, 요청의 온보딩·기억 | taste_profile, idempotency_records | 온보딩 원본, 취향 기억, 이력 |
| 쇼핑 에이전트 | 커머스 상태는 tool 조회. 후보 채점(`recommendations.candidates`)은 피드와 같은 taste_profile, book_embeddings, v_books, v_book_popularity, 이력 3종 | idempotency_records | 대화, tool 결과 |
| 서버 상태 점검 | 구성 요소 상태 | 없음 | 없음 |

## 7. 미확정 항목

명세에 저장 위치나 규칙이 정해지지 않은 항목이다. 값·주기처럼 구현하면서 정할 수 있는 것은 넣지 않았다.

**이전 판에서 미확정이었다가 정해진 것.**

- **`v_books`의 NOT NULL 제약**(2026-09-18, #8) — 카탈로그 실측과 어긋나 적재가 실패하던 문제. `author`·`publisher`·`cover_url`은 **Null 허용**으로 바꾸고(실측 1.7% · 4건 · 85.8%가 빈 값), `in_stock`은 **NOT NULL 유지**한다(`002_v_books_nullable.sql`).

- **사전 임베딩 벡터** — 테이블이 아니다. 온보딩 택소노미의 카테고리·태그 라벨(수십 개)을 서버 기동 시 ②로 임베딩해 메모리에 둔다. ⑥은 요청 시점에 ②를 부르지 않으므로 "임베딩 업스트림 무호출, 503·504 없음" 계약이 유지된다. 모델이나 택소노미가 바뀌면 재기동으로 갱신되고, 인스턴스마다 같은 모델·같은 라벨에서 계산하므로 값이 같다. 벡터가 없는 라벨은 즉석 임베딩하지 않고 태그 신호로만 쓴다.
- **키워드 검색 인덱스** — AI Postgres 안의 tsvector·pg_trgm 인덱스이며 `v_books`(§3-1)에 건다.

| 항목 | 현재 상태 | 필요한 결정 | BE 협의 |
| --- | --- | --- | --- |
| 벡터 차원 N | 잠정 `multilingual-e5-small`(384차원)로 운영하고, `bge-m3`(1024차원)와 실제 카탈로그의 recall@10을 비교해 최종 결정한다(8단계 회고). 명세 예시의 `dim: 1024`는 자리표시자다 | 차원 확정. pgvector `vector(N)`은 DDL에 박히므로 정해지기 전에는 `book_embeddings`·`taste_profile`을 만들 수 없다. 잠정값으로 먼저 만들면 모델 교체 시 **두 테이블 전량 재생성**이 필요하다(메모리 사전 벡터는 재기동으로 갱신). 이 중 `taste_profile`은 AI 혼자 재생성할 수 없다 — 온보딩 원본과 기억을 AI가 저장하지 않으므로 **BE가 전 사용자에 대해 ⑥을 다시 호출**해야 하고, 그 본문의 `memories[].vector`는 BE가 옛 모델로 보관한 값이라 `dim` 불일치로 400이 난다. 차원이 같은 교체(파인튜닝 등)는 400도 나지 않고 옛 기억 벡터·centroid가 새 도서 벡터와 조용히 섞인다 — `dim` 검사만으로는 막을 수 없다. ②는 내부 전용이라 BE가 재임베딩할 수도 없다. 모델 교체 시 기억 벡터를 다시 만드는 경로가 필요하다(아래 user_memories 왕복 행과 함께 결정) | **필요.** 전 사용자 ⑥ 재호출 절차와 기억 벡터 재임베딩 경로 |
| 멱등 키 키 공간 | 프로필 생성과 쇼핑 에이전트가 같은 규약 사용 | 두 API의 키 충돌 가능 여부. 충돌 시 잘못된 저장 응답 반환 또는 정상 요청 409 발생 | **필요.** 키 생성 규칙(prefix 등) 합의 |
| liked_book_ids 절단 기준 | 프로필 생성은 앞 50개, 공통 규약은 최근 N개로 기술이 상충 | 앞 50개인지 최근 50개인지 확정 | **필요.** 전송 순서 확인 |
| 증분 복제 가능 조건 | 복제 수단이 미정. §3의 두 전제(행 단위 upsert·delete, 인덱스 보존) 안에서 정해야 한다 | BE 원본에 행 변경 시각과 삭제 표현이 노출되는지. FK cascade가 동작하려면 **하드 삭제가 사본에 delete로 전달**돼야 한다. 소프트 삭제(비공개 플래그)면 `v_books`에 행이 남아 cascade가 걸리지 않고 비공개 도서가 검색·추천에 계속 노출되므로, 상태 컬럼을 복제해 AI가 거를지 결정 | **필요.** 원본 스키마 확인 |
| 복제 수단·주기 | 미정. 수단은 계약이 아니지만 위 전제 안에서 골라야 한다 | CDC / 변경 시각 기반 upsert·delete 중 택일, 주기 | **필요.** BE·AI·클라우드 3자 |
| 복제 범위 | 도서 외에 이력 3종·인기 집계까지 복제해 달라고 회신했고 클라우드 답변을 기다린다(7단계) | 다섯 테이블 전부 복제되는지 확정 | **필요.** 클라우드 회신 |
| 시각 컬럼의 시간대 | 이 문서는 UTC `timestamptz`로 규정했으나 BE 원본 타입은 미확인 | 원본이 `DATETIME`(시간대 없음)인지 `TIMESTAMP`(내부 UTC)인지. 어긋나면 `computed_at` 비교가 오류 없이 틀린다 | **필요.** 원본 타입 확인 |
| 복제 시 값 변환 규칙 | `price`, `category`의 원본 타입·표현이 미확인 | `price`가 `DECIMAL`일 때의 정수 변환 기준, `category`의 NFC 정규화와 공백 처리 책임 | **필요.** 원본 타입과 정규화 책임 |
| 복제 대상 컬럼 변경 통보 | 규정 없음 | 컬럼명·타입·의미 변경과 삭제 시 사전 통보 절차. 통보 없이 바뀌면 복제가 끊기는데 AI는 오류를 내지 않아 발견이 늦다 | **필요.** 통보 창구 |
| 탈퇴 사용자 취향 프로필 삭제 경로 | 삭제 의무는 §2-2에 정했고 경로가 없다 | 전용 엔드포인트, BE의 탈퇴 목록 전달, AI 자체 정리 중 무엇으로 할지 | **필요.** AI는 탈퇴 사실을 스스로 알 수 없다 |
| user_memories 왕복 | ⑤가 추출해 BE가 저장하고 ⑥ 호출 때 최대 500건을 벡터째로 되싣는다 | 지금대로 왕복할지, 복제 대상에 넣고 ⑥은 user_id만 받을지, 벡터를 빼고 ⑥이 재임베딩할지. 마이페이지 기억 삭제 요구는 현행을 지지하지만, 모델 교체 시 기억 벡터가 전부 무효가 되는 문제(벡터 차원 N 행)는 재임베딩 안을 지지한다 | **필요.** ⑥ 요청 본문 상한, 모델 교체 시 경로 |
| v_book_popularity 갱신 주기 | BE가 원본을 주기적으로 갱신, as_of로 신선도 판별 | 집계 주기와 `sales`의 집계 창 | **필요.** 주기 공유 |

**후보로만 적어 두는 것.** 테이블이 되면 AI 소유가 늘어난다. 5단계 RAG의 조각 저장소 `book_passage`와 그 조인 키 `v_books.isbn13`, 리뷰 본문 복제 `v_book_reviews`(RAG를 도입할 때 함께. 지금은 임베딩으로 우선 진행), `book_cover_embedding`(V2+, 표지 이미지 벡터. 5·6단계의 재평가 트리거가 관측될 때만), 임베딩 응답 캐시(6단계, Redis와 텍스트 해시 테이블 중 미정).