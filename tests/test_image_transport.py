"""Offline decoder and HTTP response contracts; no sockets are opened."""

from io import BytesIO

import pytest
from PIL import Image

import image_transport as transport


def picture_bytes(kind="PNG"):
    stream = BytesIO()
    Image.new("RGB", (12, 8), "blue").save(stream, format=kind)
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["JPEG", "PNG", "WEBP", "GIF"])
def test_complete_image_decode_reports_actual_format_and_dimensions(kind):
    assert transport.validate_image(picture_bytes(kind)) == (kind, 12, 8)


@pytest.mark.parametrize("body", [b"Internal Server Error"[:20], b'{"error":"timeout"} ',
                                   b"https://bad.invalid/", b"\xff\xd8\xff" + b"x" * 17,
                                   picture_bytes("PNG")[:-20], picture_bytes("JPEG")[:-20]])
def test_error_bodies_and_truncated_images_are_rejected(body):
    with pytest.raises(transport.ImageTransportError, match="invalid_image"):
        transport.validate_image(body)


@pytest.mark.parametrize("status,content_type,body,reason", [
    (503, "text/plain", b"Internal Server Err ", "http_status"),
    (200, "application/json", b'{"error":"timeout"}', "invalid_content_type"),
    (200, "image/jpeg", b"Internal Server Err ", None),
    (200, "application/octet-stream", picture_bytes(), None),
])
async def test_http_response_is_checked_before_decoding(monkeypatch, status, content_type, body, reason):
    calls = []

    class Response:
        headers = {"Content-Type": content_type}
        content_length = len(body)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            yield body

    response = Response()
    response.status, response.content = status, response

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total == 7

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            calls.append((str(url), kwargs))
            return response

    monkeypatch.setattr(transport.aiohttp, "ClientSession", Session)
    url = "https://render.invalid/card?signature=do%2Fnot%2Bchange"
    if reason:
        with pytest.raises(transport.ImageTransportError, match=reason) as caught:
            await transport.download_image(url, 7)
        assert caught.value.stage == "download"
    else:
        data, mime = await transport.download_image(url, 7)
        assert data == body and mime == content_type
    assert calls == [(url, {"max_redirects": 5})]


@pytest.mark.parametrize("source", ["/tmp/image.jpg", "file:///tmp/image.jpg", "base64://secret", "https://user:pass@host/img", "https://"])
def test_local_or_credential_bearing_reference_is_not_a_downloadable_url(source):
    assert not transport.is_image_url(source)
