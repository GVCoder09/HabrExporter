"""EPUB 3 export: an e-book for e-readers, phones and tablets.

The book is built from the same HTML blocks as HTML and PDF (:mod:`..render_html`), but:

* the markup is converted to strict XHTML: obsolete attributes and tags are removed, spoilers
  (``<details>``, which readers display inconsistently) are expanded into plain blocks;
* images are stored inside the archive as separate files. WebP, BMP and TIFF are converted to
  PNG because not every reader supports them. In :attr:`ImageMode.LINK` mode and for images that
  failed to download, a link to the original is shown, since EPUB does not allow remote images;
* the book has its own styles (``assets/epub.css``): the reader picks the font and column width;
* the article and the comments are two chapters. The table of contents (``nav.xhtml`` and
  ``toc.ncx`` for EPUB 2 readers) lists the article sections, the author block and the comments.
"""

from __future__ import annotations

import base64
import binascii
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from html import escape
from importlib import resources
from pathlib import Path

from lxml import etree
from lxml import html as lhtml

from ..formatting import format_datetime
from ..images import ImageData, ImageMode, ImageStore, sniff_mime, to_png
from ..models import Article
from ..render_html import (
    prepare_fragment,
    render_comments,
    render_company,
    render_footer,
    render_header,
    render_user_card,
)
from .base import ExportOptions, section_headings

#: Image formats EPUB readers display without conversion.
EPUB_NATIVE = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/svg+xml": "svg"}

_XHTML_NS = "http://www.w3.org/1999/xhtml"
_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_ALLOWED_ATTRS = {
    "class",
    "id",
    "style",
    "title",
    "lang",
    "dir",
    "href",
    "src",
    "alt",
    "width",
    "height",
    "colspan",
    "rowspan",
    "start",
    "reversed",
    "type",
    "datetime",
    "scope",
}
_RENAME_TAGS = {
    "center": "div",
    "font": "span",
    "big": "span",
    "small": "span",
    "tt": "code",
    "strike": "s",
}
_DROP_TAGS = {"script", "style", "iframe", "object", "embed", "form", "input", "button", "noscript"}


@lru_cache(maxsize=1)
def stylesheet() -> str:
    return resources.files("habr_exporter").joinpath("assets/epub.css").read_text(encoding="utf-8")


@dataclass(slots=True)
class _Resource:
    href: str
    media_type: str
    data: bytes


@dataclass(slots=True)
class _NavPoint:
    title: str
    href: str
    children: list[_NavPoint] = field(default_factory=list)


class _Images:
    """Book images: source URL -> ``images/imgNNN.ext`` file inside the archive."""

    def __init__(self, mode: ImageMode, store: ImageStore | None) -> None:
        self.mode = mode
        self.store = store
        self.resources: list[_Resource] = []
        self._by_key: dict[str, str | None] = {}

    def _add(self, key: str, image: ImageData) -> str | None:
        if key in self._by_key:
            return self._by_key[key]
        data, mime = image.data, image.mime
        if mime not in EPUB_NATIVE:
            raster = to_png(image)
            data, mime = (raster.data, "image/png") if raster is not None else (b"", "")
        href = None
        if data:
            href = f"images/img{len(self.resources) + 1:03d}.{EPUB_NATIVE[mime]}"
            self.resources.append(_Resource(href, mime, data))
        self._by_key[key] = href
        return href

    def src(self, url: str) -> str | None:
        """Return the ``image_src`` for :mod:`render_html`.

        A path inside the book, or ``None`` for a link.
        """
        if self.mode is not ImageMode.EMBED or self.store is None:
            return None
        image = self.store.get(url)
        return self._add(url, image) if image is not None else None

    def data_uri(self, uri: str) -> str | None:
        """Store an inline ``data:`` image (avatar placeholders etc.) as a file too."""
        header, _, payload = uri.partition(",")
        try:
            data = base64.b64decode(payload) if header.endswith(";base64") else payload.encode()
        except (binascii.Error, ValueError):
            return None
        mime = sniff_mime(data, header[5:].split(";")[0])
        return self._add(uri, ImageData(url=uri[:64], data=data, mime=mime))


# --- XHTML ---------------------------------------------------------------------------


