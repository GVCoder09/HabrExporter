"""Common types for exporters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import tzinfo
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from lxml import etree

from ..images import ImageMode, ImageStore
from ..models import Article
from ..parser import image_urls


class ExportFormat(StrEnum):
    """Supported output formats (in the order shown to the user)."""

    PDF = "pdf"
    HTML = "html"
    DOCX = "docx"
    EPUB = "epub"
    FB2 = "fb2"
    MARKDOWN = "md"
    JSON = "json"

    @property
    def extension(self) -> str:
        return "." + self.value

    @property
    def beta(self) -> bool:
        """Whether the format is still being polished.

        Rarely, the layout may differ from the site.
        """
        return self in _BETA

    @property
    def label(self) -> str:
        """Return the UI name: ``PDF``, ``HTML (beta)``..."""
        name = self.value.upper()
        return f"{name} (beta)" if self.beta else name

    @property
    def needs_qt(self) -> bool:
        """Whether a ``QApplication`` is needed.

        PDF is printed by Qt WebEngine, and SVG formulas for DOCX and FB2 are converted to PNG
        via Qt.
        """
        return self in _NEEDS_QT

    @property
    def embeds_images(self) -> bool:
        """Whether the format can store images inside the file. JSON cannot: it only keeps URLs."""
        return self is not ExportFormat.JSON


_BETA = frozenset({ExportFormat.DOCX, ExportFormat.EPUB, ExportFormat.FB2, ExportFormat.MARKDOWN})
_NEEDS_QT = frozenset({ExportFormat.PDF, ExportFormat.DOCX, ExportFormat.FB2})


@dataclass(slots=True)
class ExportOptions:
    """Export options."""

    format: ExportFormat = ExportFormat.PDF
    image_mode: ImageMode = ImageMode.EMBED
    include_comments: bool = True
    tz: tzinfo | None = None
    """Time zone for dates; ``None`` means the local time zone."""

    @property
    def downloads_images(self) -> bool:
        """Whether images must be downloaded before saving.

        DOCX downloads them in link mode too: Word needs each picture's size, and formats it cannot
        link to (SVG formulas, WebP) are still embedded as PNG.
        """
        if self.image_mode is ImageMode.LINK:
            return self.format is ExportFormat.DOCX
        return self.format.embeds_images


class Exporter(Protocol):
    """Exporter interface: saves an article to a file and returns its path."""

    extension: str
    title: str

    def export(
        self, article: Article, path: Path, options: ExportOptions, images: ImageStore | None
    ) -> Path: ...  # pragma: no cover


def section_headings(root: etree._Element) -> tuple[str, ...]:
    """Return the heading tags that split the article into sections (for e-book tables of contents).

    This is the top level present in the article: some authors mark sections with ``<h2>``, others
    start at ``<h3>``. An ``<h1>`` inside the article counts as the same level as ``<h2>``.
    """
    levels = [int(h.tag[1]) for h in root.iter("h1", "h2", "h3", "h4", "h5", "h6")]
    if not levels:
        return ()
    top = min(levels)
    return ("h1", "h2") if top <= 2 else (f"h{top}",)


def collect_image_urls(article: Article, *, include_comments: bool = True) -> list[str]:
    """Return every image the document will contain: body, comments, avatars, logo."""
    urls = image_urls(article.body_html)
    for comment in article.iter_comments() if include_comments else ():
        urls += image_urls(comment.html)
        if comment.author and comment.author.avatar_url:
            urls.append(comment.author.avatar_url)
    if article.author.avatar_url:
        urls.append(article.author.avatar_url)
    if article.company and article.company.logo_url:
        urls.append(article.company.logo_url)
    return list(dict.fromkeys(urls))
