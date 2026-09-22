---
wiki: AI-7 CD 구축 정보 가이드
type: guide
group: ai-7
owner: 미정
status: 작성중
updated: 2026-09-22
---
**요약** AI 서버와 PostgreSQL·pgvector를 CD에 연결할 때 자동화 도구와 클라우드 담당자에게 전달할 값과 검증 기준을 정의한다.

## 1. 적용 범위

이 문서는 Amazon ECR의 AI 서버 이미지를 AI EC2에 배포하고 PostgreSQL·pgvector 연결을 검증하는 CD 구축에 적용한다. CD는 GitHub Actions에서 AWS OIDC로 인증하고 SSM Run Command로 EC2의 Docker Compose를 실행한다.

실제 비밀번호·토큰·내부 IP는 문서, GitHub Actions 로그, SSM 명령에 넣지 않는다. V1의 애플리케이션 Secret은 AI EC2의 `.env.production`에만 저장하고 파일 권한을 배포 계정으로 제한한다.

## 2. AI 서버 정보

### 2.1 배포 단위

| 항목 | 확정값 | CD 적용 기준 |
|---|---|---|
| 런타임 | Python 3.12·FastAPI | `uvicorn app.main:app`으로 실행한다 |
| 컨테이너 포트 | `8000` | App EC2에서만 접근을 허용한다 |
| 서버 | AI EC2 1대 | 메모리 4GB 이상·gp3 30GiB를 사용한다 |
| 이미지 | Amazon ECR | SHA 태그로 게시하고 Digest로 배포한다 |
| 배포 방식 | Recreate | `ai-server`만 교체하고 DB는 유지한다 |
| 실행 방식 | Docker Compose | 저장소의 `compose.ai.yml`을 SSM으로 전달한다 |
| 원격 실행 | SSM Run Command | SSH 키와 22번 공개 포트를 사용하지 않는다 |

배포 전 현재 Image Digest와 Compose를 이전 릴리스로 저장한다. 신규 배포가 성공하면 `current-release.env`를 갱신하며 실패하면 이전 Digest와 Compose로 `ai-server`만 복원한다.

### 2.2 환경변수

| 분류 | 변수 | 기준 |
|---|---|---|
| 서버 | `HOST`, `PORT`, `TZ`, `LOG_LEVEL` | `0.0.0.0`, `8000`, `Asia/Seoul`, `INFO` |
| 릴리스 | `RELEASE_SHA` | 배포한 Git 커밋 SHA 7자를 넣는다 |
| DB | `DATABASE_URL`, `DB_POOL_MIN`, `DB_POOL_MAX` | DSN은 Secret이며 풀은 `1`, `10`을 기본값으로 한다 |
| 인증 | `AI_SERVICE_TOKEN`, `CURSOR_SIGNING_KEY` | BE와 공유하거나 모든 AI 인스턴스에 같은 값을 넣는다 |
| 임베딩 | `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `WORKER_COUNT` | `intfloat/multilingual-e5-small`, `384`, `4` |
| LLM | `EXTERNAL_AI_API_KEY`, `LLM_PROVIDER`, `LLM_MODEL_ID` | Provider의 운영값을 넣는다 |
| LLM | `LLM_BASE_URL`, `PROMPT_VERSION`, `LLM_TIMEOUT_SECONDS` | 운영 Endpoint, `v1`, `30` |
| 검증 | `LLM_MOCK` | 통합 검증은 `true`, 운영은 `false` |

`DATABASE_URL`은 `postgresql://<계정>:<비밀번호>@postgres:5432/<DB명>` 형식으로 구성한다. Compose의 `VECTOR_DB_DSN`과 `VECTOR_DB_TABLE`은 현재 애플리케이션이 읽지 않으므로 사용하지 않는다.

### 2.3 배포 성공 기준

`GET /health`가 HTTP `200`이고 `status`가 `ok` 또는 `degraded`이면 애플리케이션 배포를 통과시킨다. `status: down` 또는 HTTP `503`이면 실패로 판정하고 자동 Rollback한다.

현재 LLM 점검은 구현 전이므로 정상 기동 후에도 `degraded`가 나올 수 있다. CD는 DB·벡터 인덱스가 `ok`이고 `version`이 배포 SHA와 같은지 확인한다.

