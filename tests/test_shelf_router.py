"""POST /shelf/scans 라우터 테스트 — 요청 검사, 응답 모양, 오류 나누기(#615). 사진·모델·DB 없이 돈다."""

import pytest
from fastapi.testclient import TestClient

from app.gateway.llm import LLMTimeoutError, LLMUnavailableError
from app.main import app
from app.photo import catalog, fetch, scan
from app.photo.schemas import MAX_IMAGE_REF_CHARS
from app.routers import shelf

client = TestClient(app)

URL = "/shelf/scans"
IMAGE_REF = (
    "https://bucket.s3.ap-northeast-2.amazonaws.com/shelf/a.jpg?X-Amz-Signature=abc"
)


def _book() -> catalog.Book:
    return catalog.Book(
        book_id=1234,
        isbn="9788970507538",
        title="루비 프로그래밍 언어",
        author="Peter Cooper 외",
        publisher="생능출판사",
        pub_year=2012,
        price=15000,
        in_stock=True,
        cover_url=None,
    )


@pytest.fixture
def fake_scan(monkeypatch: pytest.MonkeyPatch) -> dict:
    """진짜 사진을 받거나 모델을 부르지 않게 scan.scan 을 바꾼다. state["result"] 나 state["error"] 로 답을 정한다."""
    state: dict = {"calls": [], "result": scan.ScanResult("none", []), "error": None}

    async def _scan(image_ref: str) -> scan.ScanResult:
        state["calls"].append(image_ref)
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    monkeypatch.setattr(shelf.scan, "scan", _scan)
    return state


def _request(**overrides) -> dict:
    return {"user_id": 123, "image_ref": IMAGE_REF, **overrides}


def test_찾은_책과_못_찾은_책을_계약_모양으로_돌려준다(fake_scan: dict) -> None:
    fake_scan["result"] = scan.ScanResult(
        "shelf",
        [
            scan.Item("FOUND", "루비 프로그래밍 언어", _book(), "text"),
            scan.Item("NOT_FOUND", "COVERT JAVA"),
        ],
    )

    res = client.post(URL, json=_request())

    assert res.status_code == 200
    assert res.json() == {
        "message": "shelf_scan_success",
        "data": {
            "photo_type": "shelf",
            "items": [
                {
                    "state": "FOUND",
                    "read_text": "루비 프로그래밍 언어",
                    "book": {
                        "book_id": 1234,
                        "isbn": "9788970507538",
                        "title": "루비 프로그래밍 언어",
                        "author": "Peter Cooper 외",
                        "publisher": "생능출판사",
                        "year": 2012,
                        "cover_url": None,
                    },
                    "spines": [],
                },
                {
                    "state": "NOT_FOUND",
                    "read_text": "COVERT JAVA",
                    "book": None,
                    "spines": [],
                },
            ],
        },
    }
    assert fake_scan["calls"] == [IMAGE_REF]


def test_책을_못_찾은_것은_오류가_아니라_빈_items다(fake_scan: dict) -> None:
    res = client.post(URL, json=_request())

    assert res.status_code == 200
    assert res.json() == {
        "message": "shelf_scan_success",
        "data": {"photo_type": "none", "items": []},
    }


def test_모르는_칸은_무시한다(fake_scan: dict) -> None:
    res = client.post(URL, json=_request(extra="무시"))

    assert res.status_code == 200


@pytest.mark.parametrize(
    "payload",
    [
        {"image_ref": IMAGE_REF},
        {"user_id": 123},
        _request(user_id="123"),
        _request(user_id=2**31),
        _request(image_ref=""),
        _request(image_ref=123),
        _request(image_ref="a" * (MAX_IMAGE_REF_CHARS + 1)),
        _request(image_ref="https://x/\x00"),
        [],
    ],
)
def test_계약과_다른_요청은_400이고_사진을_보지_않는다(
    fake_scan: dict, payload
) -> None:
    res = client.post(URL, json=payload)

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}
    assert fake_scan["calls"] == []


def test_JSON이_아니면_400이다(fake_scan: dict) -> None:
    res = client.post(
        URL, content=b"{not json", headers={"Content-Type": "application/json"}
    )

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_본문이_너무_크면_413이다(fake_scan: dict) -> None:
    res = client.post(URL, content=b"x" * (shelf.MAX_BODY_BYTES + 1))

    assert res.status_code == 413
    assert res.json() == {"message": "payload_too_large", "data": None}


@pytest.mark.parametrize(
    ("reason", "status", "message"),
    [
        ("blocked", 400, "invalid_request"),
        ("unreachable", 404, "image_not_found"),
        ("too_large", 413, "payload_too_large"),
        ("unsupported", 422, "image_unreadable"),
    ],
)
def test_사진을_쓸_수_없으면_까닭에_맞는_오류다(
    fake_scan: dict, reason: str, status: int, message: str
) -> None:
    fake_scan["error"] = fetch.ImageError(reason)

    res = client.post(URL, json=_request())

    assert res.status_code == status
    assert res.json() == {"message": message, "data": None}


@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (scan.ScanTimeoutError(), 504, "generation_timeout"),
        (LLMTimeoutError("느림"), 504, "generation_timeout"),
        (LLMUnavailableError("장애"), 503, "upstream_unavailable"),
        (RuntimeError("뜻밖"), 500, "internal_server_error"),
    ],
)
def test_모델이_답하지_못하면_503_504로_나눈다(
    fake_scan: dict, error: Exception, status: int, message: str
) -> None:
    fake_scan["error"] = error

    res = client.post(URL, json=_request())

    assert res.status_code == status
    assert res.json() == {"message": message, "data": None}


def test_사진_주소는_로그에_남기지_않는다(
    fake_scan: dict, caplog: pytest.LogCaptureFixture
) -> None:
    # 주소에는 서명이 붙어 있어, 로그를 읽는 사람이 그 사진을 열 수 있다
    fake_scan["error"] = fetch.ImageError("unreachable")

    client.post(URL, json=_request())

    assert "X-Amz-Signature" not in caplog.text
    assert "unreachable" in caplog.text
