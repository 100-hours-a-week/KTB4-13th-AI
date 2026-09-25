"""요청 본문을 상한 안에서 읽는다 — 넘으면 호출부가 413으로 답한다(#99).

②(`/embeddings`, #83)가 먼저 만든 패턴을 여러 라우터가 같이 쓰게 뺐다. Starlette의
`RequestBodyLimitMiddleware`를 안 쓰는 이유: 그 미들웨어는 413을 평문
`Content Too Large`로 돌려줘 계약({"message": "payload_too_large", "data": null})이
깨진다. 응답 형식은 각 라우터가 이미 쓰는 `responses.error(413, "payload_too_large")`로
맞추고, 이 함수는 "넘었는지"만 판단한다.
"""

from fastapi import Request


async def read_limited(request: Request, max_bytes: int) -> bytes | None:
    """본문을 조각씩 받으며 크기를 센다. 상한을 넘는 순간 멈추고 None을 돌려준다.

    Content-Length 헤더 검사만으로는 부족하다 — 그 헤더를 빼고(chunked) 보내면
    이 사전 검사를 지나치고, `request.body()`로 한 번에 받으면 본문을 이미 끝까지
    메모리에 올린 뒤에야 크기를 안다(거절 자체가 공격 수단이 된다). 그래서 헤더로
    먼저 값싸게 거르고, 그래도 통과하면 스트리밍하며 직접 센다.
    """
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > max_bytes
    ):
        return None

    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
