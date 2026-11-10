"""사진 받기(app/photo/fetch.py) 테스트(#572, #615)."""

import asyncio
import base64
import io

import httpx
import pytest
from PIL import Image

from app.photo import fetch

BUCKET = "https://bucket.s3.ap-northeast-2.amazonaws.com/chat-images/"


def _image_bytes(size=(40, 20), fmt="PNG", exif: Image.Exif | None = None) -> bytes:
    out = io.BytesIO()
    image = Image.new("RGB", size, "white")
    if exif is None:
        image.save(out, fmt)
    else:
        image.save(out, fmt, exif=exif)
    return out.getvalue()


def _decode(data_url: str) -> Image.Image:
    prefix = "data:image/jpeg;base64,"
    assert data_url.startswith(prefix)
    return Image.open(io.BytesIO(base64.b64decode(data_url[len(prefix) :])))


@pytest.mark.parametrize(
    "url",
    [
        BUCKET + "7/a.jpg",
        BUCKET + "7/sub/a.png",
    ],
)
def test_허용한_주소_아래면_연다(url: str) -> None:
    assert fetch.is_allowed(url, [BUCKET])


@pytest.mark.parametrize(
    "url",
    [
        "https://other.s3.ap-northeast-2.amazonaws.com/chat-images/7/a.jpg",
        # 호스트 뒤에 다른 이름을 붙인 주소
        "https://bucket.s3.ap-northeast-2.amazonaws.com.evil.example/chat-images/a.jpg",
        # 계정 칸에 허용한 호스트를 넣은 주소
        "https://bucket.s3.ap-northeast-2.amazonaws.com@evil.example/chat-images/a.jpg",
        # 위 폴더로 빠져나가는 경로
        BUCKET + "../secret.jpg",
        "http://bucket.s3.ap-northeast-2.amazonaws.com/chat-images/a.jpg",
        "https://bucket.s3.ap-northeast-2.amazonaws.com/other/a.jpg",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_허용하지_않은_주소는_열지_않는다(url: str) -> None:
    assert not fetch.is_allowed(url, [BUCKET])


def test_허용_목록이_비면_어떤_주소도_열지_않는다() -> None:
    assert not fetch.is_allowed(BUCKET + "a.jpg", [])


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _download(url: str, handler) -> bytes:
    return asyncio.run(fetch.download(url, [BUCKET], transport=_transport(handler)))


def test_사진을_받는다() -> None:
    body = _image_bytes()

    assert (
        _download(BUCKET + "a.png", lambda req: httpx.Response(200, content=body))
        == body
    )


def test_허용하지_않은_주소는_요청을_보내지_않는다() -> None:
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200)

    with pytest.raises(fetch.ImageError) as e:
        _download("https://evil.example/a.png", handler)

    assert e.value.reason == "blocked"
    assert sent == []


@pytest.mark.parametrize("status", [403, 404, 500])
def test_만료되거나_없는_사진은_받지_못한다(status: int) -> None:
    with pytest.raises(fetch.ImageError) as e:
        _download(BUCKET + "a.png", lambda req: httpx.Response(status))

    assert e.value.reason == "unreachable"


def test_다른_주소로_넘기는_응답은_따라가지_않는다() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    with pytest.raises(fetch.ImageError) as e:
        _download(BUCKET + "a.png", handler)

    assert e.value.reason == "unreachable"


def test_크기를_밝힌_큰_사진은_받기_전에_끊는다() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-length": str(fetch.MAX_IMAGE_BYTES + 1)}, content=b""
        )

    with pytest.raises(fetch.ImageError) as e:
        _download(BUCKET + "a.png", handler)

    assert e.value.reason == "too_large"


def test_크기를_밝히지_않은_큰_사진도_넘는_순간_끊는다(monkeypatch) -> None:
    monkeypatch.setattr(fetch, "MAX_IMAGE_BYTES", 10)

    async def chunks():
        yield b"x" * 8
        yield b"x" * 8

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    with pytest.raises(fetch.ImageError) as e:
        _download(BUCKET + "a.png", handler)

    assert e.value.reason == "too_large"


def test_연결이_안_되면_받지_못한다() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(fetch.ImageError) as e:
        _download(BUCKET + "a.png", handler)

    assert e.value.reason == "unreachable"


def test_큰_사진은_긴_변을_줄인다() -> None:
    image = _decode(fetch.prepare(_image_bytes(size=(3000, 1500))))

    assert image.size == (fetch.LONG_EDGE_PX, fetch.LONG_EDGE_PX // 2)


def test_작은_사진은_키우지_않는다() -> None:
    assert _decode(fetch.prepare(_image_bytes(size=(40, 20)))).size == (40, 20)


def test_휴대폰이_남긴_방향대로_세운다() -> None:
    exif = Image.Exif()
    exif[0x0112] = 6  # 오른쪽으로 90도 돌려 보라는 표시
    raw = _image_bytes(size=(40, 20), fmt="JPEG", exif=exif)

    assert _decode(fetch.prepare(raw)).size == (20, 40)


def test_사진_정보를_지운다() -> None:
    exif = Image.Exif()
    exif[0x010F] = "phone-maker"  # 기기 제조사
    raw = _image_bytes(fmt="JPEG", exif=exif)

    assert len(_decode(fetch.prepare(raw)).getexif()) == 0


def test_JPG_PNG가_아니면_쓰지_않는다() -> None:
    with pytest.raises(fetch.ImageError) as e:
        fetch.prepare(_image_bytes(fmt="GIF"))

    assert e.value.reason == "unsupported"


def test_그림이_아니면_쓰지_않는다() -> None:
    with pytest.raises(fetch.ImageError) as e:
        fetch.prepare(b"not an image")

    assert e.value.reason == "unsupported"


def test_화소가_너무_많으면_열기_전에_거절한다(monkeypatch) -> None:
    monkeypatch.setattr(fetch, "MAX_PIXELS", 100)

    with pytest.raises(fetch.ImageError) as e:
        fetch.prepare(_image_bytes(size=(20, 20)))

    assert e.value.reason == "too_large"
