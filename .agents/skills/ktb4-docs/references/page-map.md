# 원본 → 새 문서 대응표

- 원본 파일명·제목·URL은 첫 백업(`backup/wiki-original-2026-09-17/`, 원본 커밋 `a25afef1`) 기준으로 맞췄다.
- **원본 제목**은 GitHub Wiki가 파일명으로 자동 표시하는 제목이다 (하이픈→공백, 그 외 문자는 그대로). 문서 안의 `#` 제목과 다를 수 있다.
- 상태: `대기` → `변환 중` → `변환됨(확인 대기)` → `완료` / `보류` / `제외`
- 이 표는 변환 작업의 기준이다. 표에 없는 원본은 변환하지 않고 먼저 행을 추가한다.
- 새 문서는 **원본 페이지마다 하나**다 (1:1, 결정 #16). 상위 페이지와 하위 페이지는 같은 group·같은 폴더에 두고, 상위 페이지가 `order: 0` 대표다. 설계·선택 근거·부록으로 나누지 않는다 (AI-1 부록만 파트 결정으로 예외).
- 클라우드 파트는 원본 번호 표기가 제각각(`1단계`, `1-1`, `2.1` 등)이라 새 문서의 `CLD-N`은 **원본 번호를 그대로 옮긴 것이 아니라 소속(허브) 기준으로 다시 매긴 것**이다. 원본 번호는 각 허브 페이지 안의 절 번호일 뿐 전체 순번이 아니다.

## 클라우드

| 원본 파일 | 원본 제목 | 위키 URL | 새 문서 (wiki) | group | 상태 | 메모 |
|---|---|---|---|---|---|---|
| `3단계.-CD(지속적-배포)-파이프라인-설계.md` | 3단계. CD(지속적 배포) 파이프라인 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/3%EB%8B%A8%EA%B3%84.-CD%28%EC%A7%80%EC%86%8D%EC%A0%81-%EB%B0%B0%ED%8F%AC%29-%ED%8C%8C%EC%9D%B4%ED%94%84%EB%9D%BC%EC%9D%B8-%EC%84%A4%EA%B3%84) | CLD-3 CD 파이프라인 설계 (`overview.md`, order 0) | cld-3 | 대기 | **첫 변환 대상.** 충돌 시 이 페이지 값이 기준 |
| `3-1-CD-적용-범위-및-승인-정책.md` | 3 1 CD 적용 범위 및 승인 정책 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/3-1-CD-%EC%A0%81%EC%9A%A9-%EB%B2%94%EC%9C%84-%EB%B0%8F-%EC%8A%B9%EC%9D%B8-%EC%A0%95%EC%B1%85) | CLD-3 CD 적용 범위 및 승인 정책 (`scope-approval.md`) | cld-3 | 대기 | 상위 페이지와 겹침·차이 대조 |
| `3-2-배포-전략-및-전체-CI-CD-Pipeline.md` | 3 2 배포 전략 및 전체 CI CD Pipeline | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/3-2-%EB%B0%B0%ED%8F%AC-%EC%A0%84%EB%9E%B5-%EB%B0%8F-%EC%A0%84%EC%B2%B4-CI-CD-Pipeline) | CLD-3 배포 전략 및 CI CD Pipeline (`strategy-pipeline.md`) | cld-3 | 대기 | |
| `3-3-배포-검증-및-Rollback.md` | 3 3 배포 검증 및 Rollback | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/3-3-%EB%B0%B0%ED%8F%AC-%EA%B2%80%EC%A6%9D-%EB%B0%8F-Rollback) | CLD-3 배포 검증 및 Rollback (`verify-rollback.md`) | cld-3 | 대기 | |
| `3-4-배포-보안-및-Secret-관리.md` | 3 4 배포 보안 및 Secret 관리 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/3-4-%EB%B0%B0%ED%8F%AC-%EB%B3%B4%EC%95%88-%EB%B0%8F-Secret-%EA%B4%80%EB%A6%AC) | CLD-3 배포 보안 및 Secret 관리 (`security-secret.md`) | cld-3 | 대기 | |
| `3-5-최소-구조-비용-및-현재-한계.md` | 3 5 최소 구조 비용 및 현재 한계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/3-5-%EC%B5%9C%EC%86%8C-%EA%B5%AC%EC%A1%B0-%EB%B9%84%EC%9A%A9-%EB%B0%8F-%ED%98%84%EC%9E%AC-%ED%95%9C%EA%B3%84) | CLD-3 최소 구조 비용 및 현재 한계 (`cost-limits.md`) | cld-3 | 대기 | 비용 내용은 여기에만 있음. 중단 시간·Secret 방식이 상위와 다름 → 해당 문장 아래 `> 기준:` 한 줄 (#16) |
| `1단계.-EC2---Docker-Compose-기반-초기-배포-설계.md` | 1단계. EC2   Docker Compose 기반 초기 배포 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/1%EB%8B%A8%EA%B3%84.-EC2---Docker-Compose-%EA%B8%B0%EB%B0%98-%EC%B4%88%EA%B8%B0-%EB%B0%B0%ED%8F%AC-%EC%84%A4%EA%B3%84) | CLD-1 초기 배포 설계 (`overview.md`, order 0) | cld-1 | 변환됨(확인 대기) | 상위는 요약, 상세는 하위 6개. 굵은 줄 절 제목 → `###` |
| `1-1-서비스-규모-정의-및-예상-트래픽.md` | 1 1 서비스 규모 정의 및 예상 트래픽 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/1-1-%EC%84%9C%EB%B9%84%EC%8A%A4-%EA%B7%9C%EB%AA%A8-%EC%A0%95%EC%9D%98-%EB%B0%8F-%EC%98%88%EC%83%81-%ED%8A%B8%EB%9E%98%ED%94%BD) | CLD-1 서비스 규모 정의 및 예상 트래픽 (`scale-traffic.md`) | cld-1 | 변환됨(확인 대기) | |
| `1-2-인프라-구성-및-선택-근거.md` | 1 2 인프라 구성 및 선택 근거 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/1-2-%EC%9D%B8%ED%94%84%EB%9D%BC-%EA%B5%AC%EC%84%B1-%EB%B0%8F-%EC%84%A0%ED%83%9D-%EA%B7%BC%EA%B1%B0) | CLD-1 인프라 구성 및 선택 근거 (`infra-rationale.md`) | cld-1 | 변환됨(확인 대기) | |
| `1-3-운영-기준-및-예상-비용.md` | 1 3 운영 기준 및 예상 비용 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/1-3-%EC%9A%B4%EC%98%81-%EA%B8%B0%EC%A4%80-%EB%B0%8F-%EC%98%88%EC%83%81-%EB%B9%84%EC%9A%A9) | CLD-1 운영 기준 및 예상 비용 (`ops-cost.md`) | cld-1 | 변환됨(확인 대기) | `3 5` 가 비용 기준 문서로 참조 |
| `2-Docker-컨테이너-구성-설계.md` | 2 Docker 컨테이너 구성 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/2-Docker-%EC%BB%A8%ED%85%8C%EC%9D%B4%EB%84%88-%EA%B5%AC%EC%84%B1-%EC%84%A4%EA%B3%84) | CLD-1 Docker 컨테이너 구성 설계 (`containers.md`) | cld-1 | 변환됨(확인 대기) | |
| `3-Docker-Compose-기반-배포-구조-설계.md` | 3 Docker Compose 기반 배포 구조 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/3-Docker-Compose-%EA%B8%B0%EB%B0%98-%EB%B0%B0%ED%8F%AC-%EA%B5%AC%EC%A1%B0-%EC%84%A4%EA%B3%84) | CLD-1 Docker Compose 배포 구조 설계 (`compose.md`) | cld-1 | 변환됨(확인 대기) | |
| `4-현재-구조의-한계-정의.md` | 4 현재 구조의 한계 정의 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/4-%ED%98%84%EC%9E%AC-%EA%B5%AC%EC%A1%B0%EC%9D%98-%ED%95%9C%EA%B3%84-%EC%A0%95%EC%9D%98) | CLD-1 현재 구조의 한계 정의 (`limits.md`) | cld-1 | 변환됨(확인 대기) | 1단계 소속 |
| `2단계.-CI(지속적-통합)-파이프라인-설계.md` | 2단계. CI(지속적 통합) 파이프라인 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/2%EB%8B%A8%EA%B3%84.-CI%28%EC%A7%80%EC%86%8D%EC%A0%81-%ED%86%B5%ED%95%A9%29-%ED%8C%8C%EC%9D%B4%ED%94%84%EB%9D%BC%EC%9D%B8-%EC%84%A4%EA%B3%84) | CLD-2 CI 파이프라인 설계 | cld-2 | 대기 | |
| `4단계.-컨테이너-기반-서비스-확장-및-운영-자동화-설계.md` | 4단계. 컨테이너 기반 서비스 확장 및 운영 자동화 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/4%EB%8B%A8%EA%B3%84.-%EC%BB%A8%ED%85%8C%EC%9D%B4%EB%84%88-%EA%B8%B0%EB%B0%98-%EC%84%9C%EB%B9%84%EC%8A%A4-%ED%99%95%EC%9E%A5-%EB%B0%8F-%EC%9A%B4%EC%98%81-%EC%9E%90%EB%8F%99%ED%99%94-%EC%84%A4%EA%B3%84) | CLD-4 컨테이너 확장 설계 (`overview.md`, order 0) | cld-4 | **보류** | 작성 중. 끝나면 새 백업 후 변환 |
| `1-1-현재-서비스의-성장-상황.md` | 1 1 현재 서비스의 성장 상황 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/1-1-%ED%98%84%EC%9E%AC-%EC%84%9C%EB%B9%84%EC%8A%A4%EC%9D%98-%EC%84%B1%EC%9E%A5-%EC%83%81%ED%99%A9) | CLD-4 현재 서비스의 성장 상황 | cld-4 | 보류 | 4단계 소속 |
| `1-2-기존-인프라-한계.md` | 1 2 기존 인프라 한계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/1-2-%EA%B8%B0%EC%A1%B4-%EC%9D%B8%ED%94%84%EB%9D%BC-%ED%95%9C%EA%B3%84) | CLD-4 기존 인프라 한계 | cld-4 | 보류 | 09-17 수정 중 |
| `2.1.-단일-App-서버의-한계와-개선.md` | 2.1. 단일 App 서버의 한계와 개선 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/2.1.-%EB%8B%A8%EC%9D%BC-App-%EC%84%9C%EB%B2%84%EC%9D%98-%ED%95%9C%EA%B3%84%EC%99%80-%EA%B0%9C%EC%84%A0) | CLD-4 단일 App 서버의 한계와 개선 | cld-4 | 보류 | |
| `2.2.-데이터베이스의-한계와-개선.md` | 2.2. 데이터베이스의 한계와 개선 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/2.2.-%EB%8D%B0%EC%9D%B4%ED%84%B0%EB%B2%A0%EC%9D%B4%EC%8A%A4%EC%9D%98-%ED%95%9C%EA%B3%84%EC%99%80-%EA%B0%9C%EC%84%A0) | CLD-4 데이터베이스의 한계와 개선 | cld-4 | 보류 | |
| `1-1-현재-서비스의-성장-상황과-기존-인프라-한계.md` | 1 1 현재 서비스의 성장 상황과 기존 인프라 한계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/1-1-%ED%98%84%EC%9E%AC-%EC%84%9C%EB%B9%84%EC%8A%A4%EC%9D%98-%EC%84%B1%EC%9E%A5-%EC%83%81%ED%99%A9%EA%B3%BC-%EA%B8%B0%EC%A1%B4-%EC%9D%B8%ED%94%84%EB%9D%BC-%ED%95%9C%EA%B3%84) | - | - | **제외** | 중복. 백업에만 보관 |
| `Cloud-Wiki.md` | Cloud Wiki | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Cloud-Wiki) | CLD-0 허브 | cld-0 | 대기 | 컨벤션 내용 보존 |
| (없음) 5단계, 6단계 | - | - | - | - | 제외 | 원본 없음 |

