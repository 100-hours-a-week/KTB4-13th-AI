# 📚 북적북적 문서

변환이 끝난 문서는 **이 폴더에서만** 수정한다. 위키에는 문서 담당자가 반영한다.
원본 위키는 그대로 있고, 변환 전 원본은 [backup](../backup/) 에 보관한다.

목차는 **문서 하나에 한 줄**로 쓴다. 한 줄에 여러 문서를 `·` 로 잇지 않는다 — 여러 PR이 같은 줄을 고치면 머지 충돌이 나고, 충돌을 한쪽만 선택해 풀면 다른 PR이 추가한 링크가 조용히 사라진다.

## 결정

- [DEC-000 결정 로그](dec/000-decision-log.md)

## 클라우드

- [CLD-1 초기 배포 설계](cld/1-initial-deploy/overview.md)
- [CLD-1 서비스 규모 정의 및 예상 트래픽](cld/1-initial-deploy/scale-traffic.md)
- [CLD-1 인프라 구성 및 선택 근거](cld/1-initial-deploy/infra-rationale.md)
- [CLD-1 운영 기준 및 예상 비용](cld/1-initial-deploy/ops-cost.md)
- [CLD-1 Docker 컨테이너 구성 설계](cld/1-initial-deploy/containers.md)
- [CLD-1 Docker Compose 배포 구조 설계](cld/1-initial-deploy/compose.md)
- [CLD-1 현재 구조의 한계 정의](cld/1-initial-deploy/limits.md)
- [CLD-2 CI 파이프라인 설계](cld/2-ci-pipeline/overview.md)

## AI

- [AI-1 모델 API 명세](ai/1-model-api/spec.md)
- [AI-1 모델 API 명세 부록](ai/1-model-api/appendix.md)
- [AI-2 추론 최적화 설계](ai/2-inference-optimization/design.md)
- [AI-3 아키텍처 모듈화 설계](ai/3-architecture-modularization/design.md)
- [AI-4 멀티스텝 파이프라인 설계](ai/4-multistep-pipeline/design.md)
- [AI-5 컨텍스트 보강 설계](ai/5-context-augmentation/design.md)
- [AI-6 도구 통합 설계](ai/6-tool-integration/design.md)
- [AI-7 인프라 모니터링 설계](ai/7-infra-monitoring/design.md)
- [AI-7 CD 구축 정보 가이드](ai/7-infra-monitoring/cd-handoff-guide.md)
- [AI-8 최종 통합 설계](ai/8-final-integration/design.md)

## 풀스택

(변환 후 추가)

## 기획

(변환 후 추가)

작성·변환 규칙: [.agents/skills/ktb4-docs/SKILL.md](../../.agents/skills/ktb4-docs/SKILL.md)
