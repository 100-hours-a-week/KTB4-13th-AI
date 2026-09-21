---
wiki: CLD-2 CI 파이프라인 설계
type: design
group: cld-2
owner: 미정
status: 작성중
updated: 2026-09-21
sources:
  - 2단계.-CI(지속적-통합)-파이프라인-설계.md
order: 0
---
**요약** 세 Repository의 Pull Request 검증과 Backend·AI Server의 `main` Image 생성 정책을 설계한다.

## 2단계 - CI(지속적 통합) 파이프라인 설계

북적북적은 Frontend, Backend, AI Server를 별도의 Repository에서 개발한다.

V1의 개발 속도를 유지하면서 빌드와 배포 실패를 줄이기 위해 Pull Request에서 Lint, Test, Build를 수행한다.

Backend와 AI Server는 `main` Merge 후 Docker Image를 자동으로 생성한다.

## 1. CI 도구 선택 및 필요성

### 1.1. CI 도구 비교

GitHub 연동 여부, 별도 서버 필요 여부, 초기 설정과 운영 부담, AWS 연동, V1 기간의 비용을 기준으로 비교했다.

### GitHub Actions

소스 코드와 Pull Request가 GitHub에 있어 별도 이전이나 연동 작업 없이 바로 사용할 수 있다. Workflow도 Repository에서 함께 관리할 수 있다.
GitHub Issue, Branch 보호 규칙, 리뷰 상태와 연결하기도 쉽다.

### GitLab CI/CD

GitLab Repository 중심의 도구이다. 현재 GitHub 작업 환경을 옮기거나 별도 연동과 권한 설정을 구성해야 한다.
GitLab을 함께 사용하는 조직이라면 저장소와 Pipeline을 한 곳에서 관리할 수 있다는 장점이 있다.

### CircleCI

GitHub와 연동할 수 있지만 별도 서비스의 설정과 권한 관리가 필요하고, 팀이 익숙하지 않아 초기 문제 해결에 시간이 걸릴 수 있다.
병렬 실행과 캐시 기능을 제공하지만 현재 필요한 수준을 넘어서는 별도 운영 요소가 생깁니다.

### Jenkins

자유도가 높지만 Controller와 Runner를 직접 구축하고 업데이트·모니터링해야 한다.
사내망이나 특수한 빌드 환경을 세밀하게 제어해야 할 때 유리하지만 V1에는 과한 구성이다.

### 1.2. GitHub Actions 선택

비교 결과, V1은 별도 플랫폼이나 서버를 추가하지 않고 GitHub Actions를 사용한다. 현재 저장소와 코드 리뷰 흐름을 그대로 활용할 수 있고, 필요한 Lint·Test·Build와 ECR 연동을 한 곳에서 구성할 수 있기 때문이다.

GitHub Actions의 OIDC를 사용하면 ECR Push 때 장기 AWS Access Key를 저장하지 않고 실행 시간에만 유효한 권한을 사용할 수 있다.

### 1.3. Runner 운영 방식

CI는 GitHub에서 제공하는 `ubuntu-latest` Runner에서 실행한다. 별도의 Runner 서버는 운영하지 않다.

GitHub-hosted Runner는 Workflow마다 새로 실행되는 환경이므로 이전 작업의 파일이나 설정이 다음 실행에 남지 않다. Self-hosted Runner를 직접 운영하는 것보다 초기 설정과 보안 관리 부담이 적어 V1에 적합한다.

`ubuntu-latest`에 의존하는 런타임 버전 차이를 줄이기 위해 Workflow에서 필요한 언어와 도구 버전을 명시한다.

### 1.4. CI 도입 목적

CI에서는 Pull Request 단계와 `main` Merge 후 단계에서 각각 필요한 항목을 확인한다.

Pull Request에서는 다음 항목을 확인하여 문제가 있는 코드가 `main`에 반영되는 것을 막다.

- 코드 작성 규칙 위반
- Test 및 Application Build 실패

`main` Merge 후에는 배포에 사용할 Docker Image를 생성하면서 다음 항목을 확인한다.

