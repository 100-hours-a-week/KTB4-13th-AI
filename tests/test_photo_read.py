"""사진 글자 읽기(app/photo/read.py) 테스트(#572, #615). 모델은 가짜로 두고 받은 답을 칸으로 바꾸는 규칙만 본다."""

import asyncio

import pytest
from langchain_core.language_models import FakeListChatModel

from app.gateway.llm import LLMUnavailableError
from app.photo import read


def test_모델이_빼먹은_칸은_비운다() -> None:
    text = read.BookText.model_validate({"is_book_cover": True, "title": "토지"})

    assert text.title == "토지"
    assert text.authors == []
    assert text.isbn is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("978-89-546-8245-2", "9788954682452"),
        ("ISBN 9788954682452", "9788954682452"),
        ("97889546", None),  # 13자리가 아니면 못 읽은 것
        ("", None),
    ],
)
def test_ISBN은_숫자_13자리만_남긴다(raw: str, expected: str | None) -> None:
    assert read.BookText.model_validate({"isbn": raw}).isbn == expected


def test_빈_글자는_없는_칸이다() -> None:
    text = read.BookText.model_validate(
        {"title": "  ", "publisher": "", "authors": ["", " 김호연 "]}
    )

    assert text.title is None
    assert text.publisher is None
    assert text.authors == ["김호연"]


def test_외국어_제목은_따로_받는다() -> None:
    text = read.BookText.model_validate(
        {"title": "이펙티브 자바", "original_title": " EFFECTIVE JAVA "}
    )

    assert text.title == "이펙티브 자바"
    assert text.original_title == "EFFECTIVE JAVA"


def test_저자를_글자_하나로_줘도_받는다() -> None:
    assert read.BookText.model_validate({"authors": "한강"}).authors == ["한강"]


def test_권_번호를_숫자로_줘도_받는다() -> None:
    assert read.BookText.model_validate({"volume": 2}).volume == "2"


def test_긴_제목은_자른다() -> None:
    text = read.BookText.model_validate({"title": "가" * 300})

    assert len(text.title) == read.MAX_FIELD_CHARS


def _fake_model(answer: str, monkeypatch) -> None:
    monkeypatch.setattr(
        read,
        "get_chat_model",
        lambda **kwargs: FakeListChatModel(responses=[answer]),
    )


def test_모델의_답을_칸으로_받는다(monkeypatch) -> None:
    _fake_model(
        '{"photo_type": "cover", "books": [{"is_book_cover": true, "title": "불편한 편의점",'
        ' "authors": ["김호연"], "publisher": "나무옆의자", "volume": null, "isbn": null}]}',
        monkeypatch,
    )

    photo = asyncio.run(read.read("data:image/jpeg;base64,AAAA"))

    assert photo.photo_type == "cover"
    [text] = photo.books
    assert text.title == "불편한 편의점"
    assert text.authors == ["김호연"]
    assert text.publisher == "나무옆의자"


def test_칸_모양이_틀린_답은_모델_장애와_같게_본다(monkeypatch) -> None:
    _fake_model(
        '{"photo_type": "cover", "books": [{"is_book_cover": "아마도요"}]}', monkeypatch
    )

    with pytest.raises(LLMUnavailableError):
        asyncio.run(read.read("data:image/jpeg;base64,AAAA"))


def test_책장은_왼쪽부터_여러_권을_받는다(monkeypatch) -> None:
    _fake_model(
        '{"photo_type": "shelf", "books": [{"title": "루비 프로그래밍 언어"},'
        ' {"title": "COVERT JAVA"}]}',
        monkeypatch,
    )

    photo = asyncio.run(read.read("data:image/jpeg;base64,AAAA"))

    assert photo.photo_type == "shelf"
    assert [b.title for b in photo.books] == ["루비 프로그래밍 언어", "COVERT JAVA"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"books": [{"title": "토지"}]}, "cover"),
        ({"books": [{"title": "토지"}, {"title": "파과"}]}, "shelf"),
        ({"books": []}, "none"),
        (
            {"photo_type": "책장", "books": [{"title": "토지"}, {"title": "파과"}]},
            "shelf",
        ),
    ],
)
def test_사진_종류가_없거나_모르는_값이면_권_수로_정한다(
    raw: dict, expected: str
) -> None:
    assert read.PhotoText.model_validate(raw).photo_type == expected


def test_책_목록이_null이면_빈_목록이다() -> None:
    photo = read.PhotoText.model_validate({"photo_type": "none", "books": None})

    assert photo.books == []
    assert photo.photo_type == "none"


def test_사진_종류를_적은_대로_받는다() -> None:
    # 책장 사진에 한 권만 보여도 모델이 책장이라고 했으면 책장 규칙으로 찾는다
    photo = read.PhotoText.model_validate(
        {"photo_type": "shelf", "books": [{"title": "토지"}]}
    )

    assert photo.photo_type == "shelf"


def test_사진_읽기는_사진_전용_모델과_시간_제한을_쓴다(monkeypatch) -> None:
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return FakeListChatModel(responses=['{"photo_type": "none", "books": []}'])

    monkeypatch.setattr(read, "get_chat_model", fake)
    monkeypatch.setattr(
        read.get_settings(), "photo_llm_model_id", "google/gemini-4-argon"
    )
    monkeypatch.setattr(read.get_settings(), "photo_timeout_seconds", 60)

    asyncio.run(read.read("data:image/jpeg;base64,AAAA"))

    assert seen == {"model_id": "google/gemini-4-argon", "timeout_seconds": 60}