def _sanitize(root: etree._Element, images: _Images) -> None:
    """Bring an HTML fragment to a form the EPUB validator accepts and readers display."""
    seen_ids: set[str] = set()
    for element in list(root.iter()):
        if not isinstance(element.tag, str):  # HTML comments and processing instructions
            element.drop_tree()
            continue
        tag = element.tag.lower()
        if tag in _DROP_TAGS:
            element.drop_tree()
            continue
        if tag == "details":
            element.tag = "div"
            element.set("class", "spoiler")
        elif tag == "summary":
            element.tag = "p"
            element.set("class", "spoiler-title")
            element.text = "▸ " + (element.text or "")
        elif tag in _RENAME_TAGS:
            element.tag = _RENAME_TAGS[tag]

        if element.get("align") in {"center", "right", "left", "justify"}:
            style = (element.get("style") or "").rstrip("; ")
            element.set("style", f"{style}; text-align:{element.get('align')}".lstrip("; "))
        if tag == "a" and element.get("name") and not element.get("id"):
            element.set("id", element.get("name"))
        for name in list(element.attrib):
            value = element.get(name) or ""
            keep = name in _ALLOWED_ATTRS or name.startswith("data-")
            if name in {"width", "height"} and not value.isdigit():
                keep = False
            if name == "id" and (
                not value or value in seen_ids or not re.match(r"^[A-Za-z_][\w.-]*$", value)
            ):
                keep = False
            if not keep:
                del element.attrib[name]
        if element.get("id"):
            seen_ids.add(element.get("id"))

        if tag == "img":
            src = element.get("src") or ""
            if src.startswith("data:"):
                packed = images.data_uri(src)
                if packed is None:
                    element.drop_tree()
                    continue
                element.set("src", packed)
            if element.get("alt") is None:
                element.set("alt", "")


def _xhtml_root(markup: str, images: _Images) -> etree._Element:
    root = lhtml.fragment_fromstring(_INVALID_XML_CHARS.sub("", markup), create_parent="div")
    _sanitize(root, images)
    return root


def _serialize(root: etree._Element) -> str:
    parts = [escape(root.text, quote=False)] if root.text else []
    parts.extend(etree.tostring(child, method="xml", encoding="unicode") for child in root)
    return "".join(parts)


def _to_xhtml(markup: str, images: _Images) -> str:
    return _serialize(_xhtml_root(markup, images))


def _document(title: str, lang: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        f'<html xmlns="{_XHTML_NS}" xmlns:epub="http://www.idpf.org/2007/ops" '
        f'lang="{escape(lang)}" '
        f'xml:lang="{escape(lang)}">\n<head>\n<meta charset="utf-8"/>\n'
        f"<title>{escape(title)}</title>\n"
        '<link rel="stylesheet" type="text/css" href="style.css"/>\n</head>\n'
        f"<body>\n{body}\n</body>\n</html>\n"
    )


# --- Book assembly -------------------------------------------------------------------


