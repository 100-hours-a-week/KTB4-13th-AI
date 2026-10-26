"""DB 연결 설정의 호환성, 환경변수 입력, 특수문자 처리를 검증한다."""

import os
from urllib.parse import unquote, urlsplit

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture(autouse=True)
def clear_db_environment(monkeypatch):
    names = {"DATABASE_URL", "DB_USERNAME", "DB_PASSWORD"}
    for name in os.environ:
        if name.upper() in names:
            monkeypatch.delenv(name)


def test_separate_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://db.internal:5433/books")
    monkeypatch.setenv("DB_USERNAME", "reader")
    monkeypatch.setenv("DB_PASSWORD", "test-password")

    settings = Settings(_env_file=None)

    assert settings.database_url == (
        "postgresql://reader:test-password@db.internal:5433/books"
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://legacy:secret@primary:5432/books?sslmode=require",
        "postgres://reader@127.0.0.1:5432/books",
        "postgresql://reader:p%40ss@[::1]:5432/books",
    ],
)
def test_existing_database_url_is_preserved(url):
    assert Settings(_env_file=None, database_url=url).database_url == url


def test_separate_credentials_override_url_credentials():
    settings = Settings(
        _env_file=None,
        database_url="postgresql://old:secret@db:5432/books?sslmode=require",
        db_username="reader",
        db_password="new-password",
    )

    assert settings.database_url == (
        "postgresql://reader:new-password@db:5432/books?sslmode=require"
    )


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"db_username": "reader"}, "reader:p%40ss"),
        ({"db_password": "new-password"}, "old%40user:new-password"),
    ],
)
def test_unspecified_credentials_fall_back_to_url(overrides, expected):
    settings = Settings(
        _env_file=None,
        database_url="postgresql://old%40user:p%40ss@db:5432/books",
        **overrides,
    )

    assert settings.database_url == f"postgresql://{expected}@db:5432/books"


def test_separate_credentials_from_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DATABASE_URL=postgresql://127.0.0.1:5432/books\n"
        "DB_USERNAME=reader\nDB_PASSWORD=secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DB_PASSWORD", "environment-password")

    settings = Settings(_env_file=env_file)

    assert settings.database_url == (
        "postgresql://reader:environment-password@127.0.0.1:5432/books"
    )


def test_special_characters_preserve_ipv6_database_and_options():
    username = "reader@team:/한글"
    password = "p@ss:/?#%+ 한글"

    settings = Settings(
        _env_file=None,
        database_url="postgresql://[::1]:5433/books%2Fname?sslmode=require",
        db_username=username,
        db_password=password,
    )
    parsed = urlsplit(settings.database_url)

    assert unquote(parsed.username) == username
    assert unquote(parsed.password) == password
    assert parsed.hostname == "::1"
    assert parsed.port == 5433
    assert parsed.path == "/books%2Fname"
    assert parsed.query == "sslmode=require"
    assert parsed.fragment == ""


def test_passwordless_local_database():
    settings = Settings(
        _env_file=None,
        database_url="postgresql://127.0.0.1:5432/books",
        db_username="reader",
    )

    assert settings.database_url == "postgresql://reader@127.0.0.1:5432/books"


@pytest.mark.parametrize("url", [None, ""])
def test_url_is_required_even_with_separate_credentials(url):
    values = {"database_url": url} if url is not None else {}

    with pytest.raises(ValidationError, match="(?i)database_url"):
        Settings(_env_file=None, db_username="reader", db_password="secret", **values)


def test_password_without_username_is_rejected():
    with pytest.raises(ValidationError, match="DB_USERNAME"):
        Settings(
            _env_file=None,
            database_url="postgresql://db:5432/books",
            db_password="secret",
        )


def test_invalid_scheme_is_rejected_with_separate_credentials():
    with pytest.raises(ValidationError, match="postgresql://"):
        Settings(
            _env_file=None,
            database_url="https://db:5432/books",
            db_username="reader",
        )
