---
wiki: FS-1 테이블 명세
type: spec
group: fs-1
owner: 김세훈
status: 작성중
updated: 2026-09-28
sources:
  - "[1단계]-테이블-명세서.md"
---
**요약** 도메인 6묶음, 테이블 28개의 컬럼 정의와 설계 근거를 담은 ERD 테이블 명세다.

북적북적 ERD 및 테이블 정의서

ERDCloud와 테이블 정의 내용을 GitHub Wiki에서 읽기 쉽도록 Markdown과 HTML details 구조로 정리한 문서이다.

ERD: [ERDCloud](https://www.erdcloud.com/d/p5GP8brvicLfrNGQz)

## 설계 근거 및 향후 확장성

- 회원,소셜 계정의 활성 데이터는 `deleted_at`과 `active_flag`를 기준으로 관리하고, 활성 데이터에만 UNIQUE를 적용해 소프트 삭제 후 재가입,재연동을 허용
    - 실제 DDL에서는 `active_flag`를 STORED 생성 컬럼으로 도입 → InnoDB는 `VIRTUAL` 생성 컬럼에도 `UNIQUE` 인덱스를 지원하지만 → 메모리 여유가 있고 성능과 조회 효율이 중요하기 때문에 `STORED`
- UNIQUE는 비즈니스상 중복을 막기 위해 사용
  - → 장바구니 상품, 사용자 온보딩,답변, 소셜 계정, 상품,카테고리 연결, 리뷰 파일 연결, 쿠폰 대상 매핑
- `files`와 `review_file`을 분리해 파일 저장 메타데이터와 리뷰 연결을 분리
  - → `content_type`과 공통 파일 연결 구조를 사용하므로 동영상이 추가되어도 가능한 확장성 유지
- `order_addresses`는 주문 당시 배송지를 보존하는 스냅샷
  - → `point`와 `point_history`는 빠른 잔액 조회와 변경 이력 보존을 위해 사용

## 타입 선택 기준

- 금액과 포인트: 소수 금액을 둘째 자리까지 저장하고 FLOAT, DOUBLE의 근사값 오차를 피하기 위해 DECIMAL 사용
- 생성,수정,완료,만료 시각:
    - TIMESTAMP의 2038년 상한을 피하기 위해 DATETIME(6) 사용
    - 출간일처럼 날짜만 필요하면 DATE 사용

## 도메인별 테이블

### 회원·온보딩 · 6개

<details>
<summary><code>users</code> · 유저</summary>

**설명**

회원의 서비스 식별 정보와 계정 생명주기를 관리

**컬럼 설명**

- `id`: 유저 레코드를 고유하게 식별하는 기본 키
- `nickname`: 한글 닉네임. 최대 20자 제한을 반영한 VARCHAR(20), 최소 2자는 별도 입력 검증
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL
- `active_flag`: deleted_at이 NULL이면 1, 삭제되면 NULL. 실제 MySQL DDL에서는 VIRTUAL 생성 컬럼으로 도입하고 `(nickname, active_flag)` UNIQUE로 활성 닉네임 중복을 방지

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| nickname | VARCHAR(20) | NOT NULL | - | - | (nickname, active_flag) |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |
| deleted_at | DATETIME(6) | NULL | - | - | - |
| active_flag | TINYINT GENERATED ALWAYS AS (CASE WHEN deleted_at IS NULL THEN 1 ELSE NULL END) STORED | NULL | - | - | (nickname, active_flag) |

</details>

<details>
<summary><code>user_providers</code> · 유저 소셜 로그인 계정</summary>

**설명**

회원과 외부 소셜 로그인 계정을 연결

**컬럼 설명**

- `id`: 유저 소셜 로그인 계정 레코드를 고유하게 식별하는 기본 키
- `user_id`: 사용자와 소셜 로그인 계정의 연관관계, 유저 삭제 시 연관 계정 삭제를 위한 FK
- `provider_type`: 소셜 로그인 제공자 확장을 고려. `(provider_type, provider_user_id, active_flag)` UNIQUE로 동일 외부 계정의 활성 연결 중복을 방지
- `provider_user_id`: 소셜 로그인 제공자 발급 사용자 식별자
- `provider_email`: 소셜 로그인 제공자가 전달한 이메일. 주소 문자열을 저장하는 VARCHAR(254) 사용. 미제공 시 NULL이라는 설명과 표의 NOT NULL 일치 여부 확인 필요
    - https://www.rfc-editor.org/info/rfc5321/#section-4.5.3.1.2
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL
- `active_flag`: deleted_at이 NULL이면 1, 삭제되면 NULL

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| provider_type | VARCHAR(50) | NOT NULL | - | - | (provider_type, provider_user_id, active_flag) |
| provider_user_id | VARCHAR(255) | NOT NULL | - | - | (provider_type, provider_user_id, active_flag) |
| provider_email | VARCHAR(254) | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |
| deleted_at | DATETIME(6) | NULL | - | - | - |
| active_flag | TINYINT GENERATED ALWAYS AS (CASE WHEN deleted_at IS NULL THEN 1 ELSE NULL END) STORED | NULL | - | - | (provider_type, provider_user_id, active_flag) |

</details>

<details>
<summary><code>onboarding_questions</code> · 온보딩 질문</summary>

**설명**

온보딩에서 제시할 질문과 선택 규칙, 표시 순서를 관리

**컬럼 설명**

- `id`: 온보딩 질문 레코드를 고유하게 식별하는 기본 키
- `code`: 서버 식별용 코드(READING_TIME 등), VARCHAR(30)
- `content`: 온보딩 질문. 화면 설계상 최대 33자를 수용하고 여유를 둔 VARCHAR(40) 사용
- `min_selection`: 최소 선택 개수. 작은 정수 개수를 저장하는 TINYINT 사용. 설명상 기본값 1과 표의 기본값 '-' 일치 여부 확인 필요
- `max_selection`: 최대 선택 개수. 작은 정수 개수를 저장하는 TINYINT 사용, NULL이면 제한 없음
- `display_order`: 질문 표시 순서. 작은 정수 순번을 저장하는 TINYINT 사용, 부호 있는 타입 기준 최대 127 이내인지 확인 필요
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| code | VARCHAR(30) | NOT NULL | - | - | - |
| content | VARCHAR(40) | NOT NULL | - | - | - |
| min_selection | TINYINT | NOT NULL | - | - | - |
| max_selection | TINYINT | NULL | - | - | - |
| display_order | TINYINT | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>onboarding_options</code> · 온보딩 질문별 선택지</summary>

**설명**

각 온보딩 질문에서 선택 가능한 항목을 관리

**컬럼 설명**

- `id`: 온보딩 질문별 선택지 레코드를 고유하게 식별하는 기본 키
- `onboarding_questions_id`: 온보딩 선택지 연결 및 질문에 속한 선택지 저장을 위한 FK
- `code`: 서버 식별용 코드(BEFORE_SLEEP, BEST_SELLER 등)
- `content`: 온보딩 선택지 문구. 화면 설계상 최대 16자를 수용하고 여유를 둔 VARCHAR(30) 사용
- `display_order`: 선택지 표시 순서
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| onboarding_questions_id | BIGINT | NOT NULL | FK | - | - |
| code | VARCHAR(30) | NOT NULL | - | - | - |
| content | VARCHAR(30) | NOT NULL | - | - | - |
| display_order | TINYINT | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>user_onboardings</code> · 유저 온보딩 상태</summary>

**설명**

사용자별 온보딩 진행 상태와 완료 여부를 관리

**컬럼 설명**

- `id`: 유저 온보딩 상태 레코드를 고유하게 식별하는 기본 키
- `user_id`: 사용자별 온보딩 상태 연결. `(user_id, active_flag)` UNIQUE로 사용자당 활성 온보딩 상태를 한 건으로 유지
- `onboarding_status`: IN_PROGRESS, COMPLETED, SKIPPED 등 온보딩 진행 상태
- `completed_at`: 업무 완료 시각, 미완료 시 NULL
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL
- `active_flag`: deleted_at이 NULL이면 1, 삭제되면 NULL

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | (user_id, active_flag) |
| onboarding_status | VARCHAR(20) | NOT NULL | - | - | - |
| completed_at | DATETIME(6) | NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |
| deleted_at | DATETIME(6) | NULL | - | - | - |
| active_flag | TINYINT GENERATED ALWAYS AS (CASE WHEN deleted_at IS NULL THEN 1 ELSE NULL END) STORED | NULL | - | - | (user_id, active_flag) |

</details>

<details>
<summary><code>user_onboarding_answers</code> · 유저가 선택한 답변</summary>

**설명**

사용자가 선택한 온보딩 옵션을 기록

**컬럼 설명**

- `id`: 유저가 선택한 답변 레코드를 고유하게 식별하는 기본 키
- `user_id`: 온보딩 답변 사용자 연결 및 존재하는 사용자에 대한 답변 저장을 위한 FK. `(user_id, onboarding_option_id)` UNIQUE로 같은 선택지 답변의 중복 저장을 방지
- `onboarding_option_id`: 선택 온보딩 옵션 연결
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | (user_id, onboarding_option_id) |
| onboarding_option_id | BIGINT | NOT NULL | FK | - | (user_id, onboarding_option_id) |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

### 도서·상품·개인화 · 6개

<details>
<summary><code>books</code> · 도서</summary>

**설명**

도서 자체의 서지 정보를 관리

**컬럼 설명**

- `id`: 도서 레코드를 고유하게 식별하는 기본 키
- `isbn`: 도서 식별 번호. 앞자리 0과 하이픈을 보존할 수 있도록 문자열로 저장. 외부 API 확정 후 길이와 관련 서지 필드 재검토
- `title`: 도서 제목
- `author`: 저자
- `description`: 도서 설명
- `publisher`: 출판사명
- `published_at`: 출간일. 시각 없이 연,월,일만 필요하므로 DATE 사용
- `cover_image_url`: 표지 이미지 주소
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| isbn | VARCHAR(20) | NULL | - | - | - |
| title | VARCHAR(255) | NOT NULL | - | - | - |
| author | VARCHAR(255) | NOT NULL | - | - | - |
| description | VARCHAR(255) | NOT NULL | - | - | - |
| publisher | VARCHAR(255) | NOT NULL | - | - | - |
| published_at | DATE | NOT NULL | - | - | - |
| cover_image_url | VARCHAR(255) | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |
| deleted_at | DATETIME(6) | NULL | - | - | - |

</details>

<details>
<summary><code>items</code> · 상품</summary>

**설명**

판매 가능한 상품과 가격, 재고, 판매 상태를 관리

**컬럼 설명**

- `id`: 상품 레코드를 고유하게 식별하는 기본 키
- `book_id`: 상품의 도서 소속 관계 및 존재하는 도서만 참조하는 FK
- `name`: 상품명
- `thumbnail_url` : 상품 이미지 url
- `sale_price`: 현재 판매 단가.
- `discounted_price` : 할인 적용 후 최종 판매가.
- `cost_price`: 원가(마진, 정산 계산 고려)
- `stock_quantity`: 상품 재고 수량. 100만 부 이상도 저장할 수 있도록 INTEGER 사용
- `status`: 판매, 품절, 판매 금지 등 상품 상태
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| book_id | BIGINT | NULL | FK | - | - |
| name | VARCHAR(255) | NOT NULL | - | - | - |
| thumbnail_url | VARCHAR(255) | NULL | - | - | - |
| sale_price | DECIMAL(19,2) | NOT NULL | - | - | - |
| discounted_price | DECIMAL(19,2) | NOT NULL | - | - | - |
| cost_price | DECIMAL(19,2) | NOT NULL | - | - | - |
| stock_quantity | INTEGER | NOT NULL | - | 0 | - |
| status | VARCHAR(10) | NOT NULL | - | 'ACTIVE' | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |
| deleted_at | DATETIME(6) | NULL | - | - | - |

</details>

<details>
<summary><code>book_category</code> · 도서 카테고리</summary>

**설명**

도서를 분류하는 카테고리와 계층 경로를 관리

**컬럼 설명**

- `id`: 도서 카테고리 레코드를 고유하게 식별하는 기본 키
- `name`: 카테고리명. 데이터상 최대 14자를 수용하고 여유를 둔 VARCHAR(20) 사용
- `path`: 카테고리 계층 경로. 구분자가 포함된 가변 길이 문자열을 저장하는 VARCHAR(255), 최대 경로 길이 확인 필요
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| name | VARCHAR(20) | NOT NULL | - | - | - |
| path | VARCHAR(255) | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |
| deleted_at | DATETIME(6) | NULL | - | - | - |

</details>

<details>
<summary><code>product_category</code> · 상품 카테고리</summary>

**설명**

상품과 카테고리의 다대다 관계를 관리

**컬럼 설명**

- `id`: 상품 카테고리 레코드를 고유하게 식별하는 기본 키
- `category_id`: 연결 카테고리 식별자, `book_category.id` 참조. `(category_id, item_id, active_flag)` UNIQUE로 동일 상품,카테고리의 활성 연결 중복을 방지
- `item_id`: 연결 상품 식별자, `items.id` 참조
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL
- `active_flag`: deleted_at이 NULL이면 1, 삭제되면 NULL

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| category_id | BIGINT | NOT NULL | FK | - | (category_id, item_id, active_flag) |
| item_id | BIGINT | NOT NULL | FK | - | (category_id, item_id, active_flag) |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |
| deleted_at | DATETIME(6) | NULL | - | - | - |
| active_flag | TINYINT GENERATED ALWAYS AS (CASE WHEN deleted_at IS NULL THEN 1 ELSE NULL END) STORED | NULL | - | - | (category_id, item_id, active_flag) |

</details>

<details>
<summary><code>activity_events</code> · 유저 활동 이벤트</summary>

**설명**

사용자의 도서 관련 행동을 이벤트 단위로 기록

**컬럼 설명**

- `id`: 유저 활동 이벤트 레코드를 고유하게 식별하는 기본 키
- `user_id`: 활동 이벤트 발생 사용자 연결 및 사용자별 행동 분석, 추천을 위한 FK
- `book_id`: 활동 대상 도서 연결 및 도서 별 행동 분석, 추천을 위한 FK
- `event_type`: 이벤트 유형
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| book_id | BIGINT | NOT NULL | FK | - | - |
| event_type | VARCHAR(40) | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>memory</code> · 유저 개인화 메모리</summary>

**설명**

사용자별 개인화 문맥과 만료 시점을 관리

**컬럼 설명**

- `id`: 유저 개인화 메모리 레코드를 고유하게 식별하는 기본 키
- `user_id`: 개인화 메모리 소유 사용자 연결 및 사용자별 추천 문맥 분리를 위한 FK
- `content`: 개인화 메모리 내용. 실제 문맥 길이를 수용하는 지는 확인 필요
- `expires_at`: 업무상 만료 시각
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| content | VARCHAR(255) | NOT NULL | - | - | - |
| expires_at | DATETIME(6) | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

### 장바구니·배송 · 4개

<details>
<summary><code>carts</code> · 장바구니</summary>

**설명**

사용자별 장바구니의 소유권과 생명주기를 관리

**컬럼 설명**

- `id`: 장바구니 레코드를 고유하게 식별하는 기본 키
- `user_id`: 장바구니 소유 사용자 연결 및 장바구니 조회를 위한 FK
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>cart_item</code> · 장바구니 상품</summary>

**설명**

장바구니에 담긴 상품과 수량을 관리

**컬럼 설명**

- `id`: 장바구니 상품 레코드를 고유하게 식별하는 기본 키
- `cart_id`: 장바구니 항목 연결 및 장바구니별 상품 목록 관리를 위한 FK. `(cart_id, item_id)` UNIQUE로 동일 장바구니에 같은 상품이 중복 생성되는 것을 방지
- `item_id`: 장바구니 상품 연결
- `quantity`: 장바구니 수량. 최소 2개, 최대 500개를 저장하며 TINYINT 범위를 넘으므로 SMALLINT 사용.
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| cart_id | BIGINT | NOT NULL | FK | - | (cart_id, item_id) |
| item_id | BIGINT | NOT NULL | FK | - | (cart_id, item_id) |
| quantity | SMALLINT | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>addresses</code> · 주소</summary>

**설명**

사용자가 관리하는 배송지 목록을 저장

**컬럼 설명**

- `id`: 주소 레코드를 고유하게 식별하는 기본 키
- `user_id`: 배송지 소유 사용자 연결 및 사용자별 주소 관리를 위한 FK
- `label`: 사용자 식별용 주소 라벨
- `code`: 5자리 우편번호. 앞자리 0을 보존하고 숫자 연산이 필요 없으므로 VARCHAR(5) 사용
- `address`: 배송지 주소. 길이가 달라지는 문자열을 저장하는 VARCHAR(255), 외부 주소 API의 최대 길이 확인 필요
- `detail_address`: 사용자 입력 상세 주소. VARCHAR(255)로 최대 255자 저장, 입력 길이 제한은 화면과 일치 여부 확인 필요
- `is_default`: 기본 주소 여부, 생략 시 일반 주소(FALSE)
- `created_at`: 레코드 생성 시각
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| label | VARCHAR(20) | NOT NULL | - | - | - |
| code | VARCHAR(5) | NOT NULL | - | - | - |
| address | VARCHAR(255) | NOT NULL | - | - | - |
| detail_address | VARCHAR(255) | NULL | - | - | - |
| is_default | BOOLEAN | NOT NULL | - | FALSE | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| deleted_at | DATETIME(6) | NULL | - | - | - |

</details>

<details>
<summary><code>order_addresses</code> · 주문 배송지</summary>

**설명**

주문 시점의 배송지 값을 스냅샷으로 보존
→ 주소 정보가 변경되어도 주문 내역의 배송지 정보는 변경되지 않도록 하기 위해

**컬럼 설명**

- `id`: 주문 배송지 스냅샷 레코드를 고유하게 식별하는 기본 키
- `postal_code`: 주문 시점 우편번호. 앞자리 0을 포함한 5자리 값을 보존하도록 VARCHAR(5) 사용
- `address`: 주문 시점 도로명 주소 스냅샷
- `detail_address`: 주문 시점 상세 주소 스냅샷
- `created_at`: 주문 배송지 스냅샷 생성 일시

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| postal_code | VARCHAR(5) | NOT NULL | - | - | - |
| address | VARCHAR(255) | NOT NULL | - | - | - |
| detail_address | VARCHAR(255) | NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

### 주문·결제 · 5개

<details>
<summary><code>orders</code> · 주문</summary>

**설명**

구매 거래의 상위 단위와 금액, 진행 상태를 관리

**컬럼 설명**

- `id`: 주문 레코드를 고유하게 식별하는 기본 키
- `user_id`: 주문자 연결 및 사용자별 주문 이력 조회를 위한 FK
- `key`: 외부 노출용 비즈니스 주문번호
- `total_price`: 할인,포인트,배송비 반영 후 최종 결제 예정 금액.
- `status`: 주문 진행 상태 CREATED, PAID, PARTIAL_CANCELED, CANCELED
- `paid_at`: 결제 완료 시각, 미결제 시 NULL
- `canceled_at`: 취소 완료 시각, 미취소 시 NULL
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각
- `address_id`: 주문 시점 배송지 스냅샷 연결, `order_addresses.id` 참조. 사용자 주소 변경,삭제와 무관하게 주문 배송지를 보존

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| key | VARCHAR(255) | NULL | - | - | UNIQUE(key) |
| total_price | DECIMAL(19,2) | NOT NULL | - | - | - |
| status | VARCHAR(20) | NOT NULL | - | 'CREATED' | - |
| paid_at | DATETIME(6) | NULL | - | - | - |
| canceled_at | DATETIME(6) | NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |
| address_id | BIGINT | NOT NULL | FK | - | - |

</details>

<details>
<summary><code>order_item</code> · 주문 아이템</summary>

**설명**

주문에 포함된 상품별 수량, 가격, 상태를 저장

**컬럼 설명**

- `id`: 주문 아이템 레코드를 고유하게 식별하는 기본 키
- `order_id`: 주문 항목 연결 및 주문별 상품 구성 관리를 위한 FK
- `item_id`: 주문 항목의 판매 상품 연결 및 구매 상품 식별을 위한 FK
- `unit_price`: 주문 시점 상품 단가. 이후 판매 가격이 바뀌어도 당시 단가를 보존.
- `total_price`: 주문 금액. 소수 금액을 둘째 자리까지 정확히 저장하기 위해 DECIMAL(19,2) 사용.
- `quantity`: 주문 수량. 소수 단위가 없는 개수를 저장하므로 INTEGER 사용
- `status`: 품목 진행 상태 CREATED, PAID, CANCELED
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| order_id | BIGINT | NOT NULL | FK | - | - |
| item_id | BIGINT | NOT NULL | FK | - | - |
| unit_price | DECIMAL(19,2) | NOT NULL | - | - | - |
| total_price | DECIMAL(19,2) | NOT NULL | - | - | - |
| quantity | INTEGER | NOT NULL | - | - | - |
| status | VARCHAR(20) | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>payments</code> · 결제</summary>

**설명**

주문에 대한 결제 요청, 승인 결과와 할인 수단을 관리

**컬럼 설명**

- `id`: 결제 레코드를 고유하게 식별하는 기본 키
- `order_id`: 결제 대상 주문 연결 및 주문당 결제 레코드가 정확히 하나인 정책
- `owned_coupon_id`: 사용한 보유 쿠폰 아이디; `owned_coupon.id` 참조
- `status`: 결제 상태 READY, SUCCESS. DEFAULT 'READY'로 결제 준비 상태에서 시작
- `origin_amount`: 결제대행사 요청 금액. 요청할 금액의 소수 자릿수를 보존하도록 DECIMAL(19,2) 사용
- `coupon_discount`: 쿠폰 할인 금액. 소수 할인액을 둘째 자리까지 저장하도록 DECIMAL(19,2) 사용
- `used_point`: 사용한 포인트. 포인트 잔액과 동일한 소수 자릿수를 유지하도록 DECIMAL(19,2) 사용
- `paid_amount`: 실제 승인 결제 금액. 요청 금액과 같은 정밀도로 비교하도록 DECIMAL(19,2) 사용
- `method`: 결제 수단
- `paid_at`: 결제 완료 시각, 미결제 시 NULL
- `created_at`: 결제 생성 일시
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| order_id | BIGINT | NOT NULL | FK | - | (order_id) |
| owned_coupon_id | BIGINT | NULL | FK | - | - |
| status | VARCHAR(20) | NOT NULL | - | 'READY' | - |
| origin_amount | DECIMAL(19,2) | NOT NULL | - | - | - |
| coupon_discount | DECIMAL(19,2) | NULL | - | - | - |
| used_point | DECIMAL(19,2) | NOT NULL | - | - | - |
| paid_amount | DECIMAL(19,2) | NULL | - | - | - |
| method | VARCHAR(30) | NULL | - | - | - |
| paid_at | DATETIME(6) | NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>cancel</code> · 주문 취소/환불 이력</summary>

**설명**

주문 또는 주문 품목의 취소, 환불 이력을 건별로 기록

**컬럼 설명**

- `id`: 주문 취소/환불 이력 레코드를 고유하게 식별하는 기본 키
- `order_id`: 주문 아이디. `orders.id` 참조
- `order_item_id`: 주문 아이템 아이디. `order_item.id` 참조
- `payment_id`: 결제 아이디. `payments.id` 참조
- `user_id`: 취소 요청 사용자 아이디. `users.id` 참조
- `type`: 취소 유형(ALL: 전체 취소, PARTIAL: 부분 취소)
- `canceled_quantity`: 취소 수량. 소수 단위가 없는 개수를 저장하므로 INTEGER 사용
- `origin_amount`: 결제 당시 원금액. 원본 결제와 동일한 DECIMAL(19,2)로 당시 금액 보존
- `paid_amount`: 결제 당시 실결제액. 원본 결제와 동일한 DECIMAL(19,2)로 승인 금액 보존
- `used_point`: 결제 당시 사용 포인트. 원본 포인트 사용액과 같은 DECIMAL(19,2)로 보존
- `coupon_discount`: 결제 당시 쿠폰 할인액. 원본 할인액과 같은 DECIMAL(19,2)로 보존
- `canceled_paid_amount`: 이번 취소의 결제 환불액. 부분 환불액의 소수 자릿수를 보존하도록 DECIMAL(19,2) 사용
- `canceled_point_amount`: 이번 취소의 반환 포인트. 포인트 잔액과 동일한 소수 자릿수로 기록
- `canceled_coupon_amount`: 이번 취소의 쿠폰 할인 금액. 원본 할인액과 같은 DECIMAL(19,2)로 기록
- `canceled_at`: 취소 완료 시각, 미취소 시 NULL
- `created_at`: 취소 이력 생성 일시
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| order_id | BIGINT | NOT NULL | FK | - | - |
| order_item_id | BIGINT | NOT NULL | FK | - | - |
| payment_id | BIGINT | NOT NULL | FK | - | - |
| user_id | BIGINT | NOT NULL | FK | - | - |
| type | VARCHAR(20) | NOT NULL | - | - | - |
| canceled_quantity | INTEGER | NOT NULL | - | - | - |
| origin_amount | DECIMAL(19,2) | NOT NULL | - | - | - |
| paid_amount | DECIMAL(19,2) | NOT NULL | - | - | - |
| used_point | DECIMAL(19,2) | NULL | - | - | - |
| coupon_discount | DECIMAL(19,2) | NULL | - | - | - |
| canceled_paid_amount | DECIMAL(19,2) | NOT NULL | - | - | - |
| canceled_point_amount | DECIMAL(19,2) | NOT NULL | - | - | - |
| canceled_coupon_amount | DECIMAL(19,2) | NOT NULL | - | - | - |
| canceled_at | DATETIME(6) | NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>cancel_balance</code> · 취소 가능 잔액 원장</summary>

**설명**

주문별 추가 취소 가능 잔액과 누적 취소액을 관리

**컬럼 설명**

- `id`: 취소 가능 잔액 원장 레코드를 고유하게 식별하는 기본 키
- `order_id`: 대상 주문, `orders.id` 연결. `(order_id)` UNIQUE로 주문별 취소 가능 잔액을 단일 원장으로 관리
- `cancellable_paid_amount`: 추가 환불 가능한 결제액
- `cancellable_point_amount`: 추가 반환 가능한 포인트
- `cancellable_coupon_amount`: 추가 취소 가능한 쿠폰 할인액
- `canceled_paid_amount`: 환불된 결제액 누계. 원본 취소 이력과 같은 DECIMAL(19,2) 사용
- `canceled_point_amount`: 반환된 포인트 누계. 원본 취소 이력과 같은 DECIMAL(19,2) 사용
- `canceled_coupon_amount`: 취소된 쿠폰 할인액 누계. 원본 취소 이력과 같은 DECIMAL(19,2) 사용
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| order_id | BIGINT | NOT NULL | FK | - | (order_id) |
| cancellable_paid_amount | DECIMAL(19,2) | NOT NULL | - | 0.00 | - |
| cancellable_point_amount | DECIMAL(19,2) | NOT NULL | - | 0.00 | - |
| cancellable_coupon_amount | DECIMAL(19,2) | NOT NULL | - | 0.00 | - |
| canceled_paid_amount | DECIMAL(19,2) | NOT NULL | - | 0.00 | - |
| canceled_point_amount | DECIMAL(19,2) | NOT NULL | - | 0.00 | - |
| canceled_coupon_amount | DECIMAL(19,2) | NOT NULL | - | 0.00 | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

### 쿠폰·포인트 · 5개

<details>
<summary><code>coupon</code> · 쿠폰</summary>

**설명**

쿠폰의 할인 방식, 금액, 사용 조건, 유효 기간을 정의

**컬럼 설명**

- `id`: 쿠폰 레코드를 고유하게 식별하는 기본 키
- `name`: 사용자 표시용 쿠폰 이름
- `type`: 할인 방식, FIXED_AMOUNT: 정액, PERCENT_RATE: 정률
- `discount`: 정액 할인 금액 또는 정률 할인율. 소수 할인액과 12.50 같은 할인율을 저장하도록 DECIMAL(19,2) 사용. 정률은 0 초과 100 이하로 별도 검증
- `min_order_amount`: 쿠폰 사용에 필요한 최소 주문 금액. 주문 금액과 같은 DECIMAL(19,2)로 비교, 제한 없음은 0
- `max_discount_amount`: 정률 쿠폰의 최대 할인 금액. 계산된 할인액과 같은 DECIMAL(19,2)로 상한 비교
- `max_use_count`: 쿠폰의 최대 사용 가능 횟수
- `expired_at`: 쿠폰 유효기간 만료 시각
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| name | VARCHAR(255) | NOT NULL | - | - | - |
| type | VARCHAR(20) | NOT NULL | - | - | - |
| discount | DECIMAL(19,2) | NOT NULL | - | - | - |
| min_order_amount | DECIMAL(19,2) | NOT NULL | - | 0.00 | - |
| max_discount_amount | DECIMAL(19,2) | NOT NULL | - | - | - |
| max_use_count | INTEGER | NOT NULL | - | - | - |
| expired_at | DATETIME(6) | NOT NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>owned_coupon</code> · 유저 보유 쿠폰</summary>

**설명**

사용자에게 발급된 쿠폰과 사용 상태, 개별 만료 시점을 관리

**컬럼 설명**

- `id`: 유저 보유 쿠폰 레코드를 고유하게 식별하는 기본 키
- `user_id`: 쿠폰 보유 사용자, `users.id` 연결
- `coupon_id`: 발급 원본 쿠폰 정책, `coupon.id` 연결
- `status`: 보유 쿠폰 업무 상태(AVAILABLE: 사용 가능, USED: 사용 완료, REVOKED: 회수)
- `max_use_count`: 최대 사용 가능 횟수(발급 시점 정책 스냅샷)
- `used_count`: 현재까지 사용한 횟수
- `expires_at`: 개별 만료 일시
- `used_at`: 쿠폰 사용 완료 시각, 미사용 시 NULL
- `created_at`: 레코드 생성 시각, 쿠폰 발급일의 의미를 포함
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| coupon_id | BIGINT | NOT NULL | FK | - | - |
| status | VARCHAR(20) | NOT NULL | - | 'AVAILABLE' | - |
| max_use_count | INTEGER | NOT NULL | - | 0 | - |
| used_count | INTEGER | NOT NULL | - | 0 | - |
| expires_at | DATETIME(6) | NOT NULL | - | - | - |
| used_at | DATETIME(6) | NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>coupon_target</code> · 쿠폰 적용 대상</summary>

**설명**

쿠폰을 적용할 상품 또는 카테고리를 지정

**컬럼 설명**

- `id`: 쿠폰 적용 대상 레코드를 고유하게 식별하는 기본 키
- `coupon_id`: 적용 범위를 정의하는 쿠폰, `coupon.id` 연결
- `target_type`: 적용 대상 유형, ITEM: `items.id`, CATEGORY: `book_category.id`
- `target_id`: target_type별 대상 식별자. `(coupon_id, target_type, target_id)` UNIQUE로 동일 쿠폰의 동일 상품 또는 카테고리 중복 적용을 방지
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| coupon_id | BIGINT | NOT NULL | FK | - | (coupon_id, target_type, target_id) |
| target_type | VARCHAR(20) | NOT NULL | - | - | (coupon_id, target_type, target_id) |
| target_id | BIGINT | NOT NULL | - | - | (coupon_id, target_type, target_id) |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>point</code> · 포인트 잔액</summary>

**설명**

사용자별 현재 사용 가능한 포인트 잔액을 관리

**컬럼 설명**

- `point_id`: 포인트 잔액 레코드를 고유하게 식별하는 기본 키
- `user_id`: 사용자 포인트 잔액 연결. `(user_id)` UNIQUE로 사용자당 현재 잔액 원장을 한 건으로 유지
- `balance`: 현재 사용 가능한 포인트 잔액. 금액과 같은 소수 둘째 자리 단위로 관리하도록 DECIMAL(19,2) 사용
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| point_id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | (user_id) |
| balance | DECIMAL(19,2) | NOT NULL | - | 0.00 | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>point_history</code> · 포인트 변동 이력</summary>

**설명**

포인트의 적립, 사용, 환불, 조정 내역을 기록

**컬럼 설명**

- `id`: 포인트 변동 이력 레코드를 고유하게 식별하는 기본 키
- `user_id`: 사용자 아이디. `users.id` 참조
- `event_type`: 포인트 변동 사유(EARN, USE, REFUND, REVOKE, ADJUST)
- `amount`: 포인트 변동량. 적립은 양수, 사용,회수는 음수로 기록하고 잔액과 같은 DECIMAL(19,2) 사용
- `balance_after`: 포인트 변동 직후 잔액. point.balance와 같은 타입으로 기록해 이력과 현재 잔액의 불일치 확인
- `target_type`: 변동 원인 유형, ORDER, PAYMENT, CANCEL, SYSTEM 등. source_id와 함께 설정하거나 함께 NULL
- `target_id`: 대상 주문, 결제, 취소 등의 식별자
- `idempotency_key`: 중복 적립,사용 방지용 요청 키. `(idempotency_key)` UNIQUE로 동일 요청 재처리로 원장 행이 중복 생성되는 것을 방지
- `created_at`: 포인트 변동 생성 일시

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| event_type | VARCHAR(20) | NOT NULL | - | - | - |
| amount | DECIMAL(19,2) | NOT NULL | - | - | - |
| balance_after | DECIMAL(19,2) | NOT NULL | - | - | - |
| target_type | VARCHAR(20) | NULL | - | - | - |
| target_id | BIGINT | NULL | - | - | - |
| idempotency_key | VARCHAR(255) | NOT NULL | - | - | (idempotency_key) |
| created_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

### 리뷰·알림 · 2개

<details>
<summary><code>reviews</code> · 리뷰</summary>

**설명**

구매한 주문 품목에 대한 평점과 리뷰 내용을 관리

**컬럼 설명**

- `id`: 리뷰 레코드를 고유하게 식별하는 기본 키
- `user_id`: 리뷰 작성자 연결 및 작성자별 리뷰 관리를 위한 FK
- `target_type`: 리뷰 대상 유형(ITEM)
- `target_id`: 리뷰 대상 주문 항목 연결 및 동일 사용자의 동일 주문 항목에 활성 리뷰가 중복 작성되지 않도록 `(user_id, target_id, active_flag)` UNIQUE 적용
- `rate`: 리뷰 평점. 0.0부터 10.0까지 0.1점 단위로 저장하므로 DECIMAL(3,1) 사용. 범위 제한은 CHECK (rating BETWEEN 0.0 AND 10.0)로 별도 적용
- `content`: 리뷰 내용
- `is_spoiler`: 스포일러 포함 여부. 참,거짓을 나타내는 BOOLEAN 사용
- `key`: 리뷰 작성 권한 키 (구매 건 검증 및 중복 작성 방지, 유니크), 구매 확정 의사로 사용.
- `active_flag`: deleted_at이 NULL이면 1, 삭제되면 NULL
- `deleted_at`: 소프트 삭제 시각, 활성 데이터는 NULL
- `created_at`: 레코드 생성 시각
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | (user_id, order_item_id, active_flag), (user_id, key) |
| target_type | VARCHAR(30) | NOT NULL | - | - | - |
| target_id | BIGINT | NOT NULL | - | - | (user_id, order_item_id, active_flag) |
| rate | DECIMAL(3,1) | NULL | - | - | - |
| content | VARCHAR(255) | NULL | - | - | - |
| is_spoiler | BOOLEAN | NOT NULL | - | FALSE | - |
| key | varchar(255) | NOT NULL | - | - | (user_id, key) |
| active_flag | TINYINT GENERATED ALWAYS AS (CASE WHEN deleted_at IS NULL THEN 1 ELSE NULL END) STORED | NULL | - | - | (user_id, order_item_id, active_flag) |
| deleted_at | DATETIME(6) | NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>

<details>
<summary><code>notifications</code> · 알림</summary>

**설명**

사용자에게 전달할 메시지와 클릭 대상을 관리

**컬럼 설명**

- `id`: 알림 레코드를 고유하게 식별하는 기본 키
- `user_id`: 알림 수신자 연결 및 사용자별 알림 조회를 위한 FK
- `type`: 알림 유형
- `title`: 알림 제목
- `message`: 알림 본문
- `target_type`: 알림 클릭 대상 유형, 대상이 없으면 NULL. ITEM, ORDER 등 제한된 유형 코드를 저장
- `target_id`: target_type에 해당하는 도메인 식별자, ITEM: `items.id`, ORDER: `orders.id`
- `target_url`: 알림 클릭 시 이동할 클라이언트 경로, target_type, target_id 기반 경로
- `read_at`: 알림을 읽은 시각, 읽지 않으면 NULL
- `created_at`: 알림 생성 일시
- `updated_at`: 마지막 수정 시각

**컬럼 정의**

| 컬럼 | 타입 | NULL | 키 | DEFAULT | UNIQUE |
|---|---|---|---|---|---|
| id | BIGINT | NOT NULL | PK | - | PK |
| user_id | BIGINT | NOT NULL | FK | - | - |
| type | VARCHAR(30) | NOT NULL | - | - | - |
| title | VARCHAR(50) | NOT NULL | - | - | - |
| message | VARCHAR(255) | NOT NULL | - | - | - |
| target_type | VARCHAR(40) | NULL | - | - | - |
| target_id | BIGINT | NULL | - | - | - |
| target_url | VARCHAR(255) | NULL | - | - | - |
| read_at | DATETIME(6) | NULL | - | - | - |
| created_at | DATETIME(6) | NOT NULL | - | - | - |
| updated_at | DATETIME(6) | NOT NULL | - | - | - |

</details>
