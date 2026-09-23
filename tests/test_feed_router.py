"""④ GET /recommendations/feed 라우터 테스트 — 요청 검사와 응답 모양. DB 없이 돈다."""

import pytest
from fastapi.testclient import TestClient

from app.feed.schemas import MAX_SIZE, FeedRequest, parse_query
from app.feed.service import FeedOutcome
from app.main import app
from app.routers import feed as feed_router

client = TestClient(app)

URL = "/recommendations/feed"


def _get(params: dict | list):
    return client.get(URL, params=params)


_BOOK = {
    "book_id": 3310,
    "title": "아무튼, 산",
    "author": "장보영",
    "price": 9900,
    "cover_url": None,
    "in_stock": True,
    "match_score": 0,
}


@pytest.fixture(autouse=True)
def no_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 없이 돈다. 기본은 0건. 목록 자체는 test_feed_cold_start.py 가 본다."""

    async def _feed(req: FeedRequest) -> FeedOutcome:
        return FeedOutcome(items=[])

    monkeypatch.setattr(feed_router.service, "feed", _feed)


def test_기능을_줄여_응답했으면_헤더로_알린다(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _feed(req: FeedRequest) -> FeedOutcome:
        return FeedOutcome(items=[_BOOK], degraded="rule-only", cold_start=False)

    monkeypatch.setattr(feed_router.service, "feed", _feed)

    res = _get({"user_id": 123, "surface": "home"})

    assert res.headers["x-degraded"] == "rule-only"
    assert res.json()["data"]["cold_start"] is False


def test_온전한_응답에는_헤더가_없다() -> None:
    res = _get({"user_id": 123, "surface": "home"})

    assert "x-degraded" not in res.headers


def test_목록_조회가_실패하면_공통_형식의_500을_돌려준다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _feed(req: FeedRequest) -> FeedOutcome:
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(feed_router.service, "feed", _feed)

    res = _get({"user_id": 123, "surface": "home"})

    assert res.status_code == 500
    assert res.json() == {"message": "internal_server_error", "data": None}


def test_목록을_items_에_싣고_cold_start_로_답한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _feed(req: FeedRequest) -> FeedOutcome:
        return FeedOutcome(items=[_BOOK])

    monkeypatch.setattr(feed_router.service, "feed", _feed)

    res = _get({"user_id": 123, "surface": "home"})

    assert res.json()["data"] == {
        "items": [_BOOK],
        "next_cursor": None,
        "cold_start": True,
    }


def test_home_요청이면_200과_응답_모양을_돌려준다() -> None:
    res = _get({"user_id": 123, "surface": "home", "size": 15})

    assert res.status_code == 200
    assert res.json() == {
        "message": "feed_success",
        "data": {"items": [], "next_cursor": None, "cold_start": True},
    }


def test_개인화_목록이라_캐시하지_않게_한다() -> None:
    res = _get({"user_id": 123, "surface": "home"})

    assert res.headers["cache-control"] == "private, no-store"


def test_recommend_more는_정렬과_필터를_모두_받는다() -> None:
    res = _get(
        {
            "user_id": 123,
            "surface": "recommend_more",
            "sort": "newest",
            "category": "에세이",
            "pub_year_from": 2020,
            "pub_year_to": 2024,
            "match_score_min": 70,
            "size": 15,
            "cursor": "eyJ.abc",
        }
    )

    assert res.status_code == 200


def test_기본값은_정렬_match_개수_15다() -> None:
    req = parse_query([("user_id", "1"), ("surface", "home")])

    assert req is not None
    assert (req.sort, req.size) == ("match", 15)


def test_허용_목록에_없는_키가_오면_400이다() -> None:
    res = _get({"user_id": 123, "surface": "home", "page": 2})

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_같은_키가_두_번_오면_400이다() -> None:
    res = _get([("user_id", "1"), ("user_id", "2"), ("surface", "home")])

    assert res.status_code == 400


@pytest.mark.parametrize("missing", ["user_id", "surface"])
def test_필수_파라미터가_없으면_400이다(missing: str) -> None:
    params = {"user_id": 123, "surface": "home"}
    del params[missing]

    assert _get(params).status_code == 400


@pytest.mark.parametrize(
    "param",
    [
        {"sort": "match"},
        {"sort": "newest"},
        {"category": "에세이"},
        {"pub_year_from": 2020},
        {"match_score_min": 50},
    ],
)
def test_home은_정렬이나_필터를_보내면_400이다(param: dict) -> None:
    res = _get({"user_id": 123, "surface": "home", **param})

    assert res.status_code == 400


@pytest.mark.parametrize(
    "override",
    [
        {"surface": "search"},
        {"sort": "popular"},
        {"user_id": "abc"},
        {"size": 0},
        {"size": MAX_SIZE + 1},
        {"match_score_min": 101},
        {"match_score_min": -1},
        {"pub_year_from": 2024, "pub_year_to": 2020},
        {"category": ""},
    ],
)
def test_값이_계약과_다르면_400이다(override: dict) -> None:
    params = {"user_id": 123, "surface": "recommend_more", **override}

    assert _get(params).status_code == 400


def test_size_상한_정확히는_통과한다() -> None:
    res = _get({"user_id": 123, "surface": "home", "size": MAX_SIZE})

    assert res.status_code == 200
