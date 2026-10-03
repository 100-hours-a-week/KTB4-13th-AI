# BE MySQL → AI PostgreSQL 복제

검색·추천용 데이터는 BE MySQL이 원본이고 AI PostgreSQL은 읽기 전용 사본이다.
`bookjeok-replica.timer`가 5분마다 `python -m app.jobs.replicate_be`를 실행한다.

## 복제 대상과 상태 보존

| BE 원본 | AI 사본 | 삭제·취소 처리 |
| --- | --- | --- |
| `books` | `v_books` | `deleted_at`을 그대로 저장하고 검색·임베딩에서 제외 |
| `products` | `v_products` | `deleted_at`과 파생 `status`를 저장하고 가격·재고 조회에서 제외 |
| `orders` + `order_item` | `v_user_purchases` | 주문 상품 상태, 수량, 취소 수량, `deleted_at`을 보존 |
| `cancel` | `v_user_purchases.canceled_quantity` | 테이블이 있으면 상품별 취소 수량을 합산. 현재 Dev처럼 없으면 `CANCELED` 상태를 전량 취소로 해석 |
| `reviews` | `v_user_reviews` | `active_flag`, `deleted_at`을 그대로 보존 |
| `product_popularity_snapshots` | `v_book_popularity` | 변경된 상품의 집계만 upsert |

취소·삭제 행을 원천에서 버리지 않는 **B안**을 사용한다. 복제본에 상태를 보존하면 재처리와
장애 분석이 가능하고, AI 조회에서는 다음 조건으로만 활성 이력을 사용한다.

- 구매: `deleted_at IS NULL`, 상태가 `PAID` 또는 `PARTIAL_CANCELED`, `quantity > canceled_quantity`
- 리뷰: `active_flag = true`, `deleted_at IS NULL`
- 도서·상품: `deleted_at IS NULL`

## 증분 커서와 시간대

각 원본의 `(updated_at, id)` 또는 `(refreshed_at, product_id)`를 복합 커서로 사용한다.
같은 마이크로초에 여러 행이 바뀌어도 빠뜨리지 않으며, 대상에는 행 단위 upsert만 수행한다.
따라서 `v_books`를 TRUNCATE하거나 DROP하지 않아 기존 임베딩과 인덱스를 보존한다.
제목·저자·소개·출판사·분류·삭제 상태가 바뀐 책은 DB 트리거가 해당 책의 기존 임베딩만
삭제한다. 이후 임베딩 배치가 빈 항목을 다시 생성한다.

BE의 `DATETIME`은 계약대로 `Asia/Seoul`의 벽시각으로 해석하고 PostgreSQL `timestamptz`에는
UTC로 변환해 저장한다. 커서를 다시 MySQL에 넘길 때는 KST 벽시각으로 복원한다.

`replication_status.last_succeeded_at`으로 `/health.replication_lag_seconds`를 계산한다.
복제 지연은 관찰 지표이며 API 인스턴스를 트래픽에서 제외하는 기준으로 사용하지 않는다.

## Dev 설치·검증

1. `db/migrations/20261002_replication_state_columns.sql`을 AI PostgreSQL에 적용한다.
2. BE MySQL에 AI 인스턴스만 접속할 수 있는 읽기 전용 사용자를 만든다.
3. `/opt/bookjeok-ai-dev/replica.env`에 `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`,
   `MYSQL_PASSWORD`, `MYSQL_DB`, `REPLICATION_SOURCE_TIMEZONE=Asia/Seoul`을 저장하고 권한을 600으로 둔다.
4. `ops/systemd`의 service/timer를 `/etc/systemd/system`에 설치하고 timer를 활성화한다.
5. `systemctl start bookjeok-replica.service`를 한 번 실행한 뒤 다음을 확인한다.
   - service 종료 코드 0
   - `v_books`/`v_products` 건수가 원본과 일치
   - 취소된 구매와 삭제된 리뷰가 복제본에는 남지만 AI 이력 조회에서는 제외
   - `/health`의 `replication_lag_seconds`가 300초 이내

Dev에서 MySQL root 자격증명 불일치 때문에 전용 계정을 만들 수 없는 경우에 한해 기존
`bookjeok_app` 계정을 임시로 사용할 수 있다. 이 계정은 `bookjeok` 스키마에 쓰기 권한도
있으므로 보안 그룹에서 AI 인스턴스만 허용하고, root 접근을 복구하는 즉시 `SELECT` 전용
계정으로 교체한다. 운영 환경에는 이 예외를 적용하지 않는다.

Rollback은 timer를 중지하고 이전 AI 이미지를 배포하는 것이다. 새 컬럼은 이전 코드가 읽지 않아
애플리케이션 롤백과 호환된다.
