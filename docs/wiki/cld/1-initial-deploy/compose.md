---
wiki: CLD-1 Docker Compose 배포 구조 설계
type: design
group: cld-1
owner: 미정
status: 작성중
updated: 2026-09-17
sources:
  - 3-Docker-Compose-기반-배포-구조-설계.md
order: 5
---
**요약** App EC2와 AI EC2에서 각각 별도의 Docker Compose를 실행하며, Network·Port 분리, EBS 기반 데이터 저장, ECR·Git Commit SHA 기반 이미지 태깅을 정의한다.

## 1. 외부 진입 경로 및 컨테이너 간 통신

V1에서는 Frontend를 S3·CloudFront로 제공하고, App EC2와 AI EC2에서 각각 별도의 Docker Compose를 실행한다.

```text
Frontend: 사용자 → CloudFront → S3
일반 기능: 사용자 브라우저 → Nginx → Backend → MySQL
추천 기능: Backend → AI Server → Qdrant
```

사용자는 Nginx를 통해서만 Backend에 접근한다. Backend와 MySQL은 App EC2의 Docker Network를 통해 통신하며, Backend와 AI Server는 VPC 내부에서 통신한다.

## 2. EC2 및 Docker Compose 구성

| 서버 | 구성요소 | Compose 파일 |
|---|---|---|
| App EC2 | Nginx, Backend, MySQL | `compose.app.yml` |
| AI EC2 | AI Server, Qdrant | `compose.ai.yml` |

두 서버는 `t3.small`과 gp3 30GB를 사용한다.

App EC2에는 고정된 외부 진입 주소를 위해 Elastic IP를 사용한다. AI EC2는 외부에 직접 공개하지 않으며 App EC2와 VPC 내부에서 통신한다.

### App EC2

<details>
<summary><strong>compose.app.yml</strong></summary>

```yaml
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

services:
  nginx:
    image: nginx:1.31.5-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - /var/www/certbot:/var/www/certbot:ro
    networks:
      - web_net
    depends_on:
      - backend
    logging: *default-logging

  backend:
    image: "${BACKEND_IMAGE:?BACKEND_IMAGE를 설정해야 합니다}"
    restart: unless-stopped
    expose:
      - "8080"
    environment:
      SPRING_PROFILES_ACTIVE: production
      SERVER_PORT: "8080"
      SPRING_DATASOURCE_URL: "jdbc:mysql://mysql:3306/${MYSQL_DATABASE}?serverTimezone=Asia/Seoul&characterEncoding=UTF-8"
      SPRING_DATASOURCE_USERNAME: "${MYSQL_USER}"
      SPRING_DATASOURCE_PASSWORD: "${MYSQL_PASSWORD}"
      AI_SERVER_URL: "${AI_SERVER_URL}"
      JWT_SECRET: "${JWT_SECRET}"
      CORS_ALLOWED_ORIGIN: "${CORS_ALLOWED_ORIGIN}"
      JAVA_TOOL_OPTIONS: "${JAVA_TOOL_OPTIONS}"
      TZ: Asia/Seoul
    networks:
      - web_net
      - db_net
    depends_on:
      mysql:
        condition: service_healthy
    logging: *default-logging

  mysql:
    image: mysql:8.4.11
    restart: unless-stopped
    expose:
      - "3306"
    environment:
      MYSQL_DATABASE: "${MYSQL_DATABASE}"
      MYSQL_USER: "${MYSQL_USER}"
      MYSQL_PASSWORD: "${MYSQL_PASSWORD}"
      MYSQL_ROOT_PASSWORD: "${MYSQL_ROOT_PASSWORD}"
      TZ: Asia/Seoul
    volumes:
      - mysql_data:/var/lib/mysql
    networks:
      - db_net
    healthcheck:
      test:
        - CMD-SHELL
        - 'mysqladmin ping -h 127.0.0.1 -u root --password="$${MYSQL_ROOT_PASSWORD}" --silent'
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 40s
    logging: *default-logging

volumes:
  mysql_data:

networks:
  web_net:
    driver: bridge
  db_net:
    driver: bridge
    internal: true
```

</details>

### AI EC2

<details>
<summary><strong>compose.ai.yml</strong></summary>

