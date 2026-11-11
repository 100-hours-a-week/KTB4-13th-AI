# data

## reading_graph.json.gz

③ 책 추천 에이전트(`CHAT_AGENT`)가 쓰는 함께 대출·마니아 추천 그래프다. 서버는 `READING_GRAPH_PATH`(기본 `data/reading_graph.json.gz`)에서 읽는다.

- **출처:** [도서관 정보나루](https://www.data4library.kr/)(국립중앙도서관) 도서별 이용 분석(`usageAnalysisList`)의 함께 대출 도서·마니아 추천 도서 목록. 공개 저장소 배포는 정보나루에 문의해 허락받았다(2026-11-10).
- **든 것:** 작품 키(제목|저자), ISBN, 작품끼리의 함께 대출·마니아 목록 연결, 남의 목록에 든 횟수. 책표지 이미지 주소·대출 건수·개인정보는 없다.
- **크기:** 6.9MB, 작품 151,860개(목록 있는 작품 58,363개). 2026-11-11까지 받은 63,748권의 응답으로 만들었다. 서버 프로세스마다 메모리 약 110MB(읽는 동안 잠깐 약 170MB)
- **다시 만들기:** `python -m app.jobs.build_reading_graph <usage.jsonl> data/reading_graph.json.gz` (정보나루 원본 `usage.jsonl` 은 올리지 않는다. 여러 번 나눠 받았으면 파일을 이어 붙여 넣는다)

이 그래프로 고른 카드의 근거 칸에는 "도서관 정보나루"를 밝힌다(`app/chat/picking.py` `SOURCE_NOTE`).

## popular_books.json.gz

③ 챗봇이 분야 이름만 말한 요청("소설 추천해줘", "그냥 에세이", "추리소설 아무거나")에 쓰는 많이 읽힌 책 목록이다(#514). 서버는 `POPULAR_BOOKS_PATH`(기본 `data/popular_books.json.gz`)에서 읽는다.

- **출처:** 위 그래프와 같은 [도서관 정보나루](https://www.data4library.kr/) 도서별 이용 분석 원본. 책의 대출 건수 순서, KDC 분류, ISBN 부가기호, 정보나루가 뽑은 낱말로 꼬리표를 매겼다.
- **든 것:** 대출 많은 순으로 놓은 ISBN과 꼬리표(분야: 소설·수필·시·자기계발 / 문학의 나라 / 청소년 / 소설 세부 장르: 추리·로맨스·SF·역사·판타지). 대출 건수·낱말 원문·개인정보는 없다. 아동서(부가기호 7)와 학습참고서(5·6)는 넣지 않았다.
- **크기:** 53KB, 8,872권(소설 5,899·자기계발 2,121·수필 576·시 276). 2026-11-11까지 받은 63,748권의 응답으로 만들었다.
- **다시 만들기:** `python -m app.jobs.build_popular_books <usage.jsonl> [<usage.jsonl> ...] data/popular_books.json.gz`
