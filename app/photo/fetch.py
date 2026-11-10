"""사진을 받아 모델에 보낼 모양으로 바꾼다(#572, #615).

사진은 BE 가 올린 S3 의 짧은 주소(image_ref)로 온다. AI 는 그 주소를 직접 여는데, 요청에 실린 주소를 아무
데나 열면 서버 안쪽 주소를 대신 열어 주는 구멍이 된다. 그래서 설정에 적은 주소로 시작할 때만 연다.

사진은 저장하지 않는다. 메모리에서 줄이고 사진 정보(EXIF: 찍은 위치·기기)를 지운 JPEG 로 바꿔 모델에만 보낸다.
"""

import asyncio
import base64
import io
from typing import Literal
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

# 명세 ③: 사진은 10MB 까지
MAX_IMAGE_BYTES = 10 * 1024 * 1024
# 받는 데 이보다 오래 걸리면 기다려도 소용없는 상태로 본다. 같은 리전 S3 라 보통 1초 안이다.
FETCH_TIMEOUT_SECONDS = 5.0
# 모델에 보내는 사진의 긴 변. 책등 글씨는 작아서 표지보다 크게 보낸다. 1536px 보다 교육장 책장에서 찾은 권 수가
# 97권에서 105권으로 늘었고 시간은 거의 같았다(#615). 크게 보낼수록 입력 토큰과 시간이 는다.
LONG_EDGE_PX = 2048
JPEG_QUALITY = 85
# 휴대폰 사진은 1,200만 화소 안팎이다. 이보다 크면 열기 전에 거절한다. 작은 파일이 풀면 수십 GB 가 되는
# 그림(압축 폭탄)이 메모리를 다 쓰지 않게 한다.
MAX_PIXELS = 40_000_000
ALLOWED_FORMATS = frozenset({"JPEG", "PNG"})

Reason = Literal["blocked", "unreachable", "too_large", "unsupported"]


class ImageError(Exception):
    """사진을 못 받았거나 쓸 수 없다. reason 으로 원인을 가른다(로그·평가용)."""

    def __init__(self, reason: Reason) -> None:
        super().__init__(reason)
        self.reason: Reason = reason


def is_allowed(url: str, prefixes: list[str]) -> bool:
    """설정에 적은 주소(스킴·호스트·포트가 같고 경로가 그 아래)로 시작하는가.

    글자 앞부분만 견주면 `https://버킷.amazonaws.com.다른곳/` 이나 `https://버킷@다른곳/` 같은 주소가
    통과한다. 그래서 주소를 나눠 호스트 칸 전체가 같은지 본다. 경로의 `..` 은 위 폴더로 빠져나갈 수 있어 거절한다.
    """
    parts = urlsplit(url)
    if ".." in parts.path.split("/"):
        return False
    for prefix in prefixes:
        allowed = urlsplit(prefix)
        if (
            parts.scheme == allowed.scheme
            and parts.netloc == allowed.netloc
            and parts.path.startswith(allowed.path)
        ):
            return True
    return False


async def download(
    url: str,
    prefixes: list[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bytes:
    """사진을 받는다. 10MB 를 넘으면 다 받기 전에 끊는다. transport 는 테스트가 가짜 서버를 끼우는 자리다."""
    if not is_allowed(url, prefixes):
        raise ImageError("blocked")
    try:
        # 다른 주소로 넘기는 응답(3xx)은 따라가지 않는다. 따라가면 허용한 주소 검사를 건너뛴다.
        async with (
            httpx.AsyncClient(
                timeout=FETCH_TIMEOUT_SECONDS,
                follow_redirects=False,
                transport=transport,
            ) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                # 주소가 만료됐거나(presigned 10분) 사진이 지워졌다
                raise ImageError("unreachable")
            declared = response.headers.get("content-length")
            if declared is not None and int(declared) > MAX_IMAGE_BYTES:
                raise ImageError("too_large")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES:
                    raise ImageError("too_large")
            return bytes(body)
    except (httpx.HTTPError, ValueError) as e:
        # ValueError 는 content-length 가 숫자가 아닐 때다
        raise ImageError("unreachable") from e


def prepare(raw: bytes) -> str:
    """받은 사진을 모델에 보낼 data URL 로 바꾼다. 방향을 바로잡고, 줄이고, 사진 정보를 지운다."""
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ALLOWED_FORMATS:
                raise ImageError("unsupported")
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise ImageError("too_large")
            # 휴대폰은 픽셀을 돌리지 않고 "90도 돌려 보라"는 표시만 남긴다. 안 돌리면 모델이 옆으로 누운 글자를 읽는다
            upright = ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise ImageError("unsupported") from e
    upright.thumbnail((LONG_EDGE_PX, LONG_EDGE_PX))
    out = io.BytesIO()
    # exif 를 넘기지 않고 새로 저장하므로 원본의 사진 정보는 따라오지 않는다
    upright.save(out, "JPEG", quality=JPEG_QUALITY)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


async def fetch(
    url: str,
    prefixes: list[str],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """사진 주소를 받아 모델에 보낼 data URL 을 돌려준다. 사진 다루기는 CPU 일이라 다른 스레드에서 한다."""
    raw = await download(url, prefixes, transport=transport)
    return await asyncio.to_thread(prepare, raw)
