---
wiki: CLD-4 요청 분산 구조의 한계와 개선
type: design
group: cld-4
owner: 미정
status: 작성중
updated: 2026-09-21
sources:
  - 2.2.-요청-분산-구조의-한계와-개선.md
order: 5
---
**요약** Nginx 단일 운영의 한계와 ALB 기반 요청 분산 구조를 정리한다.

## 2.2. 요청 분산 구조의 한계와 개선

#### 1. V1의 Nginx 단일 운영

V1에서는 App EC2의 Nginx가 외부 요청을 받는 유일한 진입점이다.
```text
사용자
  ↓
Nginx
  ↓
Backend
```

Nginx와 Backend가 같은 EC2에 있으므로 EC2 장애, Nginx 재시작, 배포 오류가 로그인·검색·리뷰·장바구니·주문·결제 전체의 중단으로 이어진다. Backend를 여러 대로 늘려도 요청을 나누고 비정상 서버를 제외할 진입점이 없다.

V1의 Peak는 0.18 RPS였지만 V2의 평상시 Peak는 625 RPS이다. 평상시 Peak가 `T`분 동안 중단되면 최대 `625 × 60 × T`건의 API 요청이 영향을 받을 수 있다. 단일 Nginx 장애가 발생하면 더 많은 사용자의 탐색과 구매 흐름이 동시에 중단될 수 있으므로 외부 진입점의 이중화가 필요하다.

#### 2. Nginx 다중 운영의 한계

서로 다른 AZ의 App EC2에서 Nginx를 실행하면 EC2 한 대의 장애에는 대응할 수 있다. 하지만 Nginx 앞에서 요청을 분산하고, 장애를 감지해 다른 Nginx로 전환하는 구성을 별도로 운영해야 한다.
```text
사용자
  ↓ 별도 진입점
  ├─ AZ A · Nginx · Backend
  └─ AZ C · Nginx · Backend
```

이 구성에서는 다음 항목을 직접 관리해야 한다.

- Nginx 앞단의 요청 분산과 장애 전환
- 서버별 인증서·설정 동기화
- Backend 주소 목록 관리
- Nginx 배포와 장애 복구

Nginx를 여러 대 실행하는 것만으로는 진입점의 고가용성이 확보되지 않는다. 별도 진입점을 추가하면 관리 대상과 장애 지점도 늘어난다.

#### 3. ALB 선택 이유

