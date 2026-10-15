"""AI 책 분류 표 넣기 작업 테스트(#421).

파일 읽기와 검사는 DB 없이 돈다.
"""

import gzip
import io

import pytest

from app.jobs import load_book_classes as job

HEADER = "isbn13,class_no,reader_code,form_code,page_count"


def _parse(*lines: str):
    return job.parse([HEADER, *lines])


def test_칸을_받은_그대로_넣고_빈_칸은_NULL로_둔다() -> None:
    rows, stats = _parse("9788937462788,813.7,0,3,312", "9788937462795,,,,")

    assert rows == [
        ("9788937462788", "813.7", "0", "3", 312),
        ("9788937462795", None, None, None, None),
    ]
    assert stats.read == 2
    assert not stats.skipped


def test_앞뒤_공백은_지운다() -> None:
    rows, _ = _parse(" 9788937462788 , 813.7 ,0, 3 , 312 ")

    assert rows == [("9788937462788", "813.7", "0", "3", 312)]


def test_머리글이_다르면_멈춘다() -> None:
    with pytest.raises(ValueError, match="머리글"):
        job.parse(["isbn,class_no,reader_code,form_code,page_count"])


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("978893746278,813.7,0,3,312", "isbn13"),
        ("97889374627ab,813.7,0,3,312", "isbn13"),
        ("9788937462788,813.712345678901234567,0,3,312", "class_no"),
        ("9788937462788,813.7,01,3,312", "reader_code"),
        ("9788937462788,813.7,0,x,312", "form_code"),
        ("9788937462788,813.7,0,3,0", "page_count"),
        ("9788937462788,813.7,0,3,삼백", "page_count"),
        ("9788937462788,813.7,0,3,2147483648", "page_count"),
        ("9788937462788,813.7,0,3", "칸 수"),
    ],
)
def test_칸이_틀린_줄은_이유를_세고_버린다(line: str, reason: str) -> None:
    rows, stats = _parse(line, "9788937462795,,,,")

    assert rows == [("9788937462795", None, None, None, None)]
    assert stats.read == 2
    assert stats.skipped == {reason: 1}


def test_같은_ISBN이_다시_나오면_앞의_줄만_쓴다() -> None:
    rows, stats = _parse("9788937462788,813.7,0,3,312", "9788937462788,911,1,1,100")

    assert rows == [("9788937462788", "813.7", "0", "3", 312)]
    assert stats.skipped == {"중복": 1}


def test_gzip_파일도_읽는다() -> None:
    raw = f"{HEADER}\n9788937462788,813.7,0,3,312\n".encode()

    lines = list(job.open_text(io.BytesIO(gzip.compress(raw))))

    assert job.parse(lines)[0] == [("9788937462788", "813.7", "0", "3", 312)]


def test_BOM이_붙은_파일도_읽는다() -> None:
    raw = f"﻿{HEADER}\n9788937462788,,,,\n".encode()

    lines = list(job.open_text(io.BytesIO(raw)))

    assert job.parse(lines)[0] == [("9788937462788", None, None, None, None)]
