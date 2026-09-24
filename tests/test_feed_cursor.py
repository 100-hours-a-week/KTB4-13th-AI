"""④ 피드 커서 테스트 — 무엇을 담고 언제 끊는가. DB 없이 돈다."""

from datetime import UTC, datetime

import pytest

from app.core import cursor as core_cursor
from app.feed import cursor
from app.feed.schemas import parse_query

_NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


def _req(cursor_value: str | None = None, **params):
    items = [("user_id", "1"), ("surface", "recommend_more"), *params.items()]
    if cursor_value is not None:
        items.append(("cursor", cursor_value))
    return parse_query(items)


@pytest.fixture(autouse=True)
def signing_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")


def _next_cursor(req, mode="cold-start", version=None, offset=0):
    page = cursor.Page(offset=offset, mode=mode, issued_at=_NOW)
    return cursor.issue(page, req, mode, version)


def test_커서가_없으면_첫_페이지고_지금_시각을_쓴다() -> None:
    page = cursor.read(_req(), now=_NOW)

    assert (page.offset, page.mode, page.issued_at) == (0, None, _NOW)


def test_다음_페이지는_size_만큼_건너뛴_자리다() -> None:
    req = _req(size="15")

    page = cursor.read(_req(_next_cursor(req), size="15"))

    assert page.offset == 15


def test_첫_페이지를_받은_시각을_그대로_들고_간다() -> None:
    # 페이지마다 새로 찍으면 그 사이에 산 책이 다음 페이지에서 빠져 목록이 밀린다(명세 ④).
    req = _req()

    page = cursor.read(_req(_next_cursor(req)))

    assert page.issued_at == _NOW


@pytest.mark.parametrize(
    "changed",
    [
        {"sort": "newest"},
        {"category": "에세이"},
        {"pub_year_from": "2020"},
        {"match_score_min": "50"},
    ],
)
def test_정렬이나_필터가_바뀌면_끊는다(changed: dict) -> None:
    token = _next_cursor(_req())

    with pytest.raises(cursor.CursorExpired):
        cursor.read(_req(token, **changed))


def test_개수만_바꾸는_것은_끊지_않는다() -> None:
    # 순서는 그대로이므로 이어 붙일 수 있다.
    token = _next_cursor(_req(size="15"))

    assert cursor.read(_req(token, size="30")).offset == 15


def test_위조하거나_모양이_깨진_커서는_끊는다() -> None:
    with pytest.raises(cursor.CursorExpired):
        cursor.read(_req("아무개"))


def test_만료된_커서는_끊는다(monkeypatch: pytest.MonkeyPatch) -> None:
    token = _next_cursor(_req())
    monkeypatch.setattr(core_cursor, "TTL_SECONDS", -1)

    with pytest.raises(cursor.CursorExpired):
        cursor.read(_req(token))


def test_응답_모드가_바뀌면_끊는다() -> None:
    # 개인화한 목록과 개인화를 끈 목록은 순서가 아예 달라 같은 자리가 다른 책을 가리킨다.
    page = cursor.read(_req(_next_cursor(_req(), mode="personalized")))

    with pytest.raises(cursor.CursorExpired):
        cursor.check_mode(page, "cold-start")


def test_프로필_판_번호가_달라도_끊지_않는다() -> None:
    # 명세: 프로필이나 인기 집계가 바뀌면 갱신된 값으로 이어 붙인다.
    page = cursor.read(_req(_next_cursor(_req(), mode="personalized", version=1)))

    cursor.check_mode(page, "personalized")
