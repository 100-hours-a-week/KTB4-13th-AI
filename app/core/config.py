"""환경변수를 한 곳에서 읽는다.

코드 어디서도 os.environ 을 직접 뒤지지 않고 get_settings() 만 쓴다.
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # .env 에 여기 없는 키가 있어도 무시한다(TZ 등 OS가 쓰는 값)
        extra="ignore",
    )

    # --- 서버 (이름은 클라우드 compose.ai.yml 에서 고정됨) ---
    host: str = "0.0.0.0"
    port: int = 8000
    tz: str = "Asia/Seoul"
    log_level: str = "INFO"

    # 배포된 커밋 해시 7자. ⑧ /health 의 version 이자 로그의 Release SHA
    release_sha: str = "dev"

    # --- AI 전용 PostgreSQL ---
    # 기본값이 없다 = 필수. 없으면 앱이 아예 뜨지 않는다
    database_url: str
    db_pool_min: int = 1
    db_pool_max: int = 10

    # --- 인증 (① 부터) ---
    ai_service_token: str = ""
    cursor_signing_key: str = ""

    # --- 임베딩 (② 부터) ---
    # 384가 DDL(vector(384))에 박혀 있다. 바꾸면 두 테이블 전량 재생성
    embedding_model: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384
    worker_count: int = 4

    # --- 외부 LLM (③ 부터) ---
    external_ai_api_key: str = ""
    llm_provider: str = ""
    llm_model_id: str = ""
    # ⑤ 취향 뽑기만 쓰는 모델(#373). 비어 있으면 llm_model_id 를 쓴다. 대화 하나에서 취향 문장 몇 개를
    # 뽑는 일이라 작은 모델로 충분하고, 평가도 작은 모델로 잰다. 챗봇과 같은 값을 쓰면 운영에서는
    # 평가에 쓴 것과 다른 모델로 돌게 되어 따로 둔다.
    extraction_llm_model_id: str = ""
    # ⑤ 취향 뽑기를 자체 서빙 LLM(GPU 서버의 vLLM 처럼 OpenAI 호환인 곳)으로 부를지(#569 운영 시범). on(기본)이면
    # 자체 서빙을 먼저 부르고, 실패하거나 시간을 넘기면 지금 API 로 다시 부른다. 스팟 GPU 는 회수될 수 있고 밤에만
    # 켜 두기 때문이다. off 면 API 만 쓴다. 주소가 비어 있으면 on 이어도 API 만 쓴다(서버가 뜰 때 경고). 주소는
    # GPU 서버가 있는 환경의 설정에만 넣고, 켜고 끄기는 이 기본값을 바꾸는 PR 로 한다.
    extraction_self_hosted_mode: Literal["off", "on"] = "on"
    extraction_self_hosted_base_url: str = ""
    # GPU 서버 vLLM 의 --served-model-name 과 같아야 한다. 올린 모델을 바꿔도 이 이름은 그대로 둔다(#569).
    extraction_self_hosted_model_id: str = "bookjeok-extraction"
    # 자체 서빙 서버가 키를 검사할 때만 채운다. 외부 API 키를 다른 서버로 보내지 않으려고 따로 둔다.
    extraction_self_hosted_api_key: str = ""
    # 자체 서빙 호출 하나의 제한(초). 넘기면 API 로 다시 부르는데, 둘을 합친 시간도 llm_timeout_seconds 안에
    # 들어야 해서(명세 ⑤ 504) 짧게 둔다.
    extraction_self_hosted_timeout_seconds: float = 10
    # ③ 새 요청 판정 호출만 쓰는 모델(#513). 비어 있으면 llm_model_id 를 쓴다. 이어지는 턴마다 한 번 더 도는 짧은
    # 분류라 작고 빠른 모델로 충분하다. 평가도 작은 모델(gpt-4o-mini)로 쟀다. 챗봇과 같은 값이면 운영에서는
    # 더 큰 모델이 이 호출을 하게 되어 따로 둔다.
    relation_llm_model_id: str = ""
    # ③ 판정 두 개(문지기의 애매한 말 판정, 새 요청 판정)를 무엇으로 물을지. llm(기본)이면 지금처럼 챗봇 LLM 이 JSON 으로
    # 답하고, jev 면 판정 전용 모델 Jev 가 고른 선택지와 확신만 돌려준다(app/gateway/decision.py). 11/02 같은 사례 비교에서
    # 정확도는 같거나 조금 낫고(86.3% → 89.6%) 해석 단계가 p50 0.5초 빨랐다. Jev 는 OpenRouter 키로 부른다. ZDR 을
    # 지킨다는 문서가 없어 LLM_PROVIDER=openrouter(ZDR 켬)면 jev 여도 쓰지 않는다. 켜기는 이 기본값을 바꾸는 PR로 한다.
    chat_judge: Literal["llm", "jev"] = "llm"
    # 실측한 모델. 같은 이름 아래 스냅숏이 바뀔 수 있어 바꿀 때는 같은 사례로 다시 잰다.
    jev_model_id: str = "typesafe/jev-1.14"
    jev_url: str = "https://openrouter.ai/api/alpha/decisions"
    # ③ 분위기 요청의 후보를 외부 리랭커 API 로 다시 줄 세운 뒤 10권을 자를지(#638, app/gateway/reranker.py). 11/10 서술형
    # 35문항에서 운영 카드 모델(gpt-4o)로 3회씩 재면 카드 평균 점수는 그대로고 0점 카드가 105턴 중 12장 → 2장. OpenRouter
    # 키로 부르고 ZDR 엔드포인트가 없어 LLM_PROVIDER=openrouter(ZDR 켬)면 켜도 쓰지 않는다. 켜기는 이 기본값을 바꾸는 PR로 한다.
    chat_reranker: bool = False
    # 실측한 모델. 같은 이름 아래 모델이 바뀔 수 있어 바꿀 때는 같은 문항으로 다시 잰다.
    reranker_model_id: str = "cohere/rerank-5-fast"
    reranker_url: str = "https://openrouter.ai/api/v1/rerank"
    # 서울 AWS 에서 중앙 0.60초·최장 0.92초(11/10). 넘기면 검색 순서로 자른다.
    reranker_timeout_seconds: float = 2.0
    llm_base_url: str = "http://localhost:11434/v1"
    prompt_version: str = "v1"
    llm_timeout_seconds: int = 30
    llm_mock: bool = False
    # ⑦ 쇼핑 에이전트만 쓰는 모델(#356). 비어 있으면 llm_model_id 를 쓴다. ③ 평가의 기준 모델이 같이
    # 움직이지 않게 따로 둔다. 도구 호출을 쓰므로 ZDR 과 도구를 같이 지원하는 모델이어야 한다(#345).
    agent_llm_model_id: str = ""
    # ⑦ 쇼핑 에이전트가 쓸 진짜 BE 장바구니가 아직 없다(11단계). false(기본)면 /agent/act 는 503 으로
    # 답하고, true 면 메모리 가짜 장바구니로 동작한다. 배포 환경에서 가짜로 답하는 일이 없게 기본은 꺼 둔다.
    commerce_mock: bool = False
    # ⑦ 한 턴(LLM 호출 최대 4번과 도구 실행)의 시간 제한. 명세 ⑦ 의 504 "생성 30초 초과". llm_timeout_seconds 는
    # LLM 호출 하나의 제한이라 턴 전체는 이것으로 끊는다(#376).
    agent_turn_timeout_seconds: float = 30
    # ⑦ 카드 밖 책 찾기·주문 화면·쿠폰과 포인트(#631). 켜면 BE 공개 검색으로 카드에 없는 책을 찾아 담고, "주문해
    # 줘"면 주문 화면 버튼과 쓸 쿠폰·포인트(checkout)를 돌려준다. BE#533 이 배포되기 전에는 BE 가 목록 밖 ISBN 과
    # 주문 화면 목적지를 거절해 턴이 실패하므로 기본은 끈다. 문지기의 "사 줘" 규칙(v4)과 근거 검사의 "사 줘"도 이
    # 스위치를 따른다. 꺼져 있으면 챗봇 입구의 길은 main 과 같다.
    agent_checkout: bool = False
    # ⑦ 책 정보·리뷰(#650). 켜면 BE 공개 상품 상세·리뷰·쿠폰으로 "몇 권 남았어?", "리뷰 어때?", "쿠폰 있어?"에
    # 답하는 book.detail·book.reviews 를 주고, 문지기가 리뷰·평점·쿠폰·남은 수량 말을 ⑦로 보낸다(rules v6). BE 공개 API
    # 주소(아래)가 있어야 한다. 꺼져 있으면 프롬프트·도구가 이 기능 전과 같다
    agent_book_info: bool = False
    # BE 공개 API 주소. 로그인 없이 읽는 검색·상품 상세·리뷰·쿠폰만 부른다(사용자 권한은 쓰지 않는다). 비어 있으면
    # 책 찾기와 책 정보를 하지 않는다. COMMERCE_MOCK 이면 비워도 가공의 서가로 찾는다
    be_public_api_base_url: str = ""
    # BE 공개 API 호출 하나의 시간 제한(초). BE 검색은 안에서 AI 검색을 다시 부른다(새 검색어 약 2초)
    be_public_api_timeout_seconds: float = 5

    # --- 사진으로 책 찾기(#615) ---
    # 사진 속 글자를 읽는 호출만 쓰는 모델. 비어 있으면 llm_model_id 를 쓴다. 사진을 받는 모델이어야 한다.
    # 시험은 google/gemini-4-argon 으로 했다(#615 시험 결과).
    photo_llm_model_id: str = ""
    # 사진 주소(image_ref)가 이 중 하나로 시작할 때만 연다. 쉼표로 여러 개. 비어 있으면(기본) 어떤 주소도 열지
    # 않는다. 예: https://<버킷>.s3.ap-northeast-2.amazonaws.com/<사진 폴더>/
    photo_image_url_prefixes: str = ""
    # 사진 한 장(받기·읽기·찾기 전체)의 시간 제한. 모델에는 이 시간의 2/3만 써서 DB 검색 시간을 남긴다.
    photo_timeout_seconds: float = 60

    # --- 관심사별 홈 추천(#395) ---
    # 켜면(기본) ⑥이 관심사 점수와 추천 목록을 저장하고, 홈과 더 보기가 그 목록을 읽는다. 끄면 새 표·칸
    # (book_classes, taste_profile 의 새 칸)을 읽지도 쓰지도 않는다(#440). 켜 두려면 DB 에 그 마이그레이션과
    # 책 분류 데이터가 있어야 한다(개발 10/16, 운영 10/21 확인). 켜고 끄기는 이 기본값을 바꾸는 PR로 하고
    # 운영 설정값은 건드리지 않는다(#420).
    interest_feed: bool = True

    # --- 프로필 계산 뒤 홈 목록을 응답 뒤에 만들기(#581, 방향 #541) ---
    # 켜면 프로필 계산이 프로필과 요청 표만 저장하고 답한 뒤, 홈 목록은 서버 안 줄에 넣어 응답 뒤에
    # 만든다(app/feed/build_queue.py). 기다리는 사람이 없으니 목록 예산(profile_feed_seconds)을 응답 안에서
    # 쓰던 3초보다 넉넉히 준다. 끄면(기본) 지금처럼 응답 전에 3초 예산으로 만든다. 켜기는 실험 서버에서
    # 예산을 잰 뒤 기본값을 바꾸는 PR로 한다(#582).
    profile_feed_async: bool = False
    profile_feed_seconds: float = 6.0

    # --- 카프카로 프로필 계산 요청 받기(#666) ---
    # BE 가 온보딩 완료·취향 수정 때 PUT /preferences/profile 과 같은 본문을 카프카로도 보낸다(BE #551). 켜면 서버가
    # 뜰 때 그 토픽을 받는 소비자를 띄운다. 끄면(기본) 브로커를 찾지 않고 지금처럼 HTTP 로만 받는다.
    # 토픽·DLT 이름은 BE 계약과 같아야 한다. 소비자 그룹은 환경마다 달리 둔다(예: book-ai-profile-v1-dev).
    profile_kafka_enabled: bool = False
    # 쉼표로 여러 브로커를 적는다(BE 와 같은 이름)
    kafka_bootstrap_servers: str = ""
    profile_kafka_topic: str = "book.ai-profile.v1"
    profile_kafka_dlt_topic: str = "book.ai-profile.v1.DLT"
    profile_kafka_group_id: str = ""
    # 메시지 하나의 최대 크기. HTTP PUT 한도(app/routers/profile.py MAX_BODY_BYTES)와 같게 둔다 — 같은 본문이
    # 어느 길로 와도 받는다. 카프카 기본(1MB)보다 크니 브로커·토픽 한도도 맞춰야 한다
    profile_kafka_max_message_bytes: int = 8 * 1024 * 1024

    # --- ③ 대화로 좁혀 가기(#530) ---
    # 켜면 대화의 첫 요청이 넓을 때 카드 없이 원하는 것을 한 번 묻고, 답을 듣고 찾는다(app/chat/asking.py). 끄면(기본)
    # 첫 요청에도 바로 맞는 책 세 권을 보여 준다. 처음 만든 갈래 메뉴("…(1번), …(2번) 중 어느 쪽?")는 원하는 책을 찾는
    # 비율이 main 보다 낮아(47% → 짧은 평가 10~20%) 이 방식으로 바꿨다. 켜기는 이 기본값을 바꾸는 PR로 한다.
    chat_narrowing: bool = False

    # --- ③ 책 추천 에이전트(11/10 설계) ---
    # 켜면 1단계 호출이 검색어와 함께 떠올린 책 제목·기준 책을 내고, 검색(A)·떠올린 제목(B)·기준 책의 함께 대출·마니아
    # 목록(C)을 나란히 모은 한 목록을 판단 호출이 보고 카드·질문·한 번 더 찾기 중 하나를 고른다(app/chat/lanes.py,
    # app/chat/picking.py). 말풍선 마지막 문장은 고른 책의 함께 대출·마니아 목록에서 두 갈래를 제안한다. 기본은 켬(#646).
    # 끄면 예전의 검색 → 카드 생성 그대로다(문제가 생기면 CHAT_AGENT=false 로 되돌린다). ⑦ 쇼핑 에이전트와는 상관없다.
    chat_agent: bool = True
    # 함께 대출·마니아 그래프 파일(app/jobs/build_reading_graph.py 가 만든다). 없으면 C 갈래와 제안 줄 없이 돈다.
    reading_graph_path: str = "data/reading_graph.json.gz"

    # --- ② 소개가 바뀐 책의 벡터 다시 만들기(#300) ---
    # 켜면 임베딩 작업이 벡터를 만든 글의 해시를 새 표(book_embedding_docs)에 함께 적고, 지금 글과 다르면 다시
    # 만든다. 소개가 없어진 책의 벡터도 지우고, 끝에 빠진 책이 없는지 확인한다. 끄면(기본) 지금처럼 벡터가 없는 책만
    # 채우고 새 표를 읽지도 쓰지도 않아, 마이그레이션(20261023_book_embedding_docs.sql)이 적용되기 전에 머지돼도
    # 깨지지 않는다. 이 값은 임베딩 작업만 읽어서, 기본값을 바꾸는 배포 없이 돌릴 때 환경변수로 켠다:
    # docker exec -e EMBED_DOC_HASH=true bookjeok-ai python -m app.jobs.embed_books
    embed_doc_hash: bool = False

    # --- 로컬 개발 전용: dev/ 테스트 화면이 브라우저에서 직접 호출할 때만 필요.
    # 비어있으면(기본) 아무 효과 없다. BE 연동 시작하면 이 줄과 dev/ 폴더를 같이 지운다.
    dev_cors_origins: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
