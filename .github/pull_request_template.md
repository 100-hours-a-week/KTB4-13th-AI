## 관련 Issue

- Closes #

## 변경 이유

<!-- 이 변경이 왜 필요한지. "무엇을 했는지"는 아래에 쓰고 여기엔 "왜"를 쓴다. -->

## 주요 변경

-

## 제외 범위

<!-- 이번 PR에서 다루지 않는 것. 리뷰어가 "이건 왜 안 했지?" 하지 않도록. -->
-

## 검증

<!-- 이 저장소에 해당하는 항목만 남기고 나머지는 지운다. -->

- [ ] Unit Test
- [ ] Integration Test
- [ ] Lint (`uv run ruff check .`)
- [ ] Docker Image Build
- [ ] 수동 검증

<!-- 어떻게 검증했고 결과가 어땠는지. -->

## 위험과 Rollback

- 예상 위험:
- 관찰 지표:
- Rollback 방법:

<!--
아래 세 절은 해당할 때만 남기고, 아니면 통째로 지운다.
-->

## API 변경 (해당 시)

- 변경 전후 Request·Response 예시:
- 기존 Client 호환 여부:
- Field 추가·변경·폐기 계획:
- 오류 응답 변경:
- 저장소 간 반영 및 배포 순서:

## DB 변경 (해당 시)

- Forward Migration 방법:
- 이전 Application과의 호환 여부:
- 예상 Lock 시간:
- Backfill 필요 여부:
- Application Rollback 시 영향:

<!-- 벡터 차원이 바뀌는 변경이면 book_embeddings·taste_profile 전량 재생성이
     필요하고 taste_profile은 BE가 전 사용자 ⑥을 재호출해야 한다. 단독 배포로 분리할 것. -->

## AI 설정 변경 (해당 시)

- Provider:
- Model ID:
- Prompt Version:
- 고정 Dataset 검증 결과:
- 응답 시간 및 비용 변화:
- Timeout과 Fallback 영향:

## Review 순서

<!-- 변경이 크면 어디부터 보면 좋은지. -->