## 3. PostgreSQL·pgvector 정보

| 항목 | 확정값 | 운영 규칙 |
|---|---|---|
| 엔진 | PostgreSQL 17 + pgvector | Qdrant를 배포하지 않는다 |
| 컨테이너 | `pgvector/pgvector:pg17` | 운영 전 정확한 Image Digest를 고정한다 |
| 확장 | `vector`, `pg_trgm` | `001_init.sql`에서 생성한다 |
| 벡터 차원 | `384` | 변경하면 벡터 테이블을 전량 재생성한다 |
| 인덱스 | HNSW·`vector_cosine_ops` | `book_embeddings_hnsw_idx` 유효성을 검사한다 |
| 저장소 | `pgvector_data` | `/var/lib/postgresql/data`를 EBS에 유지한다 |
| 외부 접근 | 없음 | `ai_net` 내부의 `5432`만 사용한다 |
| 시간 타입 | `timestamptz` | 세션 시간대는 `Asia/Seoul`로 설정한다 |

운영 마이그레이션은 번호 순서로 실행하고 실패하면 애플리케이션을 교체하지 않는다. `001_init.sql`과 `002_v_books_nullable.sql`은 운영 대상이며 `003_stg_cat_books.sql`은 개발용 적재 테이블이므로 운영에서 제외한다.

애플리케이션 Rollback은 PostgreSQL 컨테이너, Docker Volume, 마이그레이션을 되돌리지 않는다. 삭제·타입 축소처럼 이전 애플리케이션과 호환되지 않는 변경은 자동 CD에서 실행하지 않는다.

BE MySQL의 도서 데이터는 행 단위 upsert·delete로 AI PostgreSQL에 복제한다. `v_books`를 `TRUNCATE`하거나 재생성하면 `book_embeddings`가 연쇄 삭제되므로 금지한다.

## 4. 입력값과 구축 전 조치

### 4.1 인프라 담당자 입력값

| 저장 위치 | 입력값 | 전달 기준 |
|---|---|---|
| GitHub Variables | `AWS_REGION`, `ECR_REPOSITORY` | 값 존재 여부만 로그에 출력한다 |
| GitHub Variables | `AWS_PUBLISH_ROLE_ARN`, `AWS_CD_ROLE_ARN` | 게시와 배포 권한을 분리한다 |
| GitHub Variables | `AI_EC2_INSTANCE_ID`, `AI_DEPLOY_DIR` | SSM 대상과 Compose 작업 경로다 |
| GitHub Variables | `AI_COMPOSE_FILE`, `AI_SERVICE_NAME` | `compose.ai.yml`, `ai-server`를 기본값으로 한다 |
| GitHub Variables | `AI_HEALTHCHECK_URL` | `http://127.0.0.1:8000/health`를 기본값으로 한다 |
| GitHub Environment | `production` 승인자 | 배포 요청자와 다른 담당자 1명을 지정한다 |
| AI EC2 | `.env.production` | DB 비밀번호와 서비스 토큰을 저장한다 |
| AI EC2 | ECR Pull Instance Role | 대상 AI ECR Repository의 읽기만 허용한다 |
| AI EC2 | SSM Managed Instance | 온라인 상태와 Run Command 권한을 확인한다 |
| 네트워크 | AI EC2 Security Group | App EC2에서 오는 `8000`만 허용한다 |

### 4.2 구축 전 조치

- 게시 OIDC Role은 `main`, 배포 OIDC Role은 `production` Environment로 제한한다
- AI EC2에 Docker Compose V2, AWS CLI, SSM Agent, `flock`과 `curl`을 설치한다
- 기존 AI 서비스가 있으면 최초 CD 전에 실행 중인 Image Digest를 `current-release.env`에 기록한다
- PostgreSQL 컨테이너·Volume과 운영 마이그레이션을 애플리케이션 최초 배포 전에 준비한다
- AI EC2 사양을 `t3.small` 2GB에서 메모리 4GB 이상으로 조정한다
- 클라우드 CD 문서의 Qdrant 표기를 PostgreSQL·pgvector로 교체한다
- PostgreSQL 백업 주기·보존 기간·복구 시험 일정을 확정한다
- BE MySQL에서 AI PostgreSQL로 연결할 복제 방식과 읽기 전용 계정을 확정한다
