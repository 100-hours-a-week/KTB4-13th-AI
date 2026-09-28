---
wiki: DEC-000 결정 로그
type: decision-log
group: dec-000
owner: 김세훈
status: 작성중
updated: 2026-09-26
---
**요약** 프로젝트의 모든 기술 결정을 한 줄씩 모은 입구. 상세는 링크를 따라간다.

| 날짜 | 파트 | 결정 | 영향 파트 | 상세 |
|---|---|---|---|---|
| 09-26 | AI | ①②④⑥은 V1에서 429를 내지 않는다(호출 한도 없음) | AI, BE | [모델 API 명세](../ai/1-model-api/spec.md) |
| 09-26 | AI | LLM 폴백·서킷·지표 전송·예산 전환과 임베딩 무중단 교체는 V1 미구현으로 둔다 | AI, CLD | [인프라 모니터링 설계](../ai/7-infra-monitoring/design.md) |
| 09-25 | AI | ①④ category는 온보딩 값을 받아 대응표의 핵심 분류로 거른다. 그 밖의 값은 400 (AI #219) | AI, BE | [모델 API 명세 ①④](../ai/1-model-api/spec.md) |
| 09-25 | AI | 가격·재고는 AI가 `v_products`(BE 상품 표 복제본)에서 책마다 재고 있는 상품 중 가장 싼 것 하나로 읽는다 (AI #208) | AI, BE, CLD | [ERD 3.11](../ai/9-data-erd/spec.md) |
| 09-24 | AI | ① 키워드 검색은 pg_trgm만 쓰고 형태소 분석·BM25는 V1에서 쓰지 않는다. 쓰지 않던 tsvector 색인은 지운다 (AI #120, #169) | AI, CLD | [멀티스텝 파이프라인 설계](../ai/4-multistep-pipeline/design.md) |
| 09-23 | AI | ① 제목이 검색어와 같은 책을 맨 앞에 두고, 나머지는 키워드 3 : 벡터 1 가중 RRF로 합친다 (AI #119) | AI | [멀티스텝 파이프라인 설계](../ai/4-multistep-pipeline/design.md) |
| 09-23 | AI | ⑥ 산 책에 2.0점 이하 리뷰를 달면 취향 벡터에서만 빼고 카테고리 점수는 구매 점수를 유지한다 (AI #103) | AI | [모델 API 명세 ⑥](../ai/1-model-api/spec.md) |
| 09-21 | AI | ③⑤ LangChain·⑦ LangGraph 도입 (V1 미도입 번복) | AI | [프레임워크 결정](../ai/4-multistep-pipeline/design.md) |
| 09-17 | TEAM | 원본 위키는 보존하고 스킬 적용 문서를 docs에 만들어 위키에 새 페이지로 반영. 변환 문서는 docs에서만 수정 | 전원 | [팀 결정](../../.agents/skills/ktb4-docs/references/decisions.md) |
| 09-17 | CLD | 3단계 상위 페이지와 3-5가 다르면 상위 페이지 값 기준 (API 중단 최대 5분, Secret은 EC2 .env.production). 3-5 원문은 그대로 두고 그 아래 `> 기준:` 한 줄 추가 | - | 클라우드 확인 대기 |
| 09-17 | TEAM | 위키 변환은 문서 구조(목차·소제목)와 내용은 그대로 두고 형식만 통일: 문체 어미 `~한다`, 굵은 줄·바깥 `<details>` 제목은 진짜 제목으로, 긴 예시는 부록 대신 제자리 접기, 블록별 정답 모양은 block-patterns | 전원 | [팀 결정 #16](../../.agents/skills/ktb4-docs/references/decisions.md) |
| 09-17 | FS·AI | FS API 명세와 AI API 명세의 공통 형식(응답 `{message, data}`, 에러 표기)을 맞춘다 | FS, AI | 형식 변경 시 이 로그에 추가 |
| 09-15 | AI | `book_passage` 원문조각 RAG·`preset_vectors` 얕은 RAG 폐기. `reason_long`은 `v_books.description`만 근거로 생성, AI 소유 테이블은 `book_embeddings`·`taste_profile`·멱등 기록 3종만 유지 | AI | [데이터/컨텍스트 보강 설계](../ai/5-context-augmentation/design.md) |
| 09-07 | AI | 작가 대표 벡터(그 작가 책들의 평균)는 만들지 않고, 작가 기억(`memories` type: author)은 문장 벡터 그대로 취향 벡터에 넣는다 | AI | [모델 API 명세 ⑥](../ai/1-model-api/spec.md) |
