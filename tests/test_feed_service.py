"""④ 목록을 고르는 흐름 테스트 — 프로필 유무에 따라 어느 길로 가는가. DB 없이 돈다."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.core.config import get_settings
from app.feed import cursor, exclusion, interest_page, service
from app.feed.schemas import parse_query


class _FakePool:
    def __init__(self, profile_row):
        self._row = profile_row

    @asynccontextmanager
    async def acquire(self):
        yield _FakeConn(self._row)


class _FakeConn:
    def __init__(self, row):
        self._row = row

    async def fetchrow(self, sql, *args):
        return self._row


async def _no_recent_history(conn, user_id, reflected_at, until=None):
    return {}


async def _has_book_vectors(conn):
    return True


def _run(req, profile_row, monkeypatch, **fakes):
    monkeypatch.setattr(service.db, "get_pool", lambda: _FakePool(profile_row))
    # 프로필 뒤에 생긴 이력(#175)은 따로 보는 테스트가 아니면 없다고 둔다.
    fakes.setdefault("history__category_scores_since", _no_recent_history)
    # 책 벡터는 따로 보는 테스트가 아니면 있다고 둔다.
    fakes.setdefault("vector__has_any", _has_book_vectors)
    for name, fn in fakes.items():
        module, attr = name.split("__")
        monkeypatch.setattr(getattr(service, module), attr, fn)
    return asyncio.run(service.feed(req))


def _req(**params):
    return parse_query(
        [("user_id", "1"), ("surface", "recommend_more"), *params.items()]
    )


def test_프로필이_없으면_개인화를_끈_목록으로_간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BE 는 개인화 추천에 동의한 사용자만 ⑥ 을 부른다. 프로필이 없으면 이력이 있어도 개인화하지 않는다(#266).
    async def _cold(conn, req, page, excluded=None):
        return [{"book_id": 7, "match_score": 0}], False

    outcome = _run(_req(), None, monkeypatch, cold_start__fetch=_cold)

    assert [item["book_id"] for item in outcome.items] == [7]
    assert (outcome.cold_start, outcome.degraded) == (True, None)


def test_취향_벡터를_못_만든_사용자도_같은_길로_간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ⑥ 이 cold_start 로 저장한 경우다. 벡터가 없어 채점할 수 없다.
    row = {
        "centroid": None,
        "tag_weights": "{}",
        "cold_start": True,
        "profile_version": 0,
        "computed_at": None,
    }

    async def _cold(conn, req, page, excluded=None):
        return [], False

    outcome = _run(_req(), row, monkeypatch, cold_start__fetch=_cold)

    assert outcome.cold_start is True


def test_개인화를_끈_목록은_모두_0점이라_점수_하한이_있으면_조회하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _cold(conn, req, page, excluded=None):
        raise AssertionError("목록을 조회하면 안 된다")

    outcome = _run(
        _req(match_score_min="1"), None, monkeypatch, cold_start__fetch=_cold
    )

    assert outcome.items == []


def test_프로필이_있으면_채점한_목록을_준다(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "centroid": "[0.1, 0.2]",
        "tag_weights": '{"에세이": 3}',
        "cold_start": False,
        "profile_version": 2,
        "computed_at": None,
    }

    async def _personalized(conn, req, page, centroid, tag_weights, excluded=None):
        assert centroid == [0.1, 0.2]
        assert tag_weights == {"에세이": 3}
        return [{"book_id": 9, "match_score": 84}], False

    outcome = _run(_req(), row, monkeypatch, personalized__fetch=_personalized)

    assert [item["book_id"] for item in outcome.items] == [9]
    assert (outcome.cold_start, outcome.degraded) == (False, None)


def test_벡터_조회가_안_되면_규칙_점수만으로_답하고_알린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = {
        "centroid": "[0.1]",
        "tag_weights": "{}",
        "cold_start": False,
        "profile_version": 1,
        "computed_at": None,
    }

    async def _personalized(conn, req, page, centroid, tag_weights, excluded=None):
        raise RuntimeError("벡터 색인 이상")

    got: list[dict] = []

    async def _rule_only(conn, req, page, tag_weights, excluded=None):
        got.append(tag_weights)
        return [{"book_id": 3, "match_score": 25}], False

    row = {**row, "tag_weights": '{"한국문학": 4}'}
    outcome = _run(
        _req(),
        row,
        monkeypatch,
        personalized__fetch=_personalized,
        rule_only__fetch=_rule_only,
    )

    assert outcome.degraded == service.RULE_ONLY
    assert outcome.cold_start is False
    # 개인화를 끈 목록(모두 0점)이 아니라, 프로필의 카테고리 점수로 채점한 목록이다(#171).
    assert got == [{"한국문학": 4}]
    assert [item["match_score"] for item in outcome.items] == [25]


def test_더_볼_것이_있으면_다음_페이지_커서를_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")

    async def _cold(conn, req, page, excluded=None):
        return [{"book_id": 1, "match_score": 0}], True

    outcome = _run(_req(), None, monkeypatch, cold_start__fetch=_cold)

    assert outcome.next_cursor is not None


def test_마지막_페이지면_커서를_주지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _cold(conn, req, page, excluded=None):
        return [], False

    outcome = _run(_req(), None, monkeypatch, cold_start__fetch=_cold)

    assert outcome.next_cursor is None


_PROFILE_ROW = {
    "centroid": "[0.1]",
    "tag_weights": "{}",
    "cold_start": False,
    "profile_version": 1,
    "computed_at": None,
}


async def _vector_down(conn, req, page, centroid, tag_weights, excluded=None):
    raise RuntimeError("벡터 색인 이상")


async def _vector_up(conn, req, page, centroid, tag_weights, excluded=None):
    return [{"book_id": 9, "match_score": 80}], False


async def _rule_only_page(conn, req, page, tag_weights, excluded=None):
    return [{"book_id": page.offset + 1, "match_score": 0}], True


def _rule_only_cursor(monkeypatch: pytest.MonkeyPatch) -> str:
    """벡터가 안 될 때 받은 첫 페이지의 다음 커서."""
    first = _run(
        _req(),
        _PROFILE_ROW,
        monkeypatch,
        personalized__fetch=_vector_down,
        rule_only__fetch=_rule_only_page,
    )
    assert first.degraded == service.RULE_ONLY
    return first.next_cursor


def test_벡터가_계속_안_되면_더보기도_이어서_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 모드를 목록을 만들기 전에 비교하면 "이번엔 개인화겠지"로 단정해 410 을 낸다.
    # 그러면 벡터 장애가 이어지는 동안 첫 페이지만 되풀이하게 된다(#135 리뷰).
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")
    cursor = _rule_only_cursor(monkeypatch)

    second = _run(
        _req(cursor=cursor),
        _PROFILE_ROW,
        monkeypatch,
        personalized__fetch=_vector_down,
        rule_only__fetch=_rule_only_page,
    )

    assert second.degraded == service.RULE_ONLY
    assert [item["book_id"] for item in second.items] != [1]


def test_벡터가_돌아오면_임시_목록의_커서는_끊는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 임시 목록과 개인화 목록은 순서가 달라 이어 붙일 수 없다(명세 ④).
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")
    cursor = _rule_only_cursor(monkeypatch)

    with pytest.raises(service.cursor.CursorExpired):
        _run(
            _req(cursor=cursor),
            _PROFILE_ROW,
            monkeypatch,
            personalized__fetch=_vector_up,
        )


def test_책_벡터가_한_권도_없으면_규칙_점수만으로_답하고_알린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 모델을 바꾸며 책 벡터를 다시 채우는 동안에는 벡터 조회가 에러 없이 0건이다.
    # 빈 목록으로 두지 않고 벡터를 못 쓸 때와 같이 규칙 점수로 채운다(명세 ④, #210).
    async def _no_match(conn, req, page, centroid, tag_weights, excluded=None):
        return [], False

    async def _no_book_vectors(conn):
        return False

    async def _rule_only(conn, req, page, tag_weights, excluded=None):
        return [{"book_id": 3, "match_score": 25}], False

    outcome = _run(
        _req(),
        _PROFILE_ROW,
        monkeypatch,
        personalized__fetch=_no_match,
        vector__has_any=_no_book_vectors,
        rule_only__fetch=_rule_only,
    )

    assert outcome.degraded == service.RULE_ONLY
    assert [item["book_id"] for item in outcome.items] == [3]


def test_책_벡터가_있는데_0건이면_빈_목록_그대로다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 필터에 맞는 책이 없어 0건인 것은 축소가 아니다(명세: 필터로 0건이면 빈 목록으로 200).
    async def _no_match(conn, req, page, centroid, tag_weights, excluded=None):
        return [], False

    async def _rule_only(conn, req, page, tag_weights, excluded=None):
        raise AssertionError("규칙 점수로 넘어가면 안 된다")

    outcome = _run(
        _req(),
        _PROFILE_ROW,
        monkeypatch,
        personalized__fetch=_no_match,
        rule_only__fetch=_rule_only,
    )

    assert (outcome.items, outcome.degraded) == ([], None)


def test_프로필_뒤에_생긴_이력을_카테고리_점수에_더해_채점한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ⑥ 은 온보딩 때만 불려서, 그 뒤에 산 책은 여기서 더해야 순서에 들어간다(#175).
    reflected = datetime(2026, 9, 1, tzinfo=UTC)
    row = {**_PROFILE_ROW, "tag_weights": '{"에세이": 3}', "computed_at": reflected}
    calls: list[tuple] = []

    async def _recent(conn, user_id, reflected_at, until=None):
        calls.append((user_id, reflected_at, until))
        return {"에세이": 2, "경제학": 3}

    async def _personalized(conn, req, page, centroid, tag_weights, excluded=None):
        assert tag_weights == {"에세이": 5, "경제학": 3}
        return [{"book_id": 9, "match_score": 80}], False

    _run(
        _req(),
        row,
        monkeypatch,
        history__category_scores_since=_recent,
        personalized__fetch=_personalized,
    )

    # 프로필이 반영한 시각 뒤부터, 이 목록을 처음 받은 시각까지만 본다.
    ((user_id, after, until),) = calls
    assert (user_id, after) == (1, reflected)
    assert until is not None


def _second_page_cursor(issued_at: datetime, version: int) -> str:
    """issued_at 에 첫 페이지를 받은 개인화 목록의 다음 커서."""
    page = service.cursor.Page(issued_at=issued_at)
    return service.cursor.issue(page, _req(), service.PERSONALIZED, version)


def test_둘째_페이지도_첫_페이지를_받은_시각까지의_이력만_더한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 지금 시각까지 더하면 스크롤하는 사이에 산 책으로 점수가 바뀌어 목록이 밀린다(#176, #210).
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")
    first_page_at = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    untils: list[datetime | None] = []

    async def _recent(conn, user_id, reflected_at, until=None):
        untils.append(until)
        return {}

    _run(
        _req(cursor=_second_page_cursor(first_page_at, 1)),
        _PROFILE_ROW,
        monkeypatch,
        history__category_scores_since=_recent,
        personalized__fetch=_vector_up,
    )

    assert untils == [first_page_at]


def test_페이지_사이에_프로필_판_번호가_바뀌어도_이어서_준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 명세 ④: 프로필이 바뀌면 끊지 않고 갱신된 값으로 이어 붙인다. 판 번호는 커서에 기록만 한다.
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")
    token = _second_page_cursor(datetime.now(UTC), 1)

    outcome = _run(
        _req(cursor=token),
        {**_PROFILE_ROW, "profile_version": 2},
        monkeypatch,
        personalized__fetch=_vector_up,
    )

    assert [item["book_id"] for item in outcome.items] == [9]


# ---------------------------------------------------------------------------
# 뺄 책을 요청으로 받는다(#330)
# ---------------------------------------------------------------------------

_A, _B = "9788936434120", "9788954651135"


def _exclusion_seen(monkeypatch: pytest.MonkeyPatch, profile_row, **params):
    """목록을 만드는 쪽이 넘겨받은 뺄 책의 조건."""
    seen = []

    async def _cold(conn, req, page, excluded=None):
        seen.append(excluded)
        return [], False

    async def _personalized(conn, req, page, centroid, tag_weights, excluded=None):
        seen.append(excluded)
        return [{"book_id": 1}], False

    async def _to_book_ids(conn, isbns):
        return [{_A: 10, _B: 20}[i] for i in isbns if i in (_A, _B)]

    _run(
        _req(**params),
        profile_row,
        monkeypatch,
        cold_start__fetch=_cold,
        personalized__fetch=_personalized,
        isbn__to_book_ids=_to_book_ids,
    )
    return seen[0]


@pytest.mark.parametrize("profile_row", [None, _PROFILE_ROW])
def test_가진_책이_안_왔으면_조건을_넘기지_않아_복제_표로_빼게_한다(
    monkeypatch: pytest.MonkeyPatch, profile_row
) -> None:
    assert _exclusion_seen(monkeypatch, profile_row) is None


@pytest.mark.parametrize("profile_row", [None, _PROFILE_ROW])
def test_가진_책이_오면_book_id로_바꿔_그_책들을_빼는_조건을_넘긴다(
    monkeypatch: pytest.MonkeyPatch, profile_row
) -> None:
    excluded = _exclusion_seen(
        monkeypatch, profile_row, purchased_isbns=_A, library_isbns=_B
    )

    assert excluded.params == ([10, 20],)


def test_책_표에_없는_ISBN은_빼고_넘긴다(monkeypatch: pytest.MonkeyPatch) -> None:
    excluded = _exclusion_seen(monkeypatch, None, purchased_isbns=f"{_A},9780000000001")

    assert excluded.params == ([10],)


def test_가진_책이_없다고_오면_아무것도_빼지_않는_조건을_넘긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 복제 표에 이력이 남아 있어도 읽지 않는다. BE 가 "가진 책이 없다"고 한 것이다.
    excluded = _exclusion_seen(monkeypatch, None, purchased_isbns="")

    assert excluded is not None
    assert excluded.params == ()


def test_벡터를_못_써서_규칙_점수로_갈_때도_같은_조건을_넘긴다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = []

    async def _rule_only(conn, req, page, tag_weights, excluded=None):
        seen.append(excluded)
        return [], False

    async def _to_book_ids(conn, isbns):
        return [10]

    _run(
        _req(purchased_isbns=_A),
        _PROFILE_ROW,
        monkeypatch,
        personalized__fetch=_vector_down,
        rule_only__fetch=_rule_only,
        isbn__to_book_ids=_to_book_ids,
    )

    assert seen[0].params == ([10],)


# ---------------------------------------------------------------------------
# 저장된 관심사별 목록(#454). 기능 플래그가 켜져 있고 목록이 있으면 홈은 그것을 읽는다.
# ---------------------------------------------------------------------------

_SAVED = interest_page.Stored(
    [{"book_id": 7, "interest": "요리", "match_score": 80}], 4, None
)


def _surface(surface: str, **params):
    return parse_query([("user_id", "1"), ("surface", surface), *params.items()])


async def _personalized_page(conn, req, page, centroid, tag_weights, excluded=None):
    return [{"book_id": 9, "match_score": 84}], False


def _saved_item(book_id: int, score: int = 80) -> dict:
    return {
        "book_id": book_id,
        "isbn": None,
        "title": "책",
        "author": None,
        "price": None,
        "cover_url": None,
        "in_stock": False,
        "match_score": score,
        "category": "에세이",
        "pub_year": 2024,
    }


async def _no_vectors(*args, **kwargs):
    raise AssertionError("저장된 목록을 읽을 때는 벡터를 조회하지 않는다")


def _from_saved(monkeypatch, req, count: int, **fakes):
    """플래그를 켜고, 거르고 남은 책이 count 권인 저장된 목록을 둔다."""
    monkeypatch.setenv("CURSOR_SIGNING_KEY", "test-key")
    monkeypatch.setattr(get_settings(), "interest_feed", True)

    async def _stored(conn, user_id):
        return _SAVED

    async def _books(conn, req, books, excluded):
        assert books == _SAVED.books
        # 가진 책이 안 실려 왔으면 다른 목록처럼 복제 표로 뺀다
        assert excluded.template == exclusion.replicated(1, None).template
        return [_saved_item(n) for n in range(count)]

    fakes.setdefault("personalized__fetch", _no_vectors)
    return _run(
        req,
        _PROFILE_ROW,
        monkeypatch,
        interest_page__stored=_stored,
        interest_page__books=_books,
        **fakes,
    )


def test_플래그가_켜지고_저장된_목록이_있으면_홈은_벡터_조회_없이_그_목록을_읽는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome = _from_saved(monkeypatch, _surface("home", size="2"), count=3)

    assert [item["book_id"] for item in outcome.items] == [0, 1]
    assert "category" not in outcome.items[0]  # 응답 칸만 나간다
    assert (outcome.cold_start, outcome.degraded) == (False, None)
    page = cursor.read(_surface("home", size="2", cursor=outcome.next_cursor))
    assert (page.mode, page.feed_version) == (service.INTEREST, 4)


@pytest.mark.parametrize(
    ("surface", "flag", "saved"),
    [
        ("recommend_more", True, _SAVED),  # 더 보기는 아직 지금 방식(#455)
        ("home", False, _SAVED),  # 플래그가 꺼져 있으면 새 칸을 읽지 않는다
        ("home", True, None),  # 목록을 아직 뽑지 않은 사람
    ],
)
def test_그_밖에는_지금처럼_채점한_목록을_준다(
    monkeypatch: pytest.MonkeyPatch,
    surface: str,
    flag: bool,
    saved: interest_page.Stored | None,
) -> None:
    monkeypatch.setattr(get_settings(), "interest_feed", flag)
    asked = []

    async def _stored(conn, user_id):
        asked.append(user_id)
        return saved

    outcome = _run(
        _surface(surface),
        _PROFILE_ROW,
        monkeypatch,
        interest_page__stored=_stored,
        personalized__fetch=_personalized_page,
    )

    assert [item["book_id"] for item in outcome.items] == [9]
    if not flag:
        assert asked == []
