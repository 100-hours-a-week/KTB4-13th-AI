---
wiki: CLD-1 Docker 컨테이너 구성 설계
type: design
group: cld-1
owner: 미정
status: 작성중
updated: 2026-09-17
sources:
  - 2-Docker-컨테이너-구성-설계.md
order: 4
---
**요약** V1에서는 Nginx·Backend·MySQL·AI Server·Vector DB를 컨테이너로 실행하고, Frontend·Redis·Batch·Worker는 초기 구성에서 제외한다.

## 1. Docker 사용 이유

- V1을 3주 안에 빠르게 배포해야 하므로 로컬과 서버의 실행 환경 차이를 줄이기 위해 사용한다.
- Frontend, Backend, AI Server의 서로 다른 런타임과 의존성을 분리하여 충돌을 줄인다.
- Docker Compose로 여러 서비스를 한 번에 실행하고 중지할 수 있어 초기 배포와 운영이 단순하다.

## 2. 컨테이너 구성 및 배치

V1은 3주간 운영되는 초기 POC이므로 빠른 배포와 운영 단순화를 우선한다.

Frontend는 S3·CloudFront로 제공하고, 서버 구성요소는 역할에 따라 App EC2와 AI EC2에 배치한다.

| 구성요소 | 컨테이너화 | 배치 | 선택 이유 |
|---|:---:|---|---|
| Frontend | 제외 | S3·CloudFront | React 빌드 결과물은 정적 파일이므로 컨테이너를 실행하지 않고 S3에 저장하여 CloudFront로 제공한다. |
| Nginx | 적용 | App EC2 | HTTPS 처리와 Backend API Reverse Proxy를 담당한다. |
| Backend | 적용 | App EC2 | Spring Boot와 Java의 실행 환경 및 의존성을 고정하여 배포 환경 차이를 줄인다. |
| MySQL | 적용 | App EC2 | 초기 데이터와 트래픽이 적으므로 Backend와 같은 서버에서 운영하여 비용을 줄인다 ¹ |
| AI Server | 적용 | AI EC2 | 프롬프트 처리, 외부 LLM API 호출, 임베딩 생성 및 추천 처리 로직을 Backend와 분리하여 독립적으로 배포한다 ² |
| Vector DB | 적용 | AI EC2 | 임베딩 벡터 저장과 유사도 검색을 담당하므로 AI Server와 함께 배치한다. |
| Redis | 제외 | - | 단일 Backend 환경에서는 분산 세션, 공용 캐시 및 메시지 큐의 필요성이 낮다. |
| Batch·Worker | 제외 | - | 초기 정기 작업은 Spring Scheduler 또는 EC2의 cron으로 처리한다. |

¹ 데이터는 전용 EBS에 저장한다
² GPU는 필요하지 않다

## 3. Database 배치 검토

| 선택지 | 판단 | 근거 |
|---|:---:|---|
| Backend와 같은 EC2의 MySQL 컨테이너 | 선택 | 추가 비용 없이 빠르게 구축할 수 있으며 현재 사용자 규모를 처리하기에 충분하다. |
| 별도 EC2 | 제외 | 세 번째 EC2가 필요하여 비용과 관리 대상이 증가한다. |
| RDS | 제외 | 백업과 장애 복구가 편리하지만 3주 POC에서는 비용과 설정 부담이 상대적으로 크다. |

MySQL과 Vector DB 데이터는 컨테이너 내부가 아닌 EBS에 연결된 Docker Volume에 저장한다.

## 4. 최종 구성

| 배치 위치 | 구성요소 |
|---|---|
| S3·CloudFront | React Frontend |
| App EC2 | Nginx, Spring Boot Backend, MySQL |
| AI EC2 | AI Server, Vector DB |

따라서 V1에서는 **Nginx, Backend, MySQL, AI Server, Vector DB를 컨테이너로 실행**한다.

Frontend는 컨테이너에서 제외하고 S3·CloudFront로 제공하며, Redis와 별도의 Batch·Worker도 초기 구성에서 제외한다.
