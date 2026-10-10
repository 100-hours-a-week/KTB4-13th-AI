"""⑥ POST /preferences/profile 라우터 테스트 — 요청 검사와 응답 모양.

계산·저장(service.rebuild)은 가짜로 바꿔 끼운다. 계산은 test_profile_compute.py,
저장은 test_profile_service.py 가 본다.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.core import idempotency
from app.main import app
from app.profile.schemas import USED_LIKED_BOOKS, USED_MEMORIES
from app.routers import profile
from app.routers.profile import parse_request

client = TestClient(app)


@pytest.fixture(autouse=True)
def fake_rebuild(monkeypatch: pytest.MonkeyPatch) -> list:
    """받은 (요청, 본문 해시) 를 모아 두고 cold_start=False, profile_version=3 을 돌려준다."""
    calls: list = []

    async def _fake(req, body_hash):
        calls.append((req, body_hash))
        return {
            "message": "profile_success",
            "data": {"cold_start": False, "profile_version": 3},
        }

    monkeypatch.setattr(profile.service, "rebuild", _fake)
    return calls


DIM = 384


def _memory(**overrides) -> dict:
    memory = {"type": "mood", "value": "잔잔한 소설", "vector": [0.0] * DIM, "dim": DIM}
    memory.update(overrides)
    return memory


def _request(**overrides) -> dict:
    body = {
        "user_id": 123,
        "idempotency_key": "prof_20260904_a1b2",
        "onboarding": {
            "reading_times": ["밤"],
            "criteria": ["베스트셀러"],
            "categories": ["에세이", "한국소설"],
            "tags": ["힐링", "성장"],
            "liked_book_ids": [1088, 3310],
        },
        "memories": [_memory()],
    }
    body.update(overrides)
    return body


def test_명세의_예시_요청이면_200과_응답_모양을_돌려준다() -> None:
    res = client.post("/preferences/profile", json=_request())

    assert res.status_code == 200
    assert res.json() == {
        "message": "profile_success",
        "data": {"cold_start": False, "profile_version": 3},
    }


def test_계산이나_저장이_실패하면_공통_형식의_500이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(req, body_hash):
        raise RuntimeError("DB 장애")

    monkeypatch.setattr(profile.service, "rebuild", _boom)

    res = client.post("/preferences/profile", json=_request())

    assert res.status_code == 500
    assert res.json() == {"message": "internal_server_error", "data": None}


def test_같은_키에_다른_본문이면_409다(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _conflict(req, body_hash):
        raise idempotency.IdempotencyConflict

    monkeypatch.setattr(profile.service, "rebuild", _conflict)

    res = client.post("/preferences/profile", json=_request())

    assert res.status_code == 409
    assert res.json() == {"message": "idempotency_conflict", "data": None}


def test_본문_해시는_키_순서와_공백이_달라도_같다(fake_rebuild: list) -> None:
    body = _request()
    client.post("/preferences/profile", json=body)
    reordered = json.dumps(dict(reversed(list(body.items()))), indent=2)
    client.post(
        "/preferences/profile",
        content=reordered.encode(),
        headers={"Content-Type": "application/json"},
    )

    first, second = (body_hash for _, body_hash in fake_rebuild)
    assert first == second


def test_온보딩을_건너뛴_빈_객체와_기억_없음도_통과한다() -> None:
    res = client.post("/preferences/profile", json=_request(onboarding={}, memories=[]))

    assert res.status_code == 200


@pytest.mark.parametrize(
    "field",
    [
        "reading_times",
        "criteria",
        "categories",
        "tags",
        "liked_book_ids",
        "liked_isbns",
    ],
)
def test_온보딩_목록_칸의_null은_빈_목록으로_본다(field: str) -> None:
    # 취향 카드를 지우면 BE 가 그 칸을 null 로 보낸다(#301).
    body = _request(onboarding={field: None})

    assert client.post("/preferences/profile", json=body).status_code == 200
    assert getattr(parse_request(body).onboarding, field) == []


def test_기억의_null은_빈_목록으로_본다() -> None:
    body = _request(memories=None)

    assert client.post("/preferences/profile", json=body).status_code == 200
    assert parse_request(body).memories == []


@pytest.mark.parametrize(
    "overrides",
    [
        # 온보딩 칸 자체는 필수다. 건너뛴 사용자는 {} 를 보낸다.
        {"onboarding": None},
        # 목록이 비었다는 뜻만 받는다. 목록 안의 null 은 받지 않는다.
        {"onboarding": {"tags": [None]}},
        {"onboarding": {"liked_isbns": [None]}},
        {"memories": [None]},
        # 기억 안의 벡터는 목록 칸이지만 비어 있을 수 없다.
        {"memories": [_memory(vector=None)]},
    ],
)
def test_목록_칸이_아닌_null은_400이다(overrides: dict) -> None:
    res = client.post("/preferences/profile", json=_request(**overrides))

    assert res.status_code == 400


# ---------------------------------------------------------------------------
# 요청의 history(#327). 산 책·나의 도서관 책·리뷰를 BE 가 실어 보낸다.
# ---------------------------------------------------------------------------

_A, _B, _C = "9788936434120", "9788954651135", "9788937460449"


def _history(**overrides) -> dict:
    history = {
        "purchased_isbns": [_A, _B],
        "library_isbns": [_C],
        "reviews": [{"isbn": _A, "rating": 4.5, "content": "잔잔한데 울컥했어요"}],
    }
    history.update(overrides)
    return history


def test_history를_받는다() -> None:
    body = _request(history=_history())

    assert client.post("/preferences/profile", json=body).status_code == 200

    history = parse_request(body).history
    assert history.purchased_isbns == [_A, _B]
    assert history.library_isbns == [_C]
    assert [(r.isbn, r.rating) for r in history.reviews] == [(_A, 4.5)]


@pytest.mark.parametrize("body", [_request(), _request(history=None)])
def test_history가_없거나_null이면_안_온_것으로_본다(body: dict) -> None:
    # BE 가 아직 보내지 않는다는 뜻이다. 이때는 지금처럼 복제 표의 이력으로 계산한다.
    assert client.post("/preferences/profile", json=body).status_code == 200
    assert parse_request(body).history is None


@pytest.mark.parametrize(
    "history",
    [
        {},
        {"purchased_isbns": [], "library_isbns": [], "reviews": []},
        {"purchased_isbns": None, "library_isbns": None, "reviews": None},
    ],
)
def test_빈_history는_이력이_없다는_뜻이라_안_온_것과_다르다(history: dict) -> None:
    body = _request(history=history)

    assert client.post("/preferences/profile", json=body).status_code == 200

    parsed = parse_request(body).history
    assert parsed is not None
    assert (parsed.purchased_isbns, parsed.library_isbns, parsed.reviews) == (
        [],
        [],
        [],
    )


@pytest.mark.parametrize(
    "review",
    [
        {"isbn": _A, "rating": 4.5},  # 본문이 없는 리뷰
        {"isbn": _A, "rating": 4.5, "content": None},
        {"isbn": _A, "rating": 4},  # 별점을 정수로 보냄
        {"isbn": _A, "rating": 5.0},
        {"isbn": _A, "rating": 0.5},
    ],
)
def test_리뷰는_본문이_없어도_별점이_정수여도_받는다(review: dict) -> None:
    body = _request(history=_history(reviews=[review]))

    assert client.post("/preferences/profile", json=body).status_code == 200


@pytest.mark.parametrize(
    "history",
    [
        [],  # 객체가 아니다
        _history(purchased_isbns=["978-89-364-3412-0"]),
        _history(library_isbns=[9788936434120]),
        _history(purchased_isbns=[None]),
        _history(reviews=[None]),
        _history(reviews=[{"rating": 4.5, "content": "책이 없는 리뷰"}]),
        _history(reviews=[{"isbn": _A, "content": "별점이 없는 리뷰"}]),
        _history(reviews=[{"isbn": "897364341X", "rating": 4.5}]),
        _history(reviews=[{"isbn": _A, "rating": "4.5"}]),
        _history(reviews=[{"isbn": _A, "rating": None}]),
        _history(reviews=[{"isbn": _A, "rating": -1}]),
        _history(reviews=[{"isbn": _A, "rating": 4.5, "content": "끝\x00"}]),
    ],
)
def test_history가_계약과_다르면_400이다(history: object) -> None:
    res = client.post("/preferences/profile", json=_request(history=history))

    assert res.status_code == 400


@pytest.mark.parametrize("rating", [5.5, 8, 10])
def test_별점이_5점을_넘으면_400이다(rating: float) -> None:
    # BE 코드는 10.0 까지 받는다(#307 남은 결정). 10점 만점으로 오면 거의 모든 리뷰가 "좋음"으로
    # 계산되고 에러가 나지 않으므로, 범위가 정해질 때까지 막아 둔다.
    body = _request(history=_history(reviews=[{"isbn": _A, "rating": rating}]))

    assert client.post("/preferences/profile", json=body).status_code == 400


@pytest.mark.parametrize("field", ["user_id", "idempotency_key", "onboarding"])
def test_필수_칸이_없으면_400이다(field: str) -> None:
    body = _request()
    del body[field]

    res = client.post("/preferences/profile", json=body)

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


@pytest.mark.parametrize(
    ("field", "limit"),
    [("reading_times", 5), ("criteria", 3), ("categories", 3), ("tags", 9)],
)
def test_온보딩_배열이_상한을_넘으면_400이고_상한_정확히는_통과한다(
    field: str, limit: int
) -> None:
    def _post(count: int) -> int:
        onboarding = {field: [f"값{i}" for i in range(count)]}
        return client.post(
            "/preferences/profile", json=_request(onboarding=onboarding)
        ).status_code

    assert _post(limit) == 200
    assert _post(limit + 1) == 400


def test_좋아한_책과_기억은_상한을_넘어도_400이_아니라_잘라_쓴다() -> None:
    body = _request(
        onboarding={"liked_book_ids": list(range(1, 81))},
        memories=[_memory(value=f"기억{i}") for i in range(600)],
    )

    assert client.post("/preferences/profile", json=body).status_code == 200

    req = parse_request(body)
    assert req.used_liked_book_ids() == list(range(1, USED_LIKED_BOOKS + 1))
    # 기억은 배열 뒤쪽을 최근으로 본다.
    used = req.used_memories()
    assert len(used) == USED_MEMORIES
    assert used[0].value == "기억100"
    assert used[-1].value == "기억599"


def test_ISBN으로_보낸_좋아한_책도_상한을_넘으면_앞에서부터_잘라_쓴다() -> None:
    isbns = [str(9780000000000 + i) for i in range(80)]
    body = _request(onboarding={"liked_isbns": isbns})

    assert client.post("/preferences/profile", json=body).status_code == 200

    req = parse_request(body)
    assert req.used_liked_isbns() == isbns[:USED_LIKED_BOOKS]


@pytest.mark.parametrize(
    "memory",
    [
        _memory(dim=1024, vector=[0.0] * 1024),  # 명세 예시의 옛 모델 차원
        _memory(vector=[0.0] * (DIM - 1)),  # dim 만 맞고 길이가 다름
    ],
)
def test_기억_벡터가_인덱스_차원과_다르면_400이다(memory: dict) -> None:
    res = client.post("/preferences/profile", json=_request(memories=[memory]))

    assert res.status_code == 400


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_기억_벡터에_NaN이나_무한대가_섞이면_400이다(bad: str) -> None:
    # 파이썬 json 은 NaN·Infinity 를 그대로 읽는다. 문자열로 직접 만들어 보낸다.
    vector = ", ".join(["0.0"] * (DIM - 1) + [bad])
    body = (
        '{"user_id": 1, "idempotency_key": "k", "onboarding": {},'
        f' "memories": [{{"type": "mood", "value": "v", "vector": [{vector}], "dim": {DIM}}}]}}'
    )

    res = client.post(
        "/preferences/profile",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert res.status_code == 400


@pytest.mark.parametrize(
    "overrides",
    [
        {"user_id": "123"},  # 문자열을 숫자로 바꿔 받지 않는다
        {"idempotency_key": ""},  # 빈 키끼리는 멱등 처리에서 서로 부딪친다
        {"onboarding": {"liked_book_ids": ["1088"]}},
        # ISBN 은 13자리 숫자로 된 문자열이다(#315).
        {"onboarding": {"liked_isbns": ["978-89-364-3412-0"]}},
        {"onboarding": {"liked_isbns": ["897364341X"]}},
        {"onboarding": {"liked_isbns": [9788936434120]}},
        {"memories": [_memory(vector=["0.1"] * DIM)]},
        {"memories": {"type": "mood"}},
        # #201 — int32 범위·NUL·짝 없는 서로게이트.
        {"user_id": 2_147_483_648},
        {"onboarding": {"liked_book_ids": [2_147_483_648]}},
        {"onboarding": {"tags": ["힐링\x00"]}},
    ],
)
def test_타입이_계약과_다르면_400이다(overrides: dict) -> None:
    res = client.post("/preferences/profile", json=_request(**overrides))

    assert res.status_code == 400


def test_idempotency_key에_짝_없는_서로게이트가_있으면_400이다() -> None:
    """#201 — httpx 클라이언트는 실제 서로게이트를 담은 str을 그대로 못 보낸다.
    실제 공격 벡터(JSON escape가 와이어를 타고 서버에서 풀리는 경우)를 재현하려면
    본문을 raw bytes로 직접 보내야 한다(search 라우터 테스트와 같은 이유).
    """
    body = _request(idempotency_key="__MARKER__")
    payload = json.dumps(body, ensure_ascii=False).replace('"__MARKER__"', '"\\ud800"')
    res = client.post(
        "/preferences/profile",
        content=payload.encode(),
        headers={"Content-Type": "application/json"},
    )

    assert res.status_code == 400


@pytest.mark.parametrize("content", [b"not json", b"\xff\xfe{", b"[1, 2]"])
def test_본문이_JSON_객체가_아니면_400이다(content: bytes) -> None:
    res = client.post(
        "/preferences/profile",
        content=content,
        headers={"Content-Type": "application/json"},
    )

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_본문이_상한을_넘으면_413이다() -> None:
    # 헤더만 크게 속여도 본문을 읽기 전에 막아야 한다(#99).
    res = client.post(
        "/preferences/profile",
        content=b'{"user_id":1}',
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(profile.MAX_BODY_BYTES + 1),
        },
    )

    assert res.status_code == 413
    assert res.json() == {"message": "payload_too_large", "data": None}
