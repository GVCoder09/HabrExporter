"""Single-file HTML export that opens in any browser, including on a phone.

Styles are embedded in ``<style>``. In :attr:`ImageMode.EMBED` mode images are embedded as
``data:`` URIs, so the file is self-contained and works offline. In :attr:`ImageMode.LINK` mode
(and for images that failed to download) the original URLs remain, and the browser loads them
from Habr when viewing.
"""

from __future__ import annotations

from pathlib import Path

from ..images import ImageMode, ImageStore
from ..models import Article
from ..render_html import ImageSrc, render_article
from .base import ExportOptions


def make_image_src(mode: ImageMode, images: ImageStore | None) -> ImageSrc:
    """Return the HTML image policy: embed if available, otherwise link to the original."""

    def resolve(url: str) -> str:
        if mode is ImageMode.EMBED and images is not None:
            image = images.get(url)
            if image is not None:
                return image.data_uri
        return url

    return resolve


class HtmlExporter:
    """Exporter to a single ``.html`` file."""

    extension = ".html"
    title = "HTML"

    def export(
        self, article: Article, path: Path, options: ExportOptions, images: ImageStore | None
    ) -> Path:
        html = render_article(
            article,
            make_image_src(options.image_mode, images),
            tz=options.tz,
            include_comments=options.include_comments,
        )
        path = Path(path)
        path.write_text(html, encoding="utf-8")
        return path