- Dockerfile·이미지 의존성·빌드 컨텍스트 오류
- 코드와 Docker Image 버전 불일치

### 1.5. CI 보안 기준

GitHub Actions가 ECR에 Docker Image를 Push할 때 **OIDC와 AWS IAM Role**을 사용한다.

OIDC는 Workflow가 실행될 때만 임시 인증정보를 발급하므로 장기 Access Key를 GitHub에 저장하지 않아도 된다.

IAM Role은 Backend와 AI Server Repository의 `main` Workflow에서만 사용하며, 각 ECR Repository에 이미지를 Push하는 데 필요한 권한만 부여한다.

Pull Request에는 AWS 권한을 주지 않고, `GITHUB_TOKEN`은 `contents: read`만 허용한다. 외부 Action은 버전을 지정하여 사용한다.

## 2. CI Trigger 정책 설계

세 Repository의 CI는 독립적으로 실행한다.

한 서비스의 변경이나 실패가 다른 서비스의 검증을 막지 않지만, Repository 간 연동 문제는 하나의 CI에서 확인하기 어렵다.

### 2.1. Branch 구성

추가 장기 Branch는 두지 않고 세 Repository 모두 `main`만 유지한다.

```text
main
 ├─ feature/<이슈번호>-<기능명>
 ├─ fix/<이슈번호>-<수정내용>
 ├─ chore/<이슈번호>-<작업내용>
 └─ hotfix/<이슈번호>-<수정내용>
```

각 Repository의 `main`에는 다음 보호 정책을 적용한다.

- `main` 직접 Push 금지
- Pull Request를 통한 Merge만 허용
- Repository별 필수 CI 통과
- 최소 1명의 리뷰 승인
- Merge 전 최신 `main` 반영
- Merge된 작업 Branch 자동 삭제

### 2.2. Repository별 Trigger

#### Pull Request

`main` 대상 Pull Request가 생성·재오픈되거나 새로운 Commit이 추가되면 CI를 실행한다.

| Repository | 실행 작업 |
|---|---|
| Frontend | Lint → Test → React Build |
| Backend | Lint → Test → Application Build |
| AI Server | Lint → Test |

Merge 전에 코드의 오류와 Build 가능 여부를 확인하여 문제가 있는 코드가 `main`에 반영되는 것을 막다.

#### `main` Push

Pull Request가 Merge되어 `main`이 변경되면 Backend와 AI Server의 Image CI를 실행한다.

| Repository | 실행 작업 |
|---|---|
| Frontend | 실행하지 않음 |
| Backend | OIDC 인증 → Docker Image Build → SHA Tag → ECR Push |
| AI Server | OIDC 인증 → Docker Image Build → SHA Tag → ECR Push |

Backend와 AI Server는 배포에 사용할 Docker Image를 생성하기 위해 실행한다.

Frontend는 Docker Image를 사용하지 않다. PR에서는 Build 가능 여부만 확인하고 실제 Build와 S3 배포는 배포 단계에서 수행한다.

PR에서 Image까지 만들면 Merge되지 않은 Image가 쌓이므로 Image는 `main`에서만 생성한다.

PR에서 검증을 마쳤기 때문에 `main`에서는 Test를 반복하지 않으며, Merge 시점의 상태를 다시 Test하지 않는 한계는 감수한다.

### 2.3. 중복 실행 방지

같은 Pull Request에 새로운 Commit이 추가되면 기존 실행을 취소하고 최신 Commit만 검증한다.

서로 다른 Pull Request는 별도의 실행 그룹으로 구분하여 독립적으로 검증한다.

`main`의 Image Build는 Commit SHA Tag를 사용하므로 이전 실행을 취소하지 않다.

Markdown과 `docs/**` 파일만 변경되면 CI를 실행하지 않다.

## 3. CI Pipeline 설계

### 3.1. CI Pipeline Diagram

<img alt="북적북적 CI Pipeline" src="https://github.com/user-attachments/assets/ef462862-f509-4091-9094-1c6d1ace78a3" />

