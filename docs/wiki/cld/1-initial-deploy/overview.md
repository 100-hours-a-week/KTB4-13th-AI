---
wiki: CLD-1 초기 배포 설계
type: design
group: cld-1
owner: 미정
status: 작성중
updated: 2026-09-17
sources:
  - 1단계.-EC2---Docker-Compose-기반-초기-배포-설계.md
order: 0
---
**요약** V1 초기 배포는 EC2 2대와 Docker Compose 기반으로 설계하며, 하위 6개 문서에서 규모·인프라·운영 기준·컨테이너·배포 구조·한계를 다룬다.

북적북적의 V1은 빠른 출시를 통해 시장 반응을 확인해야 하는 초기 스타트업 상황을 기준으로 설계한다.

V1은 약 3주간 운영하는 초기 POC이며, 초기 가입자는 약 900명, 일반적인 Peak Traffic은 약 0.18 RPS로 예상한다.

현재 사용자와 트래픽 규모에서는 높은 가용성을 확보하는 것보다 적은 비용과 운영 복잡도로 서비스를 빠르게 배포하는 것이 중요하다.

## 설계 문서

### 1. 초기 서비스 규모 정의

초기 가입자는 약 900명, 일반적인 Peak Traffic은 약 0.18 RPS로 예상한다.

비용과 빠른 구축을 우선하여 AWS를 선택하고, Frontend는 S3 + CloudFront, 서버는 t3.small EC2 2대로 구성한다.

- [1-1. 서비스 규모 정의 및 예상 트래픽](scale-traffic.md)
- [1-2. 인프라 구성 및 선택 근거](infra-rationale.md)
- [1-3. 운영 기준 및 예상 비용](ops-cost.md)

### 2. Docker 컨테이너 구성 설계

Nginx, Backend, MySQL은 App EC2에 배치하고, AI Server와 Qdrant는 AI EC2에 배치한다.

Frontend는 S3 + CloudFront로 제공하며, Redis와 별도 Batch 및 Worker는 초기 구성에서 제외한다.

- [Docker 컨테이너 구성 설계](containers.md)

### 3. Docker Compose 기반 배포 구조 설계

App EC2와 AI EC2에서 각각 별도의 Docker Compose를 실행한다.

컨테이너별 Network와 Port를 분리하고, MySQL과 Qdrant 데이터는 EBS에 저장한다.

애플리케이션 이미지는 ECR에 저장하고 Git Commit SHA로 버전을 관리한다.

- [Docker Compose 기반 배포 구조 설계](compose.md)

### 4. 현재 구조의 한계 정의

현재 구조는 비용이 낮고 빠르게 구축할 수 있지만, EC2 장애가 서비스 장애로 이어질 수 있다.

Auto Scaling과 자동 장애 복구가 어렵고, 서버가 증가하면 Docker Compose만으로 관리하기 어렵다는 한계가 있다.

- [현재 구조의 한계 정의](limits.md)
