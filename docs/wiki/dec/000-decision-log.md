---
wiki: DEC-000 결정 로그
type: decision-log
group: dec-000
owner: 김세훈
status: 작성중
updated: 2026-09-21
---
**요약** 프로젝트의 모든 기술 결정을 한 줄씩 모은 입구. 상세는 링크를 따라간다.

| 날짜 | 파트 | 결정 | 영향 파트 | 상세 |
|---|---|---|---|---|
| 09-21 | AI | ③⑤ LangChain·⑦ LangGraph 도입 (V1 미도입 번복) | AI | [프레임워크 결정](../ai/4-multistep-pipeline/design.md) |
| 09-17 | TEAM | 원본 위키는 보존하고 스킬 적용 문서를 docs에 만들어 위키에 새 페이지로 반영. 변환 문서는 docs에서만 수정 | 전원 | [팀 결정](../../.agents/skills/ktb4-docs/references/decisions.md) |
| 09-17 | CLD | 3단계 상위 페이지와 3-5가 다르면 상위 페이지 값 기준 (API 중단 최대 5분, Secret은 EC2 .env.production). 3-5 원문은 그대로 두고 그 아래 `> 기준:` 한 줄 추가 | - | 클라우드 확인 대기 |
| 09-17 | TEAM | 위키 변환은 문서 구조(목차·소제목)와 내용은 그대로 두고 형식만 통일: 문체 어미 `~한다`, 굵은 줄·바깥 `<details>` 제목은 진짜 제목으로, 긴 예시는 부록 대신 제자리 접기, 블록별 정답 모양은 block-patterns | 전원 | [팀 결정 #16](../../.agents/skills/ktb4-docs/references/decisions.md) |
| 09-17 | FS·AI | FS API 명세와 AI API 명세의 공통 형식(응답 `{message, data}`, 에러 표기)을 맞춘다 | FS, AI | 형식 변경 시 이 로그에 추가 |
| 09-15 | AI | `book_passage` 원문조각 RAG·`preset_vectors` 얕은 RAG 폐기. `reason_long`은 `v_books.description`만 근거로 생성, AI 소유 테이블은 `book_embeddings`·`taste_profile`·멱등 기록 3종만 유지 | AI | [데이터/컨텍스트 보강 설계](../ai/5-context-augmentation/design.md) |
