"""JSON export, for those who will process saved articles with a program.

The ``habr-exporter/article`` schema, version 1, is described in ``docs/architecture.md``. In short:

* dates are ISO 8601 with a time zone (as received from the API), numbers are numbers, missing
  values are ``null``;
* the article body is sanitized HTML (``body_html``) and plain text with paragraphs separated by
  blank lines (``body_text``);
* comments are a flat list in site display order; the tree is restored from ``parent_id``;
* images are not embedded (base64 bloats the file): ``images`` holds only their URLs, so the image
  mode from :class:`ExportOptions` has no effect here.

``version`` is incremented on incompatible schema changes; new fields are added without that.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lxml import etree
from lxml import html as lhtml

from ..images import ImageStore
from ..models import Article, Comment, Company, User
from .base import ExportOptions, collect_image_urls

SCHEMA = "habr-exporter/article"
SCHEMA_VERSION = 1

#: Article complexity: a human-readable label in the model, the API value in JSON.
_COMPLEXITY_CODES = {"Простой": "low", "Средний": "medium", "Сложный": "high"}
_WS_RE = re.compile(r"\s+")
_BLOCK_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "aside",
    "main",
    "figure",
    "figcaption",
    "blockquote",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "caption",
    "hr",
    "details",
    "summary",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}


def _date(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _plain(value: Any) -> Any:
    """Convert the result of :func:`dataclasses.asdict` to JSON types."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _user(user: User) -> dict[str, Any]:
    return {**_plain(asdict(user)), "url": user.url}


def _company(company: Company | None) -> dict[str, Any] | None:
    return {**_plain(asdict(company)), "url": company.url} if company else None


class _TextBuilder:
    """Converts an HTML fragment to plain text: blocks become paragraphs, ``<br>`` a line break.

    List items get a "•" bullet or a number (nested ones are indented), table rows are cells
    separated by tabs, code blocks are output as is, formulas as their TeX source.
    """

    def __init__(self) -> None:
        self.paragraphs: list[str] = []
        self._line: list[str] = []
        self._prefix = ""
        """List item marker: goes to the first non-empty paragraph of the item."""

    def build(self, markup: str) -> str:
        if markup.strip():
            root = lhtml.fragment_fromstring(markup, create_parent="div")
            self._inline_text(root.text)
            for child in root:
                self._node(child, depth=0)
            self._flush()
        return "\n\n".join(self.paragraphs)

    def _inline_text(self, text: str | None) -> None:
        if text:
            self._line.append(_WS_RE.sub(" ", text))

    def _flush(self) -> None:
        # Line breaks here come only from <br>: source whitespace is already collapsed.
        lines = (" ".join(line.split()) for line in "".join(self._line).split("\n"))
        text = "\n".join(line for line in lines if line)
        if text:
            self.paragraphs.append(self._prefix + text)
            self._prefix = ""
        self._line = []

    def _children(self, element: etree._Element, depth: int) -> None:
        self._inline_text(element.text)
        for child in element:
            self._node(child, depth)

    def _node(self, element: etree._Element, depth: int) -> None:
        tag = element.tag if isinstance(element.tag, str) else ""
        if tag == "br":
            self._line.append("\n")
        elif tag == "img":
            if "formula" in (element.get("class") or "").split():
                self._line.append(element.get("source") or element.get("alt") or "")
        elif tag == "pre":
            self._flush()
            code = element.text_content().strip("\n").expandtabs(4)
            if code.strip():
                self.paragraphs.append(code)
        elif tag == "li":
            self._flush()
            parent = element.getparent()
            if parent is not None and parent.tag == "ol":
                items = [li for li in parent if li.tag == "li"]
                marker = f"{int(parent.get('start') or 1) + items.index(element)}."
            else:
                marker = "•"
            self._prefix = "    " * depth + marker + " "
            self._children(element, depth + 1)
            self._flush()
            self._prefix = ""
        elif tag == "tr":
            self._flush()
            cells = [
                " ".join(cell.text_content().split())
                for cell in element
                if cell.tag in ("td", "th")
            ]
            if any(cells):
                self.paragraphs.append("\t".join(cells))
        elif tag in _BLOCK_TAGS:
            self._flush()
            self._children(element, depth)
            self._flush()
        elif tag:
            self._children(element, depth)
        self._inline_text(element.tail)


