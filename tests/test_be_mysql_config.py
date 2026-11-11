"""BE DB 를 읽는 설정(#709). #640 6단계 배치(BE → AI 반영)만 쓴다."""

import pytest

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None, database_url="postgresql://ai@localhost/ai", **overrides
    )


def test_BE_DB_주소는_기본이_비어_있다() -> None:
    # 비어 있으면 6단계 배치는 아무것도 하지 않는다. 서버가 뜨는 데에는 필요 없다
    assert _settings().be_mysql_url == ""


def test_BE_DB_주소를_환경변수로_받는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BE_MYSQL_URL", "mysql://reader:secret@be-db:3306/bookjeok")

    settings = Settings(_env_file=None, database_url="postgresql://ai@localhost/ai")

    assert settings.be_mysql_url == "mysql://reader:secret@be-db:3306/bookjeok"


def test_pymysql_을_불러올_수_있다() -> None:
    import pymysql

    assert pymysql.connect
