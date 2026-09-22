---
wiki: CLD-1 현재 구조의 한계 정의
type: design
group: cld-1
owner: 미정
status: 작성중
updated: 2026-09-17
sources:
  - 4-현재-구조의-한계-정의.md
order: 6
---
**요약** 현재 EC2 2대 + Docker Compose 구조의 장점과 EC2 장애 복구·Healthcheck 범위·데이터 복구·자원 공유·Secret 관리·롤백 범위·이미지 관리의 한계를 정리한다.

## 1. 현재 구조의 장점

- EC2 2대와 Docker Compose만 사용하므로 3주 POC 일정 안에 빠르게 배포할 수 있다.
- 초기에는 저렴하고 단순하게 운영할 수 있다.
- 사용자와 트래픽이 증가하면 EC2 사양을 변경하거나 MySQL을 별도 서버로 분리하여 확장할 수 있다.

## 2. 현재 구조의 한계

### EC2 장애 복구

Docker Compose는 컨테이너 프로세스가 종료되면 컨테이너를 다시 시작할 수 있다.

다만 EC2 자체에 장애가 발생하면 다른 서버에서 컨테이너를 자동으로 실행하지 못하므로 운영자가 직접 복구해야 한다.

### Healthcheck 범위

MySQL에만 Healthcheck를 적용한다.

따라서 Backend, AI Server 및 PostgreSQL (pgvector)의 연결 장애가 발생하면 운영자가 로그를 확인하고 직접 복구해야 한다.

### 데이터 복구

Docker Volume은 같은 EC2에서 컨테이너를 다시 생성할 때만 데이터를 유지한다.

EC2·EBS 장애나 Volume 삭제에 대비하려면 MySQL Dump와 EBS Snapshot을 별도로 관리해야 한다.

### 서버 자원 공유

App EC2에서는 Spring Boot와 MySQL이 2GB 메모리를 공유하고, AI EC2에서는 AI Server와 PostgreSQL (pgvector)이 2GB 메모리를 공유한다.

임베딩 작업이나 데이터베이스 부하가 증가하면 응답 속도 저하 또는 메모리 부족이 발생할 수 있다.

### Secret 관리

비밀번호와 API Key를 `.env.production` 파일로 관리하므로 EC2 접근 권한을 가진 사용자는 해당 값을 확인할 수 있다.

### 롤백 범위

Git Commit SHA를 이용한 이미지 롤백은 Backend와 AI Server 이미지에만 적용된다.

따라서 데이터베이스 변경사항과 Frontend 빌드 파일까지 자동으로 되돌리지는 못한다.

### 이미지 관리

ECR 태그 불변성과 자동 이미지 정리 정책을 적용하지 않으므로 이미지 태그와 불필요한 이미지 삭제를 팀에서 직접 관리해야 한다.
