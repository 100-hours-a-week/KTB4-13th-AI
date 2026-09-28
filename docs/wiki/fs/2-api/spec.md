---
wiki: FS-2 API 명세
type: spec
group: fs-2
owner: 김세훈
status: 작성중
updated: 2026-09-28
sources:
  - "[2단계]-API-명세서.md"
---
**요약** 도메인 12묶음, 엔드포인트 40개로 구성한 북적북적 풀스택 API 명세다.

- [Notion API 명세서](https://app.notion.com/p/3d5c174f9e7f8026be26cb360e8e1768?v=02dc174f9e7f83628d96084050756dbd&source=copy_link)

## 공통 응답

모든 API 응답을 다음 세 필드를 가진 `ApiResponse`로 정의한다.

```text
{ result, data, error }
```

## 도메인별 API

### 인증 · 3개

<details>
<summary><code>POST /api/v1/auth/{providerType}/login</code> — 소셜 인증 코드로 로그인 세션 생성</summary>

OAuth provider idToken으로 로그인한다. 권한: 공개

</details>

<details>
<summary><code>POST /api/v1/auth/reissue</code> — 토큰 재발급</summary>

Refresh Token으로 Access Token을 재발급한다. 권한: 공개

</details>

<details>
<summary><code>POST /api/v1/auth/logout</code> — 로그아웃</summary>

현재 사용자의 Refresh Token을 폐기하고 로그아웃한다. 권한: 회원

</details>

### 회원 · 3개

<details>
<summary><code>GET /api/v1/users/me</code> — 내 정보 조회</summary>

유저의 프로필을 조회한다. 권한: 회원

</details>

<details>
<summary><code>PATCH /api/v1/users/me</code> — 내 정보 수정</summary>

유저의 프로필을 수정한다. 권한: 회원

</details>

<details>
<summary><code>DELETE /api/v1/users/me</code> — 회원 탈퇴 처리</summary>

유저의 프로필을 삭제한다. 권한: 회원

</details>

### 온보딩 · 4개

<details>
<summary><code>GET /api/v1/onboarding/questions</code> — 질문과 선택지 조회</summary>

원본에 정의되지 않음. 권한: 회원

</details>

<details>
<summary><code>GET /api/v1/users/me/onboarding</code> — 내 온보딩 상태와 답변 조회</summary>

원본에 정의되지 않음. 권한: 회원

</details>

<details>
<summary><code>PUT /api/v1/users/me/onboarding/answers/{questionId}</code> — 질문별 답변 저장 및 수정</summary>

원본에 정의되지 않음. 권한: 회원

**입력**

헤더
| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| Authorization | string | Y | Bearer {accessToken}. JWT 토큰 |

경로
| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| questionId | number | Y | 답변을 등록하거나 수정할 온보딩 질문 식별자 |

본문
| 필드 | 타입 | 필수 | 설명 |
|---|---|---|---|
| optionIds | array | Y | 해당 질문에서 선택한 선택지 식별자 목록 |

**출력** `200 OK`

```json
{
  "result": "SUCCESS",
  "data": {
    "questionId": 1,
    "optionIds": [1, 2]
  },
  "error": null
}
```

| 필드 | 타입 | NULL | 설명 |
|---|---|---|---|
| questionId | number | NOT NULL | 저장된 질문 식별자 |
| optionIds | array | NOT NULL | 저장된 선택지 식별자 목록 |

**에러**

```json
{
  "result": "ERROR",
  "data": null,
  "error": {
    "code": "E500",
    "message": "알 수 없는 오류가 발생했습니다.",
    "data": null
  }
}
```

| 코드 | HTTP 코드 | 이름 | 메시지 |
|---|---:|---|---|
| E400 | 400 | INVALID_REQUEST | 선택 개수 또는 요청 값이 올바르지 않습니다. |
| E401 | 401 | UNAUTHORIZED | 인증 정보가 유효하지 않습니다. |
| E404 | 404 | NOT_FOUND | 질문 또는 선택지를 찾을 수 없습니다. |
| E409 | 409 | CONFLICT | 해당 질문에 속하지 않는 선택지가 포함되어 있습니다. |
| E500 | 500 | DEFAULT_ERROR | 알 수 없는 오류가 발생했습니다. |

</details>

<details>
<summary><code>PUT /api/v1/users/me/onboarding/books</code> — 마지막 단계 도서 선택 저장 및 수정</summary>

원본에 정의되지 않음. 권한: 회원

</details>

### 상품 · 3개

<details>
<summary><code>GET /api/v1/items</code> — 상품 목록 조회</summary>

카테고리에 속한 상품을 cursor 방식으로 조회한다. 권한: 공개

</details>

<details>
<summary><code>GET /api/v1/items/{itemId}</code> — 상품 상세 조회</summary>

원본에 정의되지 않음. 권한: 공개

</details>

<details>
<summary><code>GET /api/v1/categories</code> — 상품 카테고리 목록 조회</summary>

상품 카테고리의 목록을 조회한다. 권한: 공개

</details>

### 배송지 · 5개

<details>
<summary><code>GET /api/v1/addresses</code> — 내 배송지 목록 조회</summary>

유저의 배송지 목록을 조회한다. 권한: 회원

</details>

<details>
<summary><code>POST /api/v1/addresses</code> — 배송지 등록</summary>

유저의 배송지 목록에 배송지를 등록한다. 권한: 회원

</details>

<details>
<summary><code>POST /api/v1/addresses/{addressId}/default</code> — 기본 배송지 설정</summary>

배송지를 기본 배송지로 설정한다. 권한: 회원

</details>

<details>
<summary><code>PATCH /api/v1/addresses/{addressId}</code> — 배송지 수정</summary>

배송지 수정에서 전달된 필드만 갱신한다. 권한: 회원

</details>

<details>
<summary><code>DELETE /api/v1/addresses/{addressId}</code> — 배송지 삭제</summary>

배송지를 삭제한다. 권한: 회원

</details>

### 장바구니 · 5개

<details>
<summary><code>GET /api/v1/cart</code> — 내 장바구니 조회</summary>

유저의 장바구니에 담긴 상품을 조회한다. 권한: 회원

</details>

<details>
<summary><code>POST /api/v1/cart/items</code> — 장바구니 상품 추가</summary>

유저가 상품을 장바구니에 추가한다. 권한: 회원

</details>

<details>
<summary><code>PATCH /api/v1/cart/items/{cartItemId}</code> — 장바구니 상품 수량 변경</summary>

장바구니 상품의 수량을 변경한다. 권한: 회원

</details>

<details>
<summary><code>DELETE /api/v1/cart/items/{cartItemId}</code> — 장바구니 상품 삭제</summary>

장바구니에서 특정 상품을 삭제한다. 권한: 회원

</details>

<details>
<summary><code>DELETE /api/v1/cart/items</code> — 장바구니 상품 다건 삭제</summary>

장바구니에서 선택한 상품들을 삭제한다. 권한: 회원

</details>

### 주문 · 5개

<details>
<summary><code>POST /api/v1/orders</code> — 주문 생성</summary>

상품 옵션과 수량 목록으로 주문을 생성하고 주문 키를 반환한다. 권한: 회원

</details>

<details>
<summary><code>POST /api/v1/cart-orders</code> — 장바구니 기반 주문 생성</summary>

선택한 장바구니 상품들로 주문을 생성한다. 권한: 회원

</details>

<details>
<summary><code>GET /api/v1/orders/{orderKey}/checkout</code> — 주문의 결제 정보 조회</summary>

주문 금액, 주문 상품, 사용 가능한 쿠폰과 포인트를 결제 화면에 필요한 형태로 조회한다. 권한: 회원

</details>

<details>
<summary><code>GET /api/v1/orders</code> — 내 주문 목록 조회</summary>

유저의 주문 이력을 조회한다. 권한: 회원

</details>

<details>
<summary><code>GET /api/v1/orders/{orderKey}</code> — 내 주문 상세 조회</summary>

결제가 완료된 주문의 상세 정보와 주문 상품을 조회한다. 권한: 회원

</details>

### 결제 · 3개

<details>
<summary><code>POST /api/v1/payments</code> — 결제 생성</summary>

원본에 정의되지 않음. 권한: 회원

</details>

<details>
<summary><code>POST /api/v1/payments/{paymentKey}/cancel</code> — 결제 전액 취소</summary>

주문 전체를 취소한다. 권한: 회원

</details>

<details>
<summary><code>POST /api/v1/payments/{paymentKey}/partial-cancel</code> — 결제 부분 취소</summary>

주문의 일부 품목을 취소한다. 권한: 회원

</details>

### 쿠폰 · 2개

<details>
<summary><code>POST /api/v1/coupons/{couponId}/issue</code> — 쿠폰 발급</summary>

쿠폰을 현재 유저에게 발급한다. 권한: 회원

</details>

<details>
<summary><code>GET /api/v1/coupons/me</code> — 내 보유 쿠폰 조회</summary>

유저가 보유한 사용 가능한 쿠폰 목록을 조회한다. 권한: 회원

</details>

### 포인트 · 1개

<details>
<summary><code>GET /api/v1/points</code> — 내 포인트 잔액 조회</summary>

유저의 포인트 잔액과 변동 이력을 조회한다. 권한: 회원

</details>

### 리뷰 · 4개

<details>
<summary><code>GET /api/v1/reviews</code> — 상품 리뷰 목록 조회</summary>

대상 상품의 리뷰를 cursor 방식으로 조회한다. 권한: 공개

</details>

<details>
<summary><code>POST /api/v1/reviews</code> — 리뷰 작성</summary>

주문 이력이 있는 상품에 리뷰와 이미지를 등록한다. 권한: 회원

</details>

<details>
<summary><code>PATCH /api/v1/reviews/{reviewId}</code> — 리뷰 수정</summary>

원본에 정의되지 않음. 권한: 회원

</details>

<details>
<summary><code>DELETE /api/v1/reviews/{reviewId}</code> — 리뷰 삭제</summary>

작성한 리뷰를 삭제한다. 권한: 회원

</details>

### 알림 · 2개

<details>
<summary><code>GET /api/v1/notifications</code> — 내 알림 목록</summary>

원본에 정의되지 않음. 권한: 회원

</details>

<details>
<summary><code>PATCH /api/v1/notifications/{notificationId}</code> — 알림 읽음 처리</summary>

원본에 정의되지 않음. 권한: 회원

</details>