### 3.2. Lint

문법 오류, 사용하지 않는 코드, 잘못된 import와 코드 규칙 위반을 확인한다. 실패하면 Test와 Build를 실행하지 않다.

### 3.3. Test

변경한 코드가 의도한 대로 동작하고 기존 기능에 영향을 주지 않는지 확인한다. 실패하면 Merge를 차단한다.

### 3.4. Build

실행하거나 배포할 수 있는 결과물이 만들어지는지 확인한다.

- Frontend: 정적 파일 생성
- Backend: 실행 가능한 애플리케이션 파일 생성
- AI Server: 별도의 Application Build 없이 Test 후 Docker Image 생성

의존성이나 컴파일 문제로 Build가 실패하면 Merge를 차단한다.

### 3.5. Docker Image Build

Backend와 AI Server를 배포 가능한 Image로 만듭니다. Commit SHA를 Tag로 지정하여 ECR에 저장하며, 실패하면 다음 배포를 진행하지 않다.

### 3.6. GitHub Actions YAML 예시

각 Repository는 `.github/workflows/ci.yml` 파일 하나로 관리한다. 아래 예시는 Trigger와 Job 구성만 나타냅니다.

<details>
<summary>Frontend ci.yml</summary>

```yaml
name: Frontend CI

on:
  pull_request:
    branches: [main]
    types: [opened, reopened, synchronize]
    paths-ignore: ["**/*.md", "docs/**"]

permissions:
  contents: read

concurrency:
  group: frontend-pr-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  verify:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: <Lint 명령>
      - name: Test
        run: <Test 명령>
      - name: React Build
        run: <Build 명령>
      - name: Discord notification
        if: failure()
        run: <PR 작성자에게 실패 알림 전송>
```

</details>

<details>
<summary>Backend ci.yml</summary>

```yaml
name: Backend CI

on:
  pull_request:
    branches: [main]
    types: [opened, reopened, synchronize]
    paths-ignore: ["**/*.md", "docs/**"]
  push:
    branches: [main]
    paths-ignore: ["**/*.md", "docs/**"]

permissions:
  contents: read

concurrency:
  group: ${{ github.event_name == 'pull_request' && format('backend-pr-{0}', github.event.pull_request.number) || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  verify:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: <Lint 명령>
      - name: Test
        run: <Test 명령>
      - name: Application Build
        run: <Build 명령>
      - name: Discord notification
        if: failure()
        run: <PR 작성자에게 실패 알림 전송>

  image:
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - name: AWS authentication
        uses: aws-actions/configure-aws-credentials@v4
      - name: ECR login
        uses: aws-actions/amazon-ecr-login@v2
      - name: Docker Image Build and Push
        run: <SHA Tag Image Build 및 ECR Push 명령>
      - name: Discord notification
        if: failure()
        run: <Merge한 개발자에게 실패 알림 전송>
```

</details>

<details>
<summary>AI Server ci.yml</summary>

```yaml
name: AI Server CI

on:
  pull_request:
    branches: [main]
    types: [opened, reopened, synchronize]
    paths-ignore: ["**/*.md", "docs/**"]
  push:
    branches: [main]
    paths-ignore: ["**/*.md", "docs/**"]

permissions:
  contents: read

concurrency:
  group: ${{ github.event_name == 'pull_request' && format('ai-pr-{0}', github.event.pull_request.number) || github.run_id }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  verify:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Lint
        run: <Lint 명령>
      - name: Test
        run: <Test 명령>
      - name: Discord notification
        if: failure()
        run: <PR 작성자에게 실패 알림 전송>

  image:
    if: github.event_name == 'push' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - name: AWS authentication
        uses: aws-actions/configure-aws-credentials@v4
      - name: ECR login
        uses: aws-actions/amazon-ecr-login@v2
      - name: Docker Image Build and Push
        run: <SHA Tag Image Build 및 ECR Push 명령>
      - name: Discord notification
        if: failure()
        run: <Merge한 개발자에게 실패 알림 전송>
```

