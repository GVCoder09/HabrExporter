"""Downloading and preparing article images."""

from __future__ import annotations

import base64
import io
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from .client import HabrClient, HabrError

log = logging.getLogger(__name__)


class ImageMode(StrEnum):
    """How images are handled when saving."""

    EMBED = "embed"
    """Download and embed in the document: the file is self-contained and opens offline."""

    LINK = "link"
    """Do not download; link to the original image URL."""


_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
)


def sniff_mime(data: bytes, fallback: str = "") -> str:
    """Detect the image MIME type by its signature (server headers cannot always be trusted)."""
    for signature, mime in _SIGNATURES:
        if data.startswith(signature):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:512].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048].lower()):
        return "image/svg+xml"
    if fallback.startswith("image/"):
        return fallback
    return "application/octet-stream"


@dataclass(slots=True)
class ImageData:
    """Downloaded image."""

    url: str
    data: bytes
    mime: str
    width: int | None = None
    height: int | None = None

    @property
    def data_uri(self) -> str:
        return f"data:{self.mime};base64,{base64.b64encode(self.data).decode('ascii')}"


def _raster_size(data: bytes) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            return img.size
    except Exception:  # noqa: BLE001 - Pillow raises assorted exceptions on broken files
        return None, None


#: Formats Word (and python-docx) understand without conversion.
DOCX_NATIVE = {"image/png", "image/jpeg", "image/gif", "image/bmp", "image/tiff"}


@dataclass(slots=True)
class Raster:
    """Raster image suitable for inserting into DOCX."""

    data: bytes
    width: int
    height: int


def _svg_to_png(data: bytes, scale: float = 3.0) -> Raster | None:
    """Render SVG with Qt. Requires a ``QGuiApplication`` to exist."""
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
        from PyQt6.QtGui import QGuiApplication, QImage, QPainter
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError:  # pragma: no cover - Qt is a required dependency
        return None
    if QGuiApplication.instance() is None:
        return None
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        return None
    size = renderer.defaultSize()
    width, height = max(size.width(), 1), max(size.height(), 1)
    image = QImage(int(width * scale), int(height * scale), QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, image.width(), image.height()))
    painter.end()
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return Raster(bytes(buffer.data()), width, height)


def raster_for_docx(image: ImageData) -> Raster | None:
    """Convert an image to a format that can be inserted into DOCX.

    ``width``/``height`` is the logical size in pixels (for SVG, the default size, although the
    raster itself is rendered larger for sharpness). Return ``None`` if conversion failed.
    """
    if image.mime == "image/svg+xml":
        return _svg_to_png(image.data)
    if image.mime in DOCX_NATIVE and image.width and image.height:
        return Raster(image.data, image.width, image.height)
    return to_png(image)


def to_png(image: ImageData) -> Raster | None:
    """Convert a raster image (WebP, BMP, TIFF, first GIF frame...) to PNG with Pillow.

    SVG is not converted this way; use :func:`raster_for_docx` (rendering via Qt) for it.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(image.data)) as img:
            img.seek(0)
            converted = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
            out = io.BytesIO()
            converted.save(out, "PNG")
            return Raster(out.getvalue(), converted.width, converted.height)
    except Exception:  # noqa: BLE001 - Pillow raises assorted exceptions on broken files
        log.warning("Could not convert image %s to PNG", image.url)
        return None


ProgressCallback = Callable[[int, int], None]


class ImageStore:
    """Article image cache: downloads in parallel and serves images by source URL."""

    def __init__(self, client: HabrClient | None = None, *, max_workers: int = 8) -> None:
        self.client = client
        self.max_workers = max_workers
        self._images: dict[str, ImageData] = {}
        self._failed: set[str] = set()
        self._lock = Lock()

    def __contains__(self, url: str) -> bool:
        return url in self._images

    def __len__(self) -> int:
        return len(self._images)

    @property
    def failed(self) -> set[str]:
        return set(self._failed)

    def add(self, url: str, data: bytes, content_type: str = "") -> ImageData:
        """Put an image into the cache (used by the downloader and by tests)."""
        mime = sniff_mime(data, content_type)
        width, height = _raster_size(data) if mime != "image/svg+xml" else (None, None)
        image = ImageData(url=url, data=data, mime=mime, width=width, height=height)
        with self._lock:
            self._images[url] = image
        return image

    def get(self, url: str) -> ImageData | None:
        return self._images.get(url)

    def _fetch_one(self, url: str) -> None:
        assert self.client is not None
        try:
            resource = self.client.download(url)
        except HabrError as exc:
            log.warning("Image was not downloaded: %s", exc)
            with self._lock:
                self._failed.add(url)
            return
        image = self.add(url, resource.content, resource.content_type)
        if not image.mime.startswith("image/"):
            log.warning("%s is not an image (%s)", url, resource.content_type)

    def fetch_all(self, urls: Iterable[str], progress: ProgressCallback | None = None) -> None:
        """Download all images not loaded yet, reporting progress as ``(done, total)``."""
        if self.client is None:
            raise RuntimeError("Downloading images requires a HabrClient")
        todo = [u for u in dict.fromkeys(urls) if u not in self._images and u not in self._failed]
        total = len(todo)
        if progress:
            progress(0, total)
        if not todo:
            return
        done = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for _ in pool.map(self._fetch_one, todo):
                done += 1
                if progress:
                    progress(done, total)
