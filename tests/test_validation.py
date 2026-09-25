"""공통 검사(app/core/validation.py) 단위 테스트 — int32 범위·NUL·인코딩 불가 문자(#201)."""

import pytest

from app.core import validation


def test_int32_경계값은_상수와_일치한다() -> None:
    assert validation.INT32_MIN == -2_147_483_648
    assert validation.INT32_MAX == 2_147_483_647


def test_NUL이_없으면_그대로_돌려준다() -> None:
    assert validation.reject_nul("책") == "책"


def test_NUL이_있으면_ValueError() -> None:
    with pytest.raises(ValueError, match="NUL"):
        validation.reject_nul("김영\x00하")


def test_인코딩_가능하면_그대로_돌려준다() -> None:
    assert validation.reject_unencodable("여행의 이유") == "여행의 이유"


def test_짝_없는_서로게이트는_ValueError() -> None:
    with pytest.raises(ValueError, match="서로게이트|인코딩"):
        validation.reject_unencodable("\ud800")


def test_sanitize_string은_둘_다_검사한다() -> None:
    assert validation.sanitize_string("정상 문자열") == "정상 문자열"
    with pytest.raises(ValueError):
        validation.sanitize_string("\x00")
    with pytest.raises(ValueError):
        validation.sanitize_string("\ud800")
