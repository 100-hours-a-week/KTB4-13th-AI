"""환경변수를 한 곳에서 읽는다.

코드 어디서도 os.environ 을 직접 뒤지지 않고 get_settings() 만 쓴다.
"""

from functools import lru_cache

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
    llm_base_url: str = "http://localhost:11434/v1"
    prompt_version: str = "v1"
    llm_timeout_seconds: int = 30
    llm_mock: bool = False

    # --- 로컬 개발 전용: dev/ 테스트 화면이 브라우저에서 직접 호출할 때만 필요.
    # 비어있으면(기본) 아무 효과 없다. BE 연동 시작하면 이 줄과 dev/ 폴더를 같이 지운다.
    dev_cors_origins: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