def html_to_text(markup: str) -> str:
    """Return the plain text of an HTML fragment, with paragraphs separated by blank lines."""
    return _TextBuilder().build(markup or "")


def _image_alts(markup: str, alts: dict[str, str]) -> None:
    if not markup:
        return
    for img in lhtml.fragment_fromstring(markup, create_parent="div").iter("img"):
        src, alt = img.get("src"), (img.get("alt") or "").strip()
        if src and alt:
            alts.setdefault(src, alt)


def _images(article: Article, include_comments: bool) -> list[dict[str, str]]:
    """Return the document images with their ``alt`` captions.

    Body, comments, avatars and company logo.
    """
    alts: dict[str, str] = {}
    _image_alts(article.body_html, alts)
    if include_comments:
        for comment in article.iter_comments():
            _image_alts(comment.html, alts)
    urls = collect_image_urls(article, include_comments=include_comments)
    return [{"url": url, **({"alt": alts[url]} if url in alts else {})} for url in urls]


def _iter_with_parent(
    comments: list[Comment], parent: Comment | None = None
) -> Iterator[tuple[Comment, str | None]]:
    for comment in comments:
        yield comment, parent.id if parent else None
        yield from _iter_with_parent(comment.children, comment)


def _comment(comment: Comment, parent_id: str | None) -> dict[str, Any]:
    author = comment.author
    return {
        "id": comment.id,
        "parent_id": parent_id,
        "level": comment.level,
        "author": {
            "alias": author.alias,
            "fullname": author.fullname,
            "avatar_url": author.avatar_url,
        }
        if author
        else None,
        "published": _date(comment.published),
        "edited": _date(comment.edited),
        "score": comment.score,
        "votes": comment.votes,
        "is_post_author": comment.is_post_author,
        "deleted": comment.deleted,
        "html": comment.html,
        "text": html_to_text(comment.html),
    }


def build_document(
    article: Article, options: ExportOptions, *, exported_at: datetime | None = None
) -> dict[str, Any]:
    """Build the document dictionary following the ``habr-exporter/article`` schema."""
    exported_at = exported_at or datetime.now(UTC).replace(microsecond=0)
    body = {
        "id": article.id,
        "url": article.url,
        "title": article.title,
        "lang": article.lang,
        "post_type": article.post_type,
        "published": _date(article.published),
        "reading_time": article.reading_time,
        "complexity": _COMPLEXITY_CODES.get(article.complexity or "", article.complexity),
        "is_translation": article.is_translation,
        "original_author": article.original_author,
        "original_url": article.original_url,
        "hubs": [
            {
                "alias": hub.alias,
                "title": hub.title,
                "url": hub.url,
                "is_corporative": hub.is_corporative,
                "is_profiled": hub.is_profiled,
            }
            for hub in article.hubs
        ],
        "tags": list(article.tags),
        "statistics": asdict(article.statistics),
        "author": _user(article.author),
        "company": _company(article.company),
        "body_html": article.body_html,
        "body_text": html_to_text(article.body_html),
        "images": _images(article, options.include_comments),
        "comments": [_comment(c, parent) for c, parent in _iter_with_parent(article.comments)]
        if options.include_comments
        else None,
    }
    return {
        "schema": SCHEMA,
        "version": SCHEMA_VERSION,
        "exported_at": exported_at.isoformat(),
        "source_url": article.url,
        "article": body,
    }


class JsonExporter:
    extension = ".json"
    title = "JSON"

    def export(
        self, article: Article, path: Path, options: ExportOptions, images: ImageStore | None
    ) -> Path:
        """Save the article as JSON (UTF-8, indented). ``images`` is not used."""
        document = build_document(article, options)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(document, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return path