## AI

| 원본 파일 | 원본 제목 | 위키 URL | 새 문서 (wiki) | group | 상태 | 메모 |
|---|---|---|---|---|---|---|
| `AI-Wiki.md` | AI Wiki | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/AI-Wiki) | AI-0 허브 | ai-0 | 대기 | |
| `모델-API-설계.md` | 모델 API 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EB%AA%A8%EB%8D%B8-API-%EC%84%A4%EA%B3%84) | AI-1 모델 API 명세 / 부록 (부록은 파트 결정 예외, #16) | ai-1 | 변환됨(확인 대기) | 예시는 부록. ERD 참조는 `ERD (작성 예정)`. 09-17 변환 완료 → `docs/ai/1-model-api/`. 09-17 spec.md 챕터 순서를 원본과 같게 재정렬(엔드포인트 목록→용어→입력/출력 형식 명세→서비스 구조→공통 규약), "5장 action 표"·"위 표" 참조 복원. preserve_check 오류 0건 |
| `모델-추론-성능-최적화.md` | 모델 추론 성능 최적화 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EB%AA%A8%EB%8D%B8-%EC%B6%94%EB%A1%A0-%EC%84%B1%EB%8A%A5-%EC%B5%9C%EC%A0%81%ED%99%94) | AI-2 추론 최적화 설계 | ai-2 | 대기 | |
| `서비스-아키텍처-모듈화.md` | 서비스 아키텍처 모듈화 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EC%84%9C%EB%B9%84%EC%8A%A4-%EC%95%84%ED%82%A4%ED%85%8D%EC%B2%98-%EB%AA%A8%EB%93%88%ED%99%94) | AI-3 아키텍처 모듈화 설계 | ai-3 | 대기 | |
| `멀티스텝-AI-파이프라인-구현-검토.md` | 멀티스텝 AI 파이프라인 구현 검토 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EB%A9%80%ED%8B%B0%EC%8A%A4%ED%85%9D-AI-%ED%8C%8C%EC%9D%B4%ED%94%84%EB%9D%BC%EC%9D%B8-%EA%B5%AC%ED%98%84-%EA%B2%80%ED%86%A0) | AI-4 멀티스텝 파이프라인 설계 | ai-4 | 대기 | |
| `데이터-컨텍스트-보강-설계.md` | 데이터 컨텍스트 보강 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EB%8D%B0%EC%9D%B4%ED%84%B0-%EC%BB%A8%ED%85%8D%EC%8A%A4%ED%8A%B8-%EB%B3%B4%EA%B0%95-%EC%84%A4%EA%B3%84) | AI-5 컨텍스트 보강 설계 | ai-5 | 대기 | |
| `표준화된-도구-통합-및-외부-API-활용-설계.md` | 표준화된 도구 통합 및 외부 API 활용 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%ED%91%9C%EC%A4%80%ED%99%94%EB%90%9C-%EB%8F%84%EA%B5%AC-%ED%86%B5%ED%95%A9-%EB%B0%8F-%EC%99%B8%EB%B6%80-API-%ED%99%9C%EC%9A%A9-%EC%84%A4%EA%B3%84) | AI-6 도구 통합 설계 | ai-6 | 대기 | |
| `서비스-인프라-확장성과-모니터링-설계.md` | 서비스 인프라 확장성과 모니터링 설계 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EC%84%9C%EB%B9%84%EC%8A%A4-%EC%9D%B8%ED%94%84%EB%9D%BC-%ED%99%95%EC%9E%A5%EC%84%B1%EA%B3%BC-%EB%AA%A8%EB%8B%88%ED%84%B0%EB%A7%81-%EC%84%A4%EA%B3%84) | AI-7 인프라 모니터링 설계 | ai-7 | 대기 | |
| `최종-통합-설계-및-회고.md` | 최종 통합 설계 및 회고 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EC%B5%9C%EC%A2%85-%ED%86%B5%ED%95%A9-%EC%84%A4%EA%B3%84-%EB%B0%8F-%ED%9A%8C%EA%B3%A0) | AI-8 최종 통합 설계 | ai-8 | 대기 | |
| `개발-워크플로.md` | 개발 워크플로 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EA%B0%9C%EB%B0%9C-%EC%9B%8C%ED%81%AC%ED%94%8C%EB%A1%9C) | AI-9 개발 워크플로 가이드 | ai-9 | 대기 | 09-17 신규 발견(첫 page-map에 누락). 사용자 확인으로 AI 그룹 편입, 제목 확정. 로컬 세팅(AI-10)·팀 컨벤션(TEAM)과 상호 참조 |
| `로컬‐개발‐환경‐세팅.md` | 로컬 개발 환경 세팅 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EB%A1%9C%EC%BB%AC%E2%80%90%EA%B0%9C%EB%B0%9C%E2%80%90%ED%99%98%EA%B2%BD%E2%80%90%EC%84%B8%ED%8C%85) | AI-10 로컬 개발 환경 세팅 가이드 | ai-10 | 대기 | 09-17 신규 발견(첫 page-map에 누락). 사용자 확인으로 AI 그룹 편입, 제목 확정. 파일명에 유니코드 하이픈 |