```yaml
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

services:
  ai-server:
    image: "${AI_IMAGE:?AI_IMAGE를 설정해야 합니다}"
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      HOST: "0.0.0.0"
      PORT: "8000"
      QDRANT_URL: "http://qdrant:6333"
      QDRANT_COLLECTION: "${QDRANT_COLLECTION}"
      EMBEDDING_MODEL: "${EMBEDDING_MODEL}"
      WORKER_COUNT: "${WORKER_COUNT}"
      EXTERNAL_AI_API_KEY: "${EXTERNAL_AI_API_KEY}"
      TZ: Asia/Seoul
    networks:
      - ai_net
    depends_on:
      qdrant:
        condition: service_started
    logging: *default-logging

  qdrant:
    image: qdrant/qdrant:v1.18.2
    restart: unless-stopped
    expose:
      - "6333"
    volumes:
      - qdrant_data:/qdrant/storage
    networks:
      - ai_net
    logging: *default-logging

volumes:
  qdrant_data:

networks:
  ai_net:
    driver: bridge
```

</details>

## 3. Frontend 배포

React 빌드 결과물을 S3에 업로드하고 CloudFront를 통해 제공한다.

## 4. Docker Network

### App EC2

- `web_net`: Nginx ↔ Backend
- `db_net`: Backend ↔ MySQL
- Backend만 `web_net`과 `db_net`에 모두 연결
- `db_net`은 외부와 분리된 내부 네트워크로 구성

### AI EC2

- `ai_net`: AI Server ↔ Qdrant

## 5. Port 구성

| 구성요소 | 포트 | 접근 범위 |
|---|---:|---|
| Nginx | 80, 443 | 전체 사용자 |
| Backend | 8080 | `web_net` 내부 |
| MySQL | 3306 | `db_net` 내부 |
| AI Server | 8000 | App EC2에서만 접근 |
| Qdrant | 6333 | `ai_net` 내부 |
| SSH | 22 | 관리자 IP에서만 접근 |

MySQL과 Qdrant는 호스트 포트를 외부에 공개하지 않는다.

AI Server의 8000번 포트는 App EC2에서 들어오는 요청만 허용한다.

## 6. 데이터 저장

MySQL과 Qdrant 데이터는 Docker Volume을 통해 EC2에 연결된 EBS에 저장한다.

| 구성요소 | Docker Volume | 컨테이너 경로 |
|---|---|---|
| MySQL | `mysql_data` | `/var/lib/mysql` |
| Qdrant | `qdrant_data` | `/qdrant/storage` |

컨테이너가 삭제되더라도 데이터는 Docker Volume에 유지된다.

## 7. 환경변수 및 Secret 관리

서비스 실행에 필요한 설정은 `.env.production` 파일로 관리한다.

| 구성요소 | 주요 환경변수 |
|---|---|
| Backend | DB 주소, DB 이름, AI Server 주소 |
| AI Server | Qdrant 주소, 임베딩 모델, Worker 수 |
| Frontend | API 기본 주소 |

V1은 빠르게 배포해야 하므로 별도의 Secret 관리 서비스는 사용하지 않는다.

대신 `.env.production` 파일은 각 EC2에만 저장하고 Git 저장소와 Docker 이미지에는 포함하지 않는다.

## 8. Restart 및 Healthcheck

- 모든 컨테이너에 `restart: unless-stopped` 적용
- EC2 부팅 시 Docker가 자동으로 시작되도록 설정
- MySQL에 Healthcheck 적용
- Backend와 AI Server의 별도 Healthcheck 및 자동 복구 구성은 초기 POC에서 제외
- EC2 자체 장애는 자동 복구하지 않고 운영자가 직접 대응

## 9. Image Registry 및 Tag 정책

Backend와 AI Server 이미지는 AWS ECR에 저장한다.

두 EC2에는 ECR 이미지를 내려받을 수 있는 공통 읽기 권한을 부여한다.

Nginx, MySQL, Qdrant는 `latest`가 아닌 고정된 버전의 공식 이미지를 사용한다.

Backend와 AI Server 이미지에는 Git Commit SHA를 태그로 사용한다.

```text
backend:abc1234
ai:abc1234
```

이전 배포 이미지를 일부 유지하여 문제가 발생하면 직전 버전으로 되돌릴 수 있도록 구성한다.

태그 불변성, Digest 고정 및 자동 이미지 정리 정책은 빠른 배포를 위해 초기 POC에서 제외한다.

<img alt="아키텍처2" src="https://github.com/user-attachments/assets/79bcdcd4-10ae-4a0e-ab98-74fe23e0a782" />
