"""② POST /embeddings 라우터 테스트.

모델을 읽지 않으려고 gateway.embed 를 가짜로 바꿔 끼운다.
벡터 값 자체는 어댑터 테스트(test_embedding_gateway.py)가 검증한다.
여기서는 계약(상태 코드·응답 봉투·검증 경계)만 본다.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import embeddings

client = TestClient(app)


@pytest.fixture(autouse=True)
def fake_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """입력 개수만큼 가짜 384차원 벡터를 돌려준다."""

    async def _fake(
        texts: list[str], purpose: str
    ) -> tuple[list[list[float]], int, str]:
        return [[0.1] * 384 for _ in texts], 384, "intfloat/multilingual-e5-small"

    monkeypatch.setattr(embeddings.embedding, "embed", _fake)


def test_정상_요청이면_200과_계약_봉투를_돌려준다() -> None:
    res = client.post(
        "/embeddings", json={"texts": ["쓸쓸한 위로"], "purpose": "query"}
    )

    assert res.status_code == 200
    body = res.json()
    assert body["message"] == "embed_success"
    assert body["data"]["dim"] == 384
    assert len(body["data"]["vectors"]) == 1
    assert len(body["data"]["vectors"][0]) == 384
    assert body["data"]["model"] == "intfloat/multilingual-e5-small"


def test_purpose가_없으면_기본값_document로_처리한다() -> None:
    res = client.post("/embeddings", json={"texts": ["잔잔한 에세이"]})

    assert res.status_code == 200


def test_여러_건이면_입력과_같은_개수를_돌려준다() -> None:
    res = client.post("/embeddings", json={"texts": ["첫째", "둘째", "셋째"]})

    assert len(res.json()["data"]["vectors"]) == 3


def test_texts가_비면_400이다() -> None:
    res = client.post("/embeddings", json={"texts": []})

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_texts가_상한_256건을_넘으면_400이다() -> None:
    res = client.post("/embeddings", json={"texts": ["책"] * 257})

    assert res.status_code == 400


def test_상한_256건_정확히는_통과한다() -> None:
    res = client.post("/embeddings", json={"texts": ["책"] * 256})

    assert res.status_code == 200


def test_빈_문자열이_섞이면_400이다() -> None:
    res = client.post("/embeddings", json={"texts": ["정상", "   "]})

    assert res.status_code == 400


def test_texts가_없으면_400이다() -> None:
    res = client.post("/embeddings", json={"purpose": "query"})

    assert res.status_code == 400


def test_purpose가_enum_밖이면_400이다() -> None:
    res = client.post("/embeddings", json={"texts": ["책"], "purpose": "passage"})

    assert res.status_code == 400


def test_texts가_문자열_배열이_아니면_400이다() -> None:
    res = client.post("/embeddings", json={"texts": [1, 2, 3]})

    assert res.status_code == 400


def test_본문이_JSON이_아니면_400이다() -> None:
    res = client.post(
        "/embeddings",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )

    assert res.status_code == 400


def test_본문이_UTF8이_아니면_500이_아니라_400이다() -> None:
    res = client.post(
        "/embeddings",
        content=b"\xff\xfe{",
        headers={"Content-Type": "application/json"},
    )

    assert res.status_code == 400
    assert res.json() == {"message": "invalid_request", "data": None}


def test_본문이_4MB를_넘으면_413이다() -> None:
    # 헤더만 크게 속여도 본문을 읽기 전에 막아야 한다.
    res = client.post(
        "/embeddings",
        content=b'{"texts":["x"]}',
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(5 * 1024 * 1024),
        },
    )

    assert res.status_code == 413
    assert res.json() == {"message": "payload_too_large", "data": None}


def test_임베딩이_실패하면_503_upstream_unavailable이다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 모델 파일이 없거나 차원이 어긋난 상황. 500이 아니라 503으로 알려야
    # 호출자가 "임베딩이 죽었다"를 구분해 키워드 전용으로 강등할 수 있다.
    async def _fail(texts: list[str], purpose: str) -> None:
        raise RuntimeError("모델 차원 불일치")

    monkeypatch.setattr(embeddings.embedding, "embed", _fail)

    res = client.post("/embeddings", json={"texts": ["책"]})

    assert res.status_code == 503
    assert res.json() == {"message": "upstream_unavailable", "data": None}
