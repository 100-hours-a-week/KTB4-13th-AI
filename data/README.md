# data

## reading_graph.json.gz

③ 책 추천 에이전트(`CHAT_AGENT`)가 쓰는 함께 대출·마니아 추천 그래프다. 서버는 `READING_GRAPH_PATH`(기본 `data/reading_graph.json.gz`)에서 읽는다.

- **출처:** [도서관 정보나루](https://www.data4library.kr/)(국립중앙도서관) 도서별 이용 분석(`usageAnalysisList`)의 함께 대출 도서·마니아 추천 도서 목록. 공개 저장소 배포는 정보나루에 문의해 허락받았다(2026-11-10).
- **든 것:** 작품 키(제목|저자), ISBN, 작품끼리의 함께 대출·마니아 목록 연결, 남의 목록에 든 횟수. 책표지 이미지 주소·대출 건수·개인정보는 없다.
- **크기:** 2.6MB, 작품 71,457개(목록 있는 작품 18,693개)
- **다시 만들기:** `python -m app.jobs.build_reading_graph <usage.jsonl> data/reading_graph.json.gz` (정보나루 원본 `usage.jsonl` 은 올리지 않는다)

이 그래프로 고른 카드의 이유와 근거 칸에는 "도서관 정보나루"를 밝힌다(`app/chat/picking.py` `SOURCE_NOTE`).
