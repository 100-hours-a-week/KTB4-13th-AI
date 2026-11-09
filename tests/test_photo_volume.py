"""권 번호 다루기(app/photo/volume.py) 테스트(#615)."""

import pytest

from app.photo import volume


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2", {"2", "ii", "ⅱ"}),
        ("II", {"ii", "2", "ⅱ"}),
        ("상", {"상"}),
        ("2027", {"2027"}),
        ("PART 1 03", {"part103"}),
        ("Vol. 2", {"vol2"}),
    ],
)
def test_권_번호의_여러_표기(raw: str, expected: set[str]) -> None:
    assert volume.keys(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, "", "개정판", "3/E", "2nd Edition", "제3판", "특별 에디션", "무선"]
)
def test_판_표시나_숫자_없는_글자는_권_번호가_아니다(raw: str | None) -> None:
    assert volume.keys(raw) == set()


def test_숫자는_앞의_0을_떼고_본다() -> None:
    assert volume.numbers("PART 4 01") == {4, 1}


@pytest.mark.parametrize(
    ("read", "catalog_title", "ok"),
    [
        ("자바스크립트 완벽 가이드 7", "자바스크립트 완벽 가이드 7", True),
        ("자바스크립트 완벽 가이드 6", "자바스크립트 완벽 가이드 7", False),
        ("자바스크립트 완벽 가이드", "자바스크립트 완벽 가이드 7", True),
        ("자바스크립트 완벽 가이드 7", "자바스크립트 완벽 가이드", True),
        ("전지적 독자 시점 PART 4 01", "전지적 독자 시점 Part 4-01", True),
    ],
)
def test_숫자가_어긋날_때만_다른_권이다(
    read: str, catalog_title: str, ok: bool
) -> None:
    assert volume.compatible(read, catalog_title) is ok
