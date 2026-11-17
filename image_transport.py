"""Validated render artifacts; URL downloads never use BATrace credentials."""

from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit
import warnings

import aiohttp
from PIL import Image
from yarl import URL


IMAGE_SUFFIXES = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
MAX_IMAGE_BYTES = 32 * 1024 * 1024


class ImageTransportError(ValueError):
    """Only controlled stage/reason codes may enter image error messages."""

    def __init__(self, stage, reason, *, status_code=None, content_type=None):
        super().__init__(reason)
        self.stage = stage
        self.reason = reason
        self.status_code = status_code
        self.content_type = content_type


@dataclass(frozen=True)
class ImageArtifact:
    path: Path
    image_type: str
    width: int
    height: int
    image_bytes: int
    url: str | None = field(default=None, repr=False)

    @property
    def metadata(self):
        return {"path": self.path.name, "image_type": self.image_type,
                "width": self.width, "height": self.height, "image_bytes": self.image_bytes}


def validate_image(data: bytes) -> tuple[str, int, int]:
    """Verify the container and decode every frame; a suffix is not evidence."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ImageTransportError("validate", "invalid_image_size")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as picture:
                image_type, (width, height) = picture.format, picture.size
                if image_type not in IMAGE_SUFFIXES or width <= 0 or height <= 0:
                    raise ImageTransportError("validate", "unsupported_image")
                picture.verify()
            with Image.open(BytesIO(data)) as picture:
                for frame in range(getattr(picture, "n_frames", 1)):
                    picture.seek(frame)
                    picture.load()
    except ImageTransportError:
        raise
    except Exception as exc:
        raise ImageTransportError("validate", "invalid_image") from exc
    return image_type, width, height


def is_image_url(source) -> bool:
    """Accept real HTTP(S) references only; never turn a local path into a URL."""
    if not isinstance(source, str):
        return False
    try:
        parsed = urlsplit(source)
        return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


async def download_image(url: str, timeout: float) -> tuple[bytes, str]:
    """Download once with bounded redirects/body size and no request URL logging."""
    # aiohttp does not emit httpx's INFO request line containing signed URLs.
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=True) as session:
        async with session.get(URL(url, encoded=True), max_redirects=5) as response:
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if not 200 <= response.status < 300:
                raise ImageTransportError("download", "http_status", status_code=response.status,
                                          content_type=content_type)
            if content_type and not (content_type.startswith("image/") or content_type == "application/octet-stream"):
                raise ImageTransportError("download", "invalid_content_type", content_type=content_type)
            if response.content_length is not None and response.content_length > MAX_IMAGE_BYTES:
                raise ImageTransportError("download", "invalid_image_size", content_type=content_type)
            body = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES:
                    raise ImageTransportError("download", "invalid_image_size", content_type=content_type)
            return bytes(body), content_type