</details>

## 4. Docker Image 관리 전략

### 4.1. Registry 및 Repository

Docker Image Registry는 **AWS ECR**을 사용한다.

Backend와 AI Server가 AWS EC2에서 운영되므로 별도의 Registry를 두지 않다. Image 생성과 저장을 AWS 환경에서 처리해 배포 준비 과정을 줄이다.

ECR Repository는 Backend와 AI Server로 분리한다.

### 4.2. Image Tag

Docker Image에는 Git Commit SHA를 Tag로 사용한다.

Commit SHA를 사용하면 Image를 생성한 코드를 확인할 수 있다.

`latest`, Git Tag 및 Semantic Version은 Docker Image Tag로 사용하지 않다.

### 4.3. Image 보관

V1 운영 기간은 3주이므로 이미지 자동 정리는 적용하지 않다.

운영 기간과 예상 Image 수가 적어 Lifecycle 정책도 적용하지 않다.

## 5. 실패 처리 정책

### 5.1. 단계별 처리

- Lint 실패: Test와 Build를 실행하지 않고 Merge 차단
- Test 실패: Build를 실행하지 않고 Merge 차단
- Application Build 실패: Merge 차단
- Docker Image Build 실패: ECR Push 중단
- ECR Push 실패: Workflow 실패 처리 및 다음 배포 중단

CI가 실패해도 기존 ECR Image와 현재 실행 중인 서비스는 변경되지 않다.

### 5.2. 실패 알림

Pull Request와 `main` CI가 실패하면 담당 개발자에게 Discord 알림을 보냅니다. GitHub Actions를 계속 확인하지 않고 개발에 집중할 수 있도록 하기 위한 정책이다.

PR 실패는 작성자에게, `main` 실패는 Merge한 개발자에게 알립니다. GitHub 사용자명과 Discord 사용자 ID는 `DISCORD_USER_MAP`에 연결하여 해당 개발자만 멘션한다.

Discord Webhook은 GitHub Actions Secrets에 저장한다. 성공 알림은 보내지 않으며, 실패한 Workflow마다 한 번만 알립니다.

### 5.3. 메시지 형식

메시지에는 실패한 Repository와 단계, Commit SHA, 실행 링크와 시각을 표시한다.

```text
⚠️ Backend PR 검증 실패

담당자: @개발자
실패 단계: Test
PR: #42
Commit SHA: 6f2c1ab
실행 링크: https://github.com/.../actions/runs/...
실패 시각: 2026-09-05 14:30 KST
```

```text
🚨 Backend Image 생성 실패

담당자: @개발자
이벤트: main Push
실패 단계: Docker Image Build
Commit SHA: 8d3e2fa
실행 링크: https://github.com/.../actions/runs/...
실패 시각: 2026-09-05 15:10 KST
```

## 6. 현재 CI 구조의 한계

- Frontend, Backend, AI Server의 CI가 Repository별로 실행되므로 서비스 간 연동 오류를 모두 확인하기 어려움
- 이 문제는 CD 단계의 일회성 Docker Compose 통합 검증으로 보완함
- PR에서는 Docker Image를 만들지 않으므로 Dockerfile과 실제 Image의 차이는 `main` Merge 후에 확인됨
- V1은 핵심 기능 중심으로만 Test하므로 전체 기능과 높은 Test Coverage를 보장하지 않음
- GitHub-hosted Runner와 외부 LLM Mock 환경은 실제 EC2 운영 환경과 완전히 같지 않음
- 이미지 자동 정리를 적용하지 않아 운영 기간이 늘어나면 ECR 저장 비용과 관리 대상이 증가함
- CI가 통과해도 외부 API 장애, 운영 데이터 문제와 배포 후 성능 문제까지 보장하지 않음

현재 구조는 빠른 개발과 배포를 위해 검증 범위를 최소화한 구성이다.

서비스와 Repository가 늘어나거나 배포 실패가 반복되면 통합 테스트 환경, Image 보관 정책과 배포 전 검증을 확장한다.
