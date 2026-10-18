"""FictionBook 2 (``.fb2``) export, an e-book format popular with Russian-language readers.

FB2 is XML with a limited set of elements, so the article HTML is converted by a dedicated
walker (as for DOCX) rather than printed:

* paragraphs, bold, italic, strikethrough, sub/superscript, monospace and links are kept as is;
* the top-level headings of the article (usually ``h2``, ``h3`` for some authors) split it into
  sections (``<section>`` with a ``<title>``), so readers show a table of contents; smaller
  headings become ``<subtitle>``;
* FB2 has no lists, so items are output as paragraphs with a "•" bullet or a number;
* FB2 has no code blocks either, so each line becomes a ``<code>`` paragraph with indentation kept
  as non-breaking spaces; there is no syntax highlighting;
* quotes and spoilers become ``<cite>``, tables become ``<table>``;
* images are embedded at the end of the file (``<binary>``) as PNG or JPEG: GIF, WebP and others
  are converted to PNG, SVG formulas are rendered via Qt. Avatars and the logo are omitted
  because readers stretch them to the full screen width;
* comment replies are an indented ``<cite>`` with "↳" arrows for the thread depth and a note of
  whom they reply to, since FB2 does not allow nested quotes.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from lxml import etree
from lxml import html as lhtml

from ..formatting import (
    format_count,
    format_date,
    format_datetime,
    format_number,
    format_score,
    plural,
)
from ..images import ImageMode, ImageStore, raster_for_docx, to_png
from ..models import Article, Comment, Company, User
from .base import ExportOptions, section_headings

FB_NS = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK_NS = "http://www.w3.org/1999/xlink"
HREF = f"{{{XLINK_NS}}}href"

#: Image formats every FB2 reader understands.
FB2_NATIVE = {"image/png": "png", "image/jpeg": "jpg"}
#: Maximum number of "↳" arrows before a reply; deeper levels are not shown.
MAX_REPLY_ARROWS = 6
#: Length of the annotation (start of the article) in the book description, in characters.
ANNOTATION_LENGTH = 400

_WS_RE = re.compile(r"\s+")
_INVALID_XML_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")
_NBSP = " "
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
    "pre",
    "ul",
    "ol",
    "li",
    "dl",
    "dt",
    "dd",
    "table",
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
_INLINE_STYLES = {
    "strong": "strong",
    "b": "strong",
    "em": "emphasis",
    "i": "emphasis",
    "cite": "emphasis",
    "dfn": "emphasis",
    "s": "strikethrough",
    "strike": "strikethrough",
    "del": "strikethrough",
    "sub": "sub",
    "sup": "sup",
    "code": "code",
    "kbd": "code",
    "samp": "code",
    "tt": "code",
}


def _fb(tag: str) -> str:
    return f"{{{FB_NS}}}{tag}"


def _clean(text: str) -> str:
    return _INVALID_XML_CHARS.sub("", text)


def _append_text(element: etree._Element, text: str) -> None:
    """Append text to the end of an element (after the last child, into its ``tail``)."""
    text = _clean(text)
    if not text:
        return
    if len(element):
        last = element[-1]
        last.tail = (last.tail or "") + text
    else:
        element.text = (element.text or "") + text


def _sub(parent: etree._Element, tag: str, text: str | None = None, **attrs: str) -> etree._Element:
    element = etree.SubElement(parent, _fb(tag), attrs)
    if text:
        element.text = _clean(text)
    return element


def _trim(paragraph: etree._Element) -> None:
    """Strip whitespace at the start and end of a paragraph.

    It is left over by HTML whitespace normalization.
    """
    if paragraph.text:
        paragraph.text = paragraph.text.lstrip(" ")
    last = paragraph
    while len(last):
        last = last[-1]
    if last is paragraph:
        paragraph.text = (paragraph.text or "").rstrip(" ")
    elif last.tail:
        last.tail = last.tail.rstrip(" ")
    elif last.text and last.getparent() is paragraph:
        last.text = last.text.rstrip(" ")


def _is_empty(paragraph: etree._Element) -> bool:
    return (
        not "".join(paragraph.itertext()).strip() and paragraph.find(f".//{_fb('image')}") is None
    )


@dataclass(frozen=True, slots=True)
class Ctx:
    """Where blocks are output: ``<cite>`` allows neither a nested quote nor a block image."""

    in_cite: bool = False
    list_depth: int = 0


class _Target:
    """Current block container.

    In the article body, ``section_tags`` headings open a new section.
    """

    def __init__(
        self,
        container: etree._Element,
        new_section: Callable[[str], etree._Element] | None = None,
        section_tags: tuple[str, ...] = (),
    ) -> None:
        self.container = container
        self.new_section = new_section
        self.section_tags = section_tags


class Fb2Builder:
    """Builds a FictionBook document from an article."""

    def __init__(self, options: ExportOptions, images: ImageStore | None) -> None:
        self.options = options
        self.store = images
        self.binaries: list[tuple[str, str, bytes]] = []  # (id, MIME, data)
        self._image_ids: dict[str, str | None] = {}
        self.root = etree.Element(_fb("FictionBook"), nsmap={None: FB_NS, "l": XLINK_NS})

    # --- Images ----------------------------------------------------------------

    def image_id(self, url: str) -> str | None:
        """Return the ``<binary>`` id for an image, or ``None`` if it cannot be embedded."""
        if url in self._image_ids:
            return self._image_ids[url]
        image_id = None
        image = (
            self.store.get(url)
            if self.store is not None and self.options.image_mode is ImageMode.EMBED
            else None
        )
        if image is not None:
            if image.mime in FB2_NATIVE:
                data, mime = image.data, image.mime
            else:
                raster = raster_for_docx(image) if image.mime == "image/svg+xml" else to_png(image)
                data, mime = (raster.data, "image/png") if raster is not None else (b"", "")
            if data:
                image_id = f"img{len(self.binaries) + 1}.{FB2_NATIVE[mime]}"
                self.binaries.append((image_id, mime, data))
        self._image_ids[url] = image_id
        return image_id

    def _image_element(
        self, parent: etree._Element, img: etree._Element, *, in_link: bool = False
    ) -> None:
        """Return an image inside a paragraph: embedded, or a link to the original.

        Inside a link it is plain text.
        """
        url = img.get("src") or ""
        alt = (img.get("alt") or "").strip()
        image_id = self.image_id(url) if url and not url.startswith("data:") else None
        if image_id:
            attrs = {HREF: f"#{image_id}"}
            if alt:
                attrs["alt"] = _clean(alt)
            etree.SubElement(parent, _fb("image"), attrs)
        elif url and not url.startswith("data:"):
            caption = f"🖼 {alt or 'Изображение'}: {url}"
            if in_link:
                _append_text(parent, caption)
            else:
                etree.SubElement(parent, _fb("a"), {HREF: url}).text = _clean(caption)

    # --- HTML traversal ----------------------------------------------------------

    def html(self, target: _Target, markup: str, ctx: Ctx | None = None) -> None:
        if not markup or not markup.strip():
            return
        root = lhtml.fragment_fromstring(markup, create_parent="div")
        self.blocks(target, root, ctx or Ctx())

    def blocks(self, target: _Target, element: etree._Element, ctx: Ctx) -> None:
        """Process mixed block content.

        Text and inline elements fill a paragraph, a block closes it.
        """
        current: list[etree._Element | None] = [None]

        def para() -> etree._Element:
            if current[0] is None:
                current[0] = _sub(target.container, "p")
            return current[0]

        def close() -> None:
            if current[0] is not None:
                _trim(current[0])
                if _is_empty(current[0]):
                    target.container.remove(current[0])
            current[0] = None

        if element.text and element.text.strip():
            self.inline_text(para(), element.text)
        for child in element:
            if not isinstance(child.tag, str):
                pass
            elif (tag := child.tag.lower()) in _BLOCK_TAGS or (
                tag == "img" and _is_block_image(child)
            ):
                close()
                self.block(target, child, ctx)
            elif tag == "br":
                close()
            else:
                self.inline(para(), child)
            if child.tail and (child.tail.strip() or current[0] is not None):
                self.inline_text(para(), child.tail)
        close()

    def inline_text(self, parent: etree._Element, text: str) -> None:
        _append_text(parent, _WS_RE.sub(" ", text))

    def inline(
        self, parent: etree._Element, element: etree._Element, *, in_link: bool = False
    ) -> None:
        """Process inline content.

        Styles, links and images in the current paragraph (or table cell).
        """
        tag = element.tag.lower() if isinstance(element.tag, str) else ""
        if tag == "img":
            self._image_element(parent, element, in_link=in_link)
            return
        if tag == "br":
            _append_text(parent, " ")
            return
        target = parent
        href = element.get("href") or ""
        if tag == "a" and href and not href.startswith("#") and not in_link:
            target = etree.SubElement(parent, _fb("a"), {HREF: _clean(href)})
            in_link = True
        elif tag in _INLINE_STYLES:
            target = etree.SubElement(parent, _fb(_INLINE_STYLES[tag]))
        if element.text:
            self.inline_text(target, element.text)
        for child in element:
            if isinstance(child.tag, str):
                self.inline(target, child, in_link=in_link)
                if child.tag.lower() in _BLOCK_TAGS:
                    _append_text(target, " ")
            if child.tail:
                self.inline_text(target, child.tail)
        if target is not parent and not len(target) and not (target.text or "").strip():
            parent.remove(target)

    def _paragraph(
        self, target: _Target, element: etree._Element, wrap: str | None = None
    ) -> etree._Element:
        p = _sub(target.container, "p")
        inner = _sub(p, wrap) if wrap else p
        self.inline(inner, _as_span(element))
        _trim(p)
        if _is_empty(p):
            target.container.remove(p)
        return p

    def block(self, target: _Target, element: etree._Element, ctx: Ctx) -> None:
        tag = element.tag.lower()
        if tag in target.section_tags and target.new_section is not None and not ctx.in_cite:
            title = _WS_RE.sub(" ", element.text_content()).strip()
            target.container = target.new_section(title)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            subtitle = _sub(target.container, "subtitle")
            self.inline(subtitle, _as_span(element))
            _trim(subtitle)
            if _is_empty(subtitle):
                target.container.remove(subtitle)
        elif tag == "p":
            self.blocks(target, element, ctx)
        elif tag == "pre":
            self.code_block(target, element)
        elif tag in {"blockquote", "details"} or "spoiler" in (element.get("class") or "").split():
            self.quote(target, element, ctx, spoiler=tag != "blockquote")
        elif tag in {"ul", "ol"}:
            self.list_block(target, element, ctx, ordered=tag == "ol")
        elif tag == "li":
            self.list_block(target, element, ctx, ordered=False, single=True)
        elif tag == "img":
            self.block_image(target, element, ctx)
        elif tag == "figcaption":
            self._paragraph(target, element, wrap="emphasis")
        elif tag == "hr":
            _sub(target.container, "empty-line")
        elif tag == "table":
            self.table(target, element)
        elif tag in {"summary", "dt"} or "spoiler_title" in (element.get("class") or ""):
            self._paragraph(target, element, wrap="strong")
        else:  # div, figure, section, dl, dd and other containers
            self.blocks(target, element, ctx)

    def block_image(self, target: _Target, img: etree._Element, ctx: Ctx) -> None:
        if ctx.in_cite:  # <cite> has no block images, so put the image in its own paragraph
            self._image_element(_sub(target.container, "p"), img)
            return
        url = img.get("src") or ""
        image_id = self.image_id(url) if url and not url.startswith("data:") else None
        if image_id:
            attrs = {HREF: f"#{image_id}"}
            if (img.get("alt") or "").strip():
                attrs["alt"] = _clean(img.get("alt").strip())
            etree.SubElement(target.container, _fb("image"), attrs)
        elif url and not url.startswith("data:"):
            self._image_element(_sub(target.container, "p"), img)

    def code_block(self, target: _Target, element: etree._Element) -> None:
        source = element.text_content().strip("\n").expandtabs(4)
        for line in source.split("\n"):
            if not line.strip():
                _sub(target.container, "empty-line")
                continue
            stripped = line.lstrip(" ")
            text = _NBSP * (len(line) - len(stripped)) + stripped.rstrip()
            _sub(_sub(target.container, "p"), "code", text)

    def quote(self, target: _Target, element: etree._Element, ctx: Ctx, *, spoiler: bool) -> None:
        body = element
        if spoiler:
            summary = element.find("summary")
            if summary is None:
                summary = next(iter(element.xpath("./*[contains(@class, 'spoiler_title')]")), None)
            title = _WS_RE.sub(" ", summary.text_content()).strip() if summary is not None else ""
            _sub(_sub(target.container, "p"), "strong", "▸ " + (title or "Спойлер"))
            body = lhtml.Element("div")
            body.text = element.text if summary is None else summary.tail
            for child in element:
                if child is not summary:
                    body.append(_clone(child))
        if ctx.in_cite:
            # FB2 does not allow nested <cite>: a quote inside a reply is set in italics.
            before = len(target.container)
            self.blocks(target, body, ctx)
            for paragraph in target.container[before:]:
                if paragraph.tag == _fb("p"):
                    _emphasize(paragraph)
            return
        cite = _sub(target.container, "cite")
        self.blocks(_Target(cite), body, replace(ctx, in_cite=True))
        if not len(cite):
            target.container.remove(cite)

    def list_block(
        self,
        target: _Target,
        element: etree._Element,
        ctx: Ctx,
        *,
        ordered: bool,
        single: bool = False,
    ) -> None:
        items = (
            [element]
            if single
            else [c for c in element if isinstance(c.tag, str) and c.tag.lower() == "li"]
        )
        start = element.get("start") or "1"
        first_number = int(start) if ordered and start.isdigit() else 1
        indent = _NBSP * 3 * ctx.list_depth
        inner = replace(ctx, list_depth=ctx.list_depth + 1)
        for number, item in enumerate(items, start=first_number):
            marker = (
                f"{indent}{number}.{_NBSP}"
                if ordered
                else f"{indent}{('•', '◦', '▪')[ctx.list_depth % 3]}{_NBSP}"
            )
            before = len(target.container)
            self.blocks(target, item, inner)
            added = [c for c in target.container[before:] if c.tag == _fb("p")]
            if added:
                first = added[0]
                first.text = marker + (first.text or "")
            else:
                _sub(target.container, "p", marker)

    def table(self, target: _Target, element: etree._Element) -> None:
        rows = element.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
        if not rows:
            return
        table = _sub(target.container, "table")
        for row in rows:
            tr = _sub(table, "tr")
            for cell in row:
                if not isinstance(cell.tag, str) or cell.tag.lower() not in {"td", "th"}:
                    continue
                attrs = {
                    k: v
                    for k, v in cell.attrib.items()
                    if k in {"colspan", "rowspan"} and v.isdigit()
                }
                out = _sub(tr, cell.tag.lower(), **attrs)
                self.inline(out, _as_span(cell))
                _trim(out)
            if not len(tr):
                table.remove(tr)
        if not len(table):
            target.container.remove(table)

    # --- Document sections -------------------------------------------------------

    def description(self, article: Article) -> None:
        desc = _sub(self.root, "description")
        info = _sub(desc, "title-info")
        _sub(info, "genre", "comp_www")
        _author(info, article.author)
        _sub(info, "book-title", article.title)
        annotation = _plain_text(article.body_html)
        if len(annotation) > ANNOTATION_LENGTH:
            annotation = annotation[:ANNOTATION_LENGTH].rsplit(" ", 1)[0] + "…"
        if annotation:
            _sub(_sub(info, "annotation"), "p", annotation)
        if article.tags:
            _sub(info, "keywords", ", ".join(article.tags))
        if article.published:
            _sub(
                info,
                "date",
                format_date(article.published, self.options.tz),
                value=article.published.date().isoformat(),
            )
        _sub(info, "lang", article.lang or "ru")

        now = datetime.now().astimezone()
        doc = _sub(desc, "document-info")
        _sub(_sub(doc, "author"), "nickname", "Habr Exporter")
        _sub(doc, "program-used", "Habr Exporter")
        _sub(doc, "date", format_date(now), value=now.date().isoformat())
        _sub(doc, "src-url", article.url)
        _sub(doc, "id", f"habr-article-{article.id}")
        _sub(doc, "version", "1.0")

        publish = _sub(desc, "publish-info")
        _sub(publish, "publisher", "Хабр")
        if article.published:
            _sub(publish, "year", str(article.published.year))

    def body(self, article: Article) -> None:
        tz = self.options.tz
        body = _sub(self.root, "body")
        _sub(_sub(body, "title"), "p", article.title)

        def new_section(title: str = "") -> etree._Element:
            section = _sub(body, "section")
            if title:
                _sub(_sub(section, "title"), "p", title)
            return section

        first = new_section()
        meta = _sub(first, "p")
        _sub(meta, "strong", f"@{article.author.alias}")
        _append_text(
            meta, f" · {format_datetime(article.published, tz)}" if article.published else ""
        )
        facts = []
        if article.complexity:
            facts.append(f"Уровень сложности: {article.complexity}")
        if article.reading_time:
            facts.append(f"Время на прочтение: {article.reading_time} мин")
        if article.statistics.views:
            facts.append(f"Просмотры: {format_count(article.statistics.views)}")
        if facts:
            _sub(_sub(first, "p"), "emphasis", " · ".join(facts))
        if article.hubs:
            _sub(_sub(first, "p"), "emphasis", "Хабы: " + ", ".join(h.title for h in article.hubs))
        labels = [
            x
            for x, on in (
                ("Новость", article.post_type == "news"),
                ("Перевод", article.is_translation),
            )
            if on
        ]
        if labels:
            _sub(_sub(first, "p"), "strong", " · ".join(labels))
        if article.is_translation and (article.original_author or article.original_url):
            p = _sub(first, "p", f"Автор оригинала: {article.original_author or ''} ")
            if article.original_url:
                _sub(p, "a", "Оригинал", **{HREF: article.original_url})
        _sub(first, "empty-line")

        if article.body_html.strip():
            root = lhtml.fragment_fromstring(article.body_html, create_parent="div")
            target = _Target(first, new_section, section_headings(root))
            self.blocks(target, root, Ctx())
        else:
            target = _Target(first)
        last = target.container
        _sub(last, "empty-line")
        if article.tags:
            _sub(_sub(last, "p"), "emphasis", "Теги: " + ", ".join(article.tags))
        stats = article.statistics
        _sub(
            _sub(last, "p"),
            "emphasis",
            f"Рейтинг {format_score(stats.score)} · Просмотры {format_count(stats.views)} · "
            f"В закладках {stats.favorites} · Комментарии {stats.comments}",
        )
        saved = _sub(last, "p", "Сохранено с ")
        _sub(saved, "a", article.url, **{HREF: article.url})
        _append_text(saved, f" · {format_datetime(datetime.now().astimezone(), tz)}")

        self.authors_section(new_section("Об авторе"), article)
        if self.options.include_comments:
            self.comments_section(new_section(f"Комментарии ({article.comments_total})"), article)

    def authors_section(self, section: etree._Element, article: Article) -> None:
        user = article.author
        p = _sub(section, "p")
        _sub(p, "strong", user.fullname or user.alias)
        _append_text(p, f" @{user.alias}")
        if user.speciality:
            _sub(_sub(section, "p"), "emphasis", user.speciality)
        facts = [f"Карма {format_number(user.karma)}", f"Рейтинг {format_number(user.rating)}"]
        if user.followers is not None:
            facts.append(
                f"{user.followers} "
                f"{plural(user.followers, 'подписчик', 'подписчика', 'подписчиков')}"
            )
        if user.location:
            facts.append(user.location)
        if user.registered:
            facts.append("На Хабре с " + format_date(user.registered, self.options.tz))
        _sub(section, "p", " · ".join(facts))
        _sub(_sub(section, "p"), "a", user.url, **{HREF: user.url})
        if article.company:
            self.company(section, article.company)

    def company(self, section: etree._Element, company: Company) -> None:
        _sub(section, "empty-line")
        _sub(section, "subtitle", company.title)
        if company.description:
            _sub(section, "p", company.description)
        facts = []
        if company.rating is not None:
            facts.append(f"Рейтинг {format_number(company.rating)}")
        if company.subscribers is not None:
            facts.append(f"Подписчики {format_count(company.subscribers)}")
        facts.extend(x for x in (company.location, company.staff) if x)
        if facts:
            _sub(section, "p", " · ".join(facts))
        links = [(company.url, "Блог на Хабре")]
        if company.site_url:
            links.append((company.site_url, "Сайт"))
        links.extend((c.url, c.title) for c in company.contacts if c.url != company.site_url)
        p = _sub(section, "p")
        for index, (url, title) in enumerate(links):
            if index:
                _append_text(p, " · ")
            _sub(p, "a", title, **{HREF: url})

    def comments_section(self, section: etree._Element, article: Article) -> None:
        if not article.comments:
            _sub(_sub(section, "p"), "emphasis", "Комментариев пока нет")
            return
        for index, root in enumerate(article.comments):
            if index:
                _sub(section, "empty-line")
            self.comment(section, root, depth=0, parent=None)

    def comment(
        self, section: etree._Element, comment: Comment, *, depth: int, parent: User | None
    ) -> None:
        container = section if depth == 0 else _sub(section, "cite")
        head = _sub(container, "p")
        if depth:
            _append_text(head, "↳" * min(depth, MAX_REPLY_ARROWS) + " ")
        _sub(head, "strong", comment.author.alias if comment.author else "НЛО")
        details = []
        if comment.is_post_author:
            details.append("автор")
        if comment.published:
            details.append(format_datetime(comment.published, self.options.tz))
        details.append(f"рейтинг {format_score(comment.score)}")
        if depth and parent is not None:
            details.append(f"ответ @{parent.alias}")
        _append_text(head, " · " + " · ".join(details))
        if comment.deleted and not comment.html:
            _sub(_sub(container, "p"), "emphasis", "НЛО прилетело и опубликовало эту надпись здесь")
        else:
            self.html(_Target(container), comment.html, Ctx(in_cite=depth > 0))
        for child in comment.children:
            self.comment(section, child, depth=depth + 1, parent=comment.author)

    def build(self, article: Article) -> etree._Element:
        self.description(article)
        self.body(article)
        for image_id, mime, data in self.binaries:
            binary = _sub(self.root, "binary", id=image_id)
            binary.set("content-type", mime)
            binary.text = base64.b64encode(data).decode("ascii")
        for section in self.root.iter(_fb("section")):
            if not any(child.tag != _fb("title") for child in section):
                _sub(section, "empty-line")  # FB2 does not allow an empty section
        return self.root


def _author(parent: etree._Element, user: User) -> None:
    author = _sub(parent, "author")
    names = (user.fullname or "").split()
    if len(names) >= 2:
        _sub(author, "first-name", names[0])
        _sub(author, "last-name", " ".join(names[1:]))
    _sub(author, "nickname", user.alias)
    _sub(author, "home-page", user.url)


def _emphasize(paragraph: etree._Element) -> None:
    """Move all paragraph content into an ``<emphasis>``."""
    emphasis = etree.Element(_fb("emphasis"))
    emphasis.text, paragraph.text = paragraph.text, None
    for child in list(paragraph):
        emphasis.append(child)
    paragraph.append(emphasis)


def _plain_text(markup: str) -> str:
    """Return the text of the first paragraphs of the article, for the annotation."""
    if not markup or not markup.strip():
        return ""
    root = lhtml.fragment_fromstring(markup, create_parent="div")
    paragraphs = (_WS_RE.sub(" ", p.text_content()).strip() for p in root.iter("p"))
    return _clean(" ".join(p for p in paragraphs if p))


def _is_block_image(img: etree._Element) -> bool:
    parent = img.getparent()
    if parent is None:
        return True
    if parent.tag in {
        "figure",
        "div",
        "section",
        "article",
        "td",
        "th",
        "li",
        "blockquote",
        "details",
        "p",
    }:
        text = (parent.text or "") + "".join((c.tail or "") for c in parent)
        others = [
            c
            for c in parent
            if c is not img and isinstance(c.tag, str) and c.tag not in {"br", "figcaption"}
        ]
        return not text.strip() and not others
    return False


def _as_span(element: etree._Element) -> etree._Element:
    """Return a copy of the element without its own tag, so its content is output inline."""
    copy = _clone(element)
    copy.tag = "span"
    copy.attrib.clear()
    return copy


def _clone(element: etree._Element) -> etree._Element:
    copy = deepcopy(element)
    copy.tail = None
    return copy


class Fb2Exporter:
    """Exporter to ``.fb2``."""

    extension = ".fb2"
    title = "FB2"

    def export(
        self, article: Article, path: Path, options: ExportOptions, images: ImageStore | None
    ) -> Path:
        root = Fb2Builder(options, images).build(article)
        path = Path(path)
        path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="utf-8"))
        return path