class EpubBuilder:
    """Collects the book files; :meth:`write` packs them into an ``.epub``."""

    def __init__(self, options: ExportOptions, images: ImageStore | None) -> None:
        self.options = options
        self.images = _Images(options.image_mode, images)
        self.chapters: list[tuple[str, str]] = []  # (file, XHTML)
        self.toc: list[_NavPoint] = []

    def build(self, article: Article) -> None:
        opts, src = self.options, self.images.src
        body = prepare_fragment(article.body_html, src, expand_spoilers=True)
        body_root = _xhtml_root(f'<div class="article-body">{body}</div>', self.images)
        sections = []  # article sections for the table of contents: (id, title)
        for number, heading in enumerate(
            body_root.iter(*section_headings(body_root) or ("h2",)), 1
        ):
            if not heading.get("id"):
                heading.set("id", f"section-{number}")
            sections.append((heading.get("id"), " ".join(heading.text_content().split())))
        body_xhtml = _serialize(body_root)

        cards = [render_company(article.company, src)] if article.company else []
        cards.append(render_user_card(article.author, src, opts.tz))
        saved = format_datetime(datetime.now().astimezone(), opts.tz)
        head = _to_xhtml(render_header(article, src, opts.tz), self.images)
        foot = _to_xhtml(render_footer(article), self.images)
        authors = _to_xhtml(
            f'<section class="authors" id="authors"><h2>Об авторе</h2>{"".join(cards)}</section>',
            self.images,
        )
        article_body = (
            f'<article id="article">\n{head}\n{body_xhtml}\n{foot}\n</article>\n{authors}\n'
            f'<p class="saved-from">Сохранено с <a href="{escape(article.url)}">'
            f"{escape(article.url)}</a> · "
            f"{escape(saved)}</p>"
        )
        self.chapters.append(
            ("article.xhtml", _document(article.title, article.lang, article_body))
        )
        article_point = _NavPoint(article.title, "article.xhtml")
        article_point.children = [
            _NavPoint(t or "Раздел", f"article.xhtml#{i}") for i, t in sections
        ]
        self.toc = [article_point, _NavPoint("Об авторе", "article.xhtml#authors")]

        if opts.include_comments:
            comments = _to_xhtml(
                f'<section class="comments" id="comments">'
                f"{render_comments(article, src, opts.tz)}</section>",
                self.images,
            )
            title = f"Комментарии — {article.title}"
            self.chapters.append(("comments.xhtml", _document(title, article.lang, comments)))
            self.toc.append(_NavPoint(f"Комментарии ({article.comments_total})", "comments.xhtml"))

    # --- Service files -------------------------------------------------------------

    def _opf(self, article: Article, modified: str) -> str:
        meta = [
            f'<dc:identifier id="bookid">{escape(_identifier(article))}</dc:identifier>',
            f"<dc:title>{escape(article.title)}</dc:title>",
            f"<dc:language>{escape(article.lang)}</dc:language>",
            f"<dc:creator>{escape(article.author.fullname or article.author.alias)}</dc:creator>",
            "<dc:publisher>Хабр</dc:publisher>",
            f"<dc:source>{escape(article.url)}</dc:source>",
            f'<meta property="dcterms:modified">{modified}</meta>',
        ]
        if article.published:
            meta.append(
                f"<dc:date>{article.published.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}</dc:date>"
            )
        if article.hubs:
            meta.append(
                f"<dc:description>Хабы: "
                f"{escape(', '.join(h.title for h in article.hubs))}</dc:description>"
            )
        meta.extend(f"<dc:subject>{escape(tag)}</dc:subject>" for tag in article.tags)

        items = [
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
            '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
            '<item id="css" href="style.css" media-type="text/css"/>',
        ]
        spine = []
        for name, _ in self.chapters:
            item_id = name.split(".")[0]
            items.append(f'<item id="{item_id}" href="{name}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="{item_id}"/>')
        for number, res in enumerate(self.images.resources, 1):
            items.append(
                f'<item id="image{number}" href="{res.href}" media-type="{res.media_type}"/>'
            )
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid" '
            f'xml:lang="{escape(article.lang)}">\n'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
            + "\n".join(meta)
            + "\n</metadata>\n"
            "<manifest>\n" + "\n".join(items) + "\n</manifest>\n"
            '<spine toc="ncx">\n' + "\n".join(spine) + "\n</spine>\n</package>\n"
        )

    def _nav(self, article: Article) -> str:
        def items(points: list[_NavPoint]) -> str:
            out = []
            for point in points:
                sub = f"<ol>{items(point.children)}</ol>" if point.children else ""
                out.append(
                    f'<li><a href="{escape(point.href)}">{escape(point.title)}</a>{sub}</li>'
                )
            return "".join(out)

        body = f'<nav epub:type="toc" id="toc"><h1>Оглавление</h1><ol>{items(self.toc)}</ol></nav>'
        return _document(article.title, article.lang, body)

    def _ncx(self, article: Article) -> str:
        order = [0]

        def points(nodes: list[_NavPoint]) -> str:
            out = []
            for node in nodes:
                order[0] += 1
                out.append(
                    f'<navPoint id="nav{order[0]}" playOrder="{order[0]}">'
                    f"<navLabel><text>{escape(node.title)}</text>"
                    f'</navLabel><content src="{escape(node.href)}"/>'
                    f"{points(node.children)}</navPoint>"
                )
            return "".join(out)

        nav_map = points(self.toc)
        return (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
            f'<head><meta name="dtb:uid" content="{escape(_identifier(article))}"/>'
            '<meta name="dtb:depth" content="2"/></head>\n'
            f"<docTitle><text>{escape(article.title)}</text></docTitle>\n"
            f"<navMap>{nav_map}</navMap>\n</ncx>\n"
        )

    def write(self, article: Article, path: Path) -> Path:
        modified = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        container = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            "<rootfiles>"
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>\n"
        )
        with zipfile.ZipFile(path, "w") as book:
            # mimetype goes first and uncompressed: readers detect the format by it.
            book.writestr(
                zipfile.ZipInfo("mimetype"),
                "application/epub+zip",
                compress_type=zipfile.ZIP_STORED,
            )
            deflated = {"compress_type": zipfile.ZIP_DEFLATED}
            book.writestr("META-INF/container.xml", container, **deflated)
            book.writestr("OEBPS/content.opf", self._opf(article, modified), **deflated)
            book.writestr("OEBPS/nav.xhtml", self._nav(article), **deflated)
            book.writestr("OEBPS/toc.ncx", self._ncx(article), **deflated)
            book.writestr("OEBPS/style.css", stylesheet(), **deflated)
            for name, xhtml in self.chapters:
                book.writestr(f"OEBPS/{name}", xhtml, **deflated)
            for res in self.images.resources:
                book.writestr(f"OEBPS/{res.href}", res.data, **deflated)
        return path


def _identifier(article: Article) -> str:
    return f"urn:habr:article:{article.id}"


class EpubExporter:
    """Exporter to ``.epub``."""

    extension = ".epub"
    title = "EPUB"

    def export(
        self, article: Article, path: Path, options: ExportOptions, images: ImageStore | None
    ) -> Path:
        builder = EpubBuilder(options, images)
        builder.build(article)
        return builder.write(article, Path(path))
