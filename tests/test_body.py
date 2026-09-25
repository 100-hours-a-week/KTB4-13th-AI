"""app/core/body.py 단위 테스트 — 상한 안에서 본문 읽기(#99).

②(app/routers/embeddings.py, #83)가 쓰던 걸 공용으로 뺀 것이라, 그때 쓰던
검증(헤더 사전 검사·스트리밍 중단)을 그대로 옮겨 확인한다.
"""

import asyncio

import pytest
from starlette.requests import Request

from app.core import body


def _request(headers: dict[str, str], chunks: list[bytes]) -> Request:
    """headers를 달고 chunks를 순서대로 흘려보내는 가짜 Request."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {"type": "http", "headers": raw_headers}

    it = iter(chunks)

    async def receive() -> dict:
        try:
            chunk = next(it)
            return {"type": "http.request", "body": chunk, "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def test_상한_이내면_그대로_돌려준다() -> None:
    req = _request({}, [b'{"a":1}'])
    assert asyncio.run(body.read_limited(req, max_bytes=100)) == b'{"a":1}'


def test_Content_Length가_상한을_넘으면_본문을_안_읽고_None이다() -> None:
    # 본문 자체는 작아도, 헤더가 상한을 넘는다고 주장하면 거기서 바로 거절한다.
    req = _request({"content-length": "1000"}, [b"x"])
    assert asyncio.run(body.read_limited(req, max_bytes=100)) is None


def test_Content_Length_없이_스트리밍_중에_상한을_넘으면_None이다() -> None:
    # chunked라 Content-Length가 없는 경우 — 헤더 검사를 지나쳐도 스트리밍 중에 걸려야 한다.
    chunks = [b"x" * 60, b"x" * 60]  # 합쳐서 120 > 100
    req = _request({}, chunks)
    assert asyncio.run(body.read_limited(req, max_bytes=100)) is None


def test_Content_Length가_틀린_값이어도_스트리밍으로_거른다() -> None:
    # 헤더를 신뢰하지 않는다 — 숫자가 아니거나 실제보다 작게 속여도 스트리밍 중 실측으로 거른다.
    req = _request({"content-length": "abc"}, [b"x" * 200])
    assert asyncio.run(body.read_limited(req, max_bytes=100)) is None


def test_경계값은_통과한다() -> None:
    req = _request({}, [b"x" * 100])
    assert asyncio.run(body.read_limited(req, max_bytes=100)) == b"x" * 100


@pytest.mark.parametrize("size", [99, 100])
def test_상한_이하_다양한_크기는_통과한다(size: int) -> None:
    req = _request({}, [b"x" * size])
    assert asyncio.run(body.read_limited(req, max_bytes=100)) == b"x" * size