## 풀스택

| 원본 파일 | 원본 제목 | 위키 URL | 새 문서 (wiki) | group | 상태 | 메모 |
|---|---|---|---|---|---|---|
| `Frontend-Wiki.md` | Frontend Wiki | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Frontend-Wiki) | FE-0 허브 | fe-0 | 대기 | 컨벤션 내용 보존 |
| `Backend-Wiki.md` | Backend Wiki | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Backend-Wiki) | BE-0 허브 | be-0 | 대기 | 컨벤션 내용 보존 |
| `[1단계]-테이블-명세서.md` | [1단계] 테이블 명세서 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%5B1%EB%8B%A8%EA%B3%84%5D-%ED%85%8C%EC%9D%B4%EB%B8%94-%EB%AA%85%EC%84%B8%EC%84%9C) | FS-1 테이블 명세 | fs-1 | 대기 | 바깥 도메인 `<details>` → `###`. 앞머리 5단 목록 → 2단 (block-patterns #5) |
| `[2단계]-API-명세서.md` | [2단계] API 명세서 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%5B2%EB%8B%A8%EA%B3%84%5D-API-%EB%AA%85%EC%84%B8%EC%84%9C) | FS-2 API 명세 | fs-2 | 대기 | 예시는 제자리(엔드포인트 `<details>` 안). 바깥 도메인 `<details>` → `###`. 레이블은 block-patterns #1 |
| `[3단계]-기술-스택-정의서.md` | [3단계] 기술 스택 정의서 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%5B3%EB%8B%A8%EA%B3%84%5D-%EA%B8%B0%EC%88%A0-%EC%8A%A4%ED%83%9D-%EC%A0%95%EC%9D%98%EC%84%9C) | FS-3 기술 스택 정의 | fs-3 | 대기 | |
| `비즈니스-정책-위키.md` | 비즈니스 정책 위키 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EB%B9%84%EC%A6%88%EB%8B%88%EC%8A%A4-%EC%A0%95%EC%B1%85-%EC%9C%84%ED%82%A4) | FS-4 비즈니스 정책 | fs-4 | 대기 | 09-17 재분류: 원래 PM(기획·공통)에 있었으나, 실제 작성·관리 주체가 풀스택 파트라 사용자 확인으로 FS로 이동 |