ALB는 외부 요청을 받아 여러 AZ의 App Target으로 분산하고, Health Check에 실패한 Target을 요청 대상에서 제외하는 관리형 진입점이다. [Application Load Balancer 개요](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/introduction.html) [Target Health Check](https://docs.aws.amazon.com/elasticloadbalancing/latest/application/target-group-health-checks.html)
```arduino
사용자
  ↓ HTTPS
ALB
  ├─ AZ A · App EC2 · Backend Task
  └─ AZ C · App EC2 · Backend Task
```

북적북적의 요청은 검색 결과 확인, 도서 상세·리뷰 조회, 장바구니와 주문·결제로 이어진다. 이 흐름에서 특정 Target이 장애 상태인데도 요청이 전달되면 사용자는 다음 화면으로 이동하지 못하거나 결제 결과를 다시 확인해야 한다. ALB가 비정상 Target을 요청 경로에서 제외하면 사용자의 다음 요청을 정상 Target으로 전달할 수 있어, 단일 서버 장애가 전체 구매 흐름의 중단으로 확대되는 것을 줄일 수 있다.

ALB를 선택하는 이유는 다음과 같다.

1. **요청 분산**: 평상시 Peak 625 RPS를 여러 App Target에 나눠 단일 EC2가 전체 트래픽을 처리하지 않도록 한다.
2. **장애 격리**: Health Check에 실패한 Target을 자동으로 제외해 장애 Task로 요청이 전달되지 않도록 한다.
3. **확장 연동**: ECS Service와 ASG가 추가·교체한 Target을 별도의 Backend 주소 수정 없이 요청 경로에 포함할 수 있다.
4. **HTTPS 처리**: ALB에서 HTTPS를 종료해 각 App EC2의 인증서와 Nginx 설정을 직접 동기화하지 않아도 된다.
5. **운영 부담 감소**: 인프라 담당자가 개발을 병행하는 팀 상황에서 Nginx 앞단의 장애 전환과 Target 목록 관리를 직접 구현하지 않아도 된다.

ALB만 배치한다고 가용성이 확보되는 것은 아니다. Target은 서로 다른 AZ에 배치해야 하며, ECS Service의 최소 Task 수는 한 AZ 장애 후에도 남은 Target이 요청을 처리할 수 있도록 설정해야 한다. ALB는 장애 Target을 제외할 수 있지만, 남은 Target의 처리 용량까지 자동으로 보장하지는 않으므로 Task 수 산정은 2.1 문서의 사이징 결과를 적용한다.

#### 4. Nginx 제거 여부

Frontend는 S3와 CloudFront에서 제공하고 Backend는 컨테이너로 실행하므로, Nginx의 URL 재작성·정적 파일 제공·복잡한 캐시 설정이 없다면 ALB 뒤에 Nginx를 추가할 필요가 없다.
```text
사용자
  ↓ HTTPS
ALB
  ↓
Backend Task
```

ALB와 Nginx를 함께 두면 Reverse Proxy 계층과 설정 관리 대상이 늘어난다. 따라서 다음 기능을 실제로 사용하는지 확인한 뒤, 필요하지 않으면 Nginx를 제거한다.

- URL·Header 재작성
- 이미지 업로드 크기 제한
- WebSocket·장시간 연결
- Rate Limit·IP 접근 제어
- 정적 파일 제공·캐시

대체 기능이 없는 필수 설정이 있다면 Nginx를 유지하되 ALB와 역할을 분리한다.

#### 5. 비용과 서비스 관점의 선택

단일 Nginx는 ALB보다 직접 비용이 낮을 수 있다. 하지만 V2에서는 Nginx를 Multi-AZ로 운영하기 위한 추가 EC2, 앞단의 장애 전환 구성, 인증서·설정 동기화와 장애 대응 작업이 필요하다.

App 진입점이 중단되면 정적 Frontend는 제공할 수 있어도 로그인·검색·추천·리뷰·장바구니·주문·결제 API는 사용할 수 없다. 북적북적은 추천 결과를 확인한 뒤 바로 구매하는 흐름이므로, 진입점 장애는 단순 조회 실패가 아니라 구매 기회 손실로 이어진다.

7년 차 목표 매출은 월 1억 5천만 원이므로 월 단순 환산 매출은 다음과 같다.
```text
분당 기대 매출
= 150,000,000원 ÷ (30일 × 24시간 × 60분)
= 약 3,472원/분
```

실제 장애 손실은 장애 중 구매하지 못한 사용자, 결제 재시도 실패와 복구 후 이탈까지 포함하므로 단순 분당 매출보다 커질 수 있다. 따라서 V2에서는 ALB의 추가 비용을 부담하고, 서로 다른 AZ의 Backend Target으로 요청을 분산하며 비정상 Target을 자동 제외하는 구성을 선택한다. Nginx는 고유 기능이 없다는 확인 후 제거한다.

#### 6. 최종 구성
```arduino
사용자
  ↓ HTTPS
ALB
  ├─ AZ A · ECS on EC2 · Backend Task
  └─ AZ C · ECS on EC2 · Backend Task
```

- ALB는 최소 2개 AZ에 연결한다.
- Backend Task는 서로 다른 AZ에 배치한다.
- Target Group Health Check는 실제 요청 처리 가능 상태를 확인한다.
- Health Check에 실패한 Target은 요청 대상에서 제외한다.
- ECS Service와 ASG가 부족한 Task·EC2를 보충한다.
- 평상시에는 Backend Task 4개를 실행하고 AZ별 2개씩 배치한다.
- 한 AZ 장애 후에는 정상 AZ의 Task 2개가 요청을 처리하며, 계산상 운영 기준 용량은 약 336 RPS이다.