## 기획·공통

| 원본 파일 | 원본 제목 | 위키 URL | 새 문서 (wiki) | group | 상태 | 메모 |
|---|---|---|---|---|---|---|
| `Home.md` | Home | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Home) | - (원본 유지) | - | 제외 | 사이드바만 새 페이지로 연결 |
| `_Sidebar.md` | _Sidebar | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/_Sidebar) | 생성됨 | - | - | `wiki_export.py --sidebar` 가 생성, 원본은 백업 |
| `_Footer.md` | _Footer | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/_Footer) | - | - | 제외 | 09-17 신규 발견(첫 page-map에 누락). `_Sidebar`와 동일하게 원본만 백업, 새 문서 없음. 사용자 확인으로 제외 확정 (Figma·프로젝트 보드 URL 플레이스홀더는 그대로 둠) |
| `Vision.md` | Vision | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Vision) | PM 비전 | pm-vision | 대기 | |
| `Roadmap.md` | Roadmap | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Roadmap) | PM 로드맵 | pm-roadmap | 대기 | |
| `Product‐Backlog.md` | Product Backlog | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Product%E2%80%90Backlog) | PM 프로덕트 백로그 | pm-backlog | 대기 | 파일명에 유니코드 하이픈 |
| `북적북적-V2-예상-매출.md` | 북적북적 V2 예상 매출 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%EB%B6%81%EC%A0%81%EB%B6%81%EC%A0%81-V2-%EC%98%88%EC%83%81-%EB%A7%A4%EC%B6%9C) | PM V2 예상 매출 | pm-revenue | 대기 | |
| `Release‐Planning.md` | Release Planning | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Release%E2%80%90Planning) | REL 릴리즈 플래닝 | rel | 대기 | |
| `Sprint‐Planning.md` | Sprint Planning | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Sprint%E2%80%90Planning) | SPR 스프린트 플래닝 | spr-planning | 대기 | 한 페이지 계속 갱신 |
| `Sprint‐Backlog.md` | Sprint Backlog | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/Sprint%E2%80%90Backlog) | SPR 스프린트 백로그 | spr-backlog | 대기 | 원본 내용 축소 금지 |
| `팀-컨벤션.md` | 팀 컨벤션 | [wiki](https://github.com/100-hours-a-week/KTB4-13th-wiki/wiki/%ED%8C%80-%EC%BB%A8%EB%B2%A4%EC%85%98) | TEAM 팀 컨벤션 | team-convention | 대기 | |
| (신규) | - | - | DEC-000 결정 로그 | dec-000 | 작성중 | |

## 변환 순서

1. cld-3 (테스트) → 2. ai-1~8 → 3. fs-1~3 → 4. cld-1, cld-2 → 5. 허브·기획 → 6. cld-4 (작성 끝난 뒤)
7. ai-9, ai-10 (09-17 신규 발견. 이름·소속 확정됨, ai-1~8 이후 진행)
