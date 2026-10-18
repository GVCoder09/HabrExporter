"""Markdown export for notes (Obsidian, Logseq), wikis, repositories and editors like Typora.

The markup is CommonMark with GitHub extensions (tables, strikethrough); what Markdown lacks stays
as HTML tags that almost every viewer understands: spoilers are ``<details>``, sub/superscript
is ``<sub>``/``<sup>``. Formulas are TeX between ``$...$`` (inline) and ``$$...$$`` (display), as
Obsidian, Typora, GitHub and pandoc read them.

The file starts with YAML front matter holding the article metadata. Comments follow the article,
replies are nested as quotes (no deeper than :data:`MAX_QUOTE_DEPTH`; deeper ones stay at the same
level, as on the site).

In :attr:`ImageMode.EMBED` mode images are embedded as ``data:`` URIs. To keep base64 from getting
in the way of reading, the text uses reference links ``![caption][img-1]`` and the data itself is
collected at the end of the file. Images that failed to download and :attr:`ImageMode.LINK` mode
produce ordinary links to the originals.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from datetime import tzinfo
from pathlib import Path

from lxml import etree
from lxml import html as lhtml

from ..formatting import format_count, format_datetime, format_score
from ..highlight import language_from_classes
from ..images import ImageMode, ImageStore
from ..models import Article, Comment
from .base import ExportOptions

#: Replies are not nested deeper than this: a dozen ``> > > ...`` is unreadable.
MAX_QUOTE_DEPTH = 5

_BREAK = "\x00"
"""Placeholder for an inline ``<br>``: becomes a hard line break ``\\`` in the final text."""

_WS_RE = re.compile(r"\s+")
_ESCAPE_RE = re.compile(r"([\\`*\[\]<>$~|])")
#: ``_`` inside a word (``snake_case``) is not emphasis, so it is escaped only at word boundaries.
_UNDERSCORE_RE = re.compile(r"(?<![0-9A-Za-zА-Яа-яЁё])_|_(?![0-9A-Za-zА-Яа-яЁё])")
_ENTITY_RE = re.compile(r"&(?=#?\w+;)")
#: Line starts that Markdown would read as a heading, quote, list or thematic break.
_LINE_START_RE = re.compile(r"^(?:#{1,6}(?=\s|$)|>|[-+](?=\s|$)|=+\s*$|-{3,}\s*$)")
_ORDERED_START_RE = re.compile(r"^(\d{1,9})([.)])(?=\s|$)")

_PARAGRAPH_TAGS = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "footer",
    "aside",
    "main",
    "center",
    "figure",
    "dl",
    "dd",
}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_BLOCK_TAGS = (
    _PARAGRAPH_TAGS
    | _HEADING_TAGS
    | {
        "ul",
        "ol",
        "blockquote",
        "pre",
        "table",
        "details",
        "hr",
        "figcaption",
        "dt",
    }
)


def _tag(element: etree._Element) -> str:
    return element.tag if isinstance(element.tag, str) else ""


def escape_text(text: str) -> str:
    """Escape characters that Markdown would take for markup."""
    text = _ESCAPE_RE.sub(r"\\\1", text)
    text = _UNDERSCORE_RE.sub(r"\\_", text)
    return _ENTITY_RE.sub("&amp;", text)


def _escape_line_start(line: str) -> str:
    if _LINE_START_RE.match(line):
        return "\\" + line
    return _ORDERED_START_RE.sub(r"\1\\\2", line, count=1)


def _destination(url: str) -> str:
    """Return a link destination.

    With spaces or brackets it goes in angle brackets, as CommonMark requires.
    """
    if re.search(r"[\s()<>]", url):
        return "<" + url.replace("<", "%3C").replace(">", "%3E") + ">"
    return url


def _code_span(code: str) -> str:
    code = code.replace("\n", " ")
    if not code:
        return ""
    fence = "`" * (max((len(run) for run in re.findall(r"`+", code)), default=0) + 1)
    pad = " " if code.startswith(("`", " ")) or code.endswith(("`", " ")) else ""
    return f"{fence}{pad}{code}{pad}{fence}"


def _wrap(marker: str, content: str) -> str:
    """Wrap text as ``**text**``, moving edge whitespace outside.

    Otherwise emphasis does not work.
    """
    core = content.strip(" ")
    if not core.replace(_BREAK, "").strip():
        return content
    lead = content[: len(content) - len(content.lstrip(" "))]
    trail = content[len(content.rstrip(" ")) :]
    return f"{lead}{marker}{core}{marker}{trail}"


def _finish_paragraph(text: str) -> str:
    """Collapse whitespace at line edges and turn ``<br>`` markers into hard line breaks."""
    lines = [line.strip() for line in text.split(_BREAK)]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return "\\\n".join(_escape_line_start(line) for line in lines if line)


def _indent(text: str, prefix: str, first: str | None = None) -> str:
    lines = text.split("\n")
    head = (first if first is not None else prefix) + lines[0]
    return "\n".join([head, *((prefix + line).rstrip() if line else "" for line in lines[1:])])


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


class _ListBlock(str):
    """List block: inside a list item it is written right below the text, without a blank line."""

    __slots__ = ()


ImageRef = Callable[[str, str], str]
"""``(url, caption) -> image markup``: a reference to a ``data:`` URI or a link to the original."""


class MarkdownConverter:
    """Converts the sanitized HTML of an article or comment to Markdown."""

    def __init__(self, image_ref: ImageRef | None = None) -> None:
        self.image_ref = image_ref or (
            lambda url, alt: f"![{escape_text(alt)}]({_destination(url)})"
        )

    def convert(self, markup: str) -> str:
        if not markup or not markup.strip():
            return ""
        root = lhtml.fragment_fromstring(markup, create_parent="div")
        return "\n\n".join(self.blocks(root))

    # --- Blocks ------------------------------------------------------------------

    def blocks(self, element: etree._Element) -> list[str]:
        """Return the element content as paragraphs.

        Text between blocks forms separate paragraphs.
        """
        out: list[str] = []
        run: list[str] = [self.text(element.text)]

        def flush() -> None:
            paragraph = _finish_paragraph("".join(run))
            run.clear()
            if paragraph:
                out.append(paragraph)

        for child in element:
            if _tag(child) in _BLOCK_TAGS:
                flush()
                out.extend(block for block in self.block(child) if block)
            else:
                run.append(self.inline(child))
            run.append(self.text(child.tail))
        flush()
        return out

    def block(self, element: etree._Element) -> list[str]:
        tag = _tag(element)
        if tag in _HEADING_TAGS:
            # The article title is "#", so sections start with "##".
            text = " ".join(_finish_paragraph(self.children(element)).replace("\\\n", " ").split())
            return [f"{'#' * max(int(tag[1]), 2)} {text}"] if text else []
        if tag in ("ul", "ol"):
            return [self.list(element)]
        if tag == "blockquote":
            inner = "\n\n".join(self.blocks(element))
            return [_quote(inner)] if inner else []
        if tag == "pre":
            return [self.code_block(element)]
        if tag == "table":
            return [self.table(element)]
        if tag == "details":
            return [self.details(element)]
        if tag == "hr":
            return ["---"]
        if tag in ("figcaption", "dt"):
            text = _finish_paragraph(_wrap("**" if tag == "dt" else "*", self.children(element)))
            return [text] if text else []
        return self.blocks(element)

    def list(self, element: etree._Element) -> str:
        ordered = _tag(element) == "ol"
        try:
            number = int(element.get("start") or 1)
        except ValueError:
            number = 1
        items: list[str] = []
        for child in element:
            if _tag(child) != "li":
                # A list directly inside a list (happens in old articles) continues the item.
                parts = (
                    self.block(child)
                    if _tag(child) in _BLOCK_TAGS
                    else [_finish_paragraph(self.inline(child))]
                )
                text = "\n\n".join(p for p in parts if p)
                if text:
                    if items:
                        items[-1] += "\n" + _indent(text, "   " if ordered else "  ")
                    else:
                        items.append(text)
                continue
            marker = f"{number}." if ordered else "-"
            number += 1
            parts = self.blocks(child)
            body = parts[0] if parts else ""
            for part in parts[1:]:
                body += ("\n" if isinstance(part, _ListBlock) else "\n\n") + part
            items.append(_indent(body, " " * (len(marker) + 1), first=marker + " ").rstrip())
        loose = any("\n\n" in item for item in items)
        return _ListBlock(("\n\n" if loose else "\n").join(items))

    def code_block(self, element: etree._Element) -> str:
        code = element.find("code")
        source = (code if code is not None else element).text_content()
        source = "\n".join(line.rstrip() for line in source.strip("\n").split("\n"))
        language = language_from_classes(code.get("class")) if code is not None else None
        fence = "`" * max(3, max((len(run) for run in re.findall(r"`{3,}", source)), default=0) + 1)
        return f"{fence}{language or ''}\n{source}\n{fence}"

    def table(self, element: etree._Element) -> str:
        rows: list[list[str]] = []
        for tr in element.iter("tr"):
            if next(tr.iterancestors("table"), None) is not element:
                continue  # a row of a nested table, already included as text in the cell
            cells: list[str] = []
            for cell in tr:
                if _tag(cell) not in ("td", "th"):
                    continue
                text = "<br>".join(self.blocks(cell)).replace("\\\n", "<br>").replace("\n", " ")
                cells.append(re.sub(r"(?<!\\)\|", r"\\|", text))
                try:
                    span = int(cell.get("colspan") or 1)
                except ValueError:
                    span = 1
                cells.extend([""] * (min(span, 50) - 1))
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(row) + " |" for row in rows]
        lines.insert(1, "|" + "|".join([" --- "] * width) + "|")
        return "\n".join(lines)

    def details(self, element: etree._Element) -> str:
        summary = element.find("summary")
        title = " ".join(summary.text_content().split()) if summary is not None else ""
        if summary is not None:
            tail = summary.tail
            summary.tail = None
            element.remove(summary)
            element.text = (element.text or "") + (tail or "")
        body = "\n\n".join(self.blocks(element))
        parts = ["<details>", f"<summary>{html.escape(title or 'Спойлер', quote=False)}</summary>"]
        if body:
            parts += ["", body, ""]
        parts.append("</details>")
        return "\n".join(parts)

    # --- Inline markup -----------------------------------------------------------

    @staticmethod
    def text(value: str | None) -> str:
        return escape_text(_WS_RE.sub(" ", value)) if value else ""

    def children(self, element: etree._Element) -> str:
        parts = [self.text(element.text)]
        for child in element:
            parts += [self.inline(child), self.text(child.tail)]
        return "".join(parts)

    def inline(self, element: etree._Element) -> str:
        tag = _tag(element)
        if not tag:
            return ""  # HTML comment; the caller appends its tail
        if tag == "br":
            return _BREAK
        if tag == "img":
            return self.image(element)
        if tag == "code":
            return _code_span(element.text_content())
        if tag in ("strong", "b"):
            return _wrap("**", self.children(element))
        if tag in ("em", "i", "cite", "dfn"):
            return _wrap("*", self.children(element))
        if tag in ("s", "del", "strike"):
            return _wrap("~~", self.children(element))
        if tag in ("sub", "sup"):
            content = self.children(element).strip()
            return f"<{tag}>{content}</{tag}>" if content else ""
        if tag == "a":
            return self.link(element)
        # span, u, mark, abbr and blocks inside a line (<a><div>...</div></a>): just the content.
        content = self.children(element)
        return f" {content} " if tag in _BLOCK_TAGS else content

    def link(self, element: etree._Element) -> str:
        href = (element.get("href") or "").strip()
        content = self.children(element)
        if not href or href.startswith(("#", "javascript:")):
            return content
        if not content.replace(_BREAK, "").strip():
            if element.find(".//img") is not None:
                return content
            content = escape_text(href)
        content = content.replace(_BREAK, " ")
        lead = content[: len(content) - len(content.lstrip(" "))]
        trail = content[len(content.rstrip(" ")) :]
        return f"{lead}[{content.strip()}]({_destination(href)}){trail}"

    def image(self, element: etree._Element) -> str:
        classes = (element.get("class") or "").split()
        if "formula" in classes:
            tex = " ".join((element.get("source") or element.get("alt") or "").split())
            if tex:
                return f"${tex}$" if "inline" in classes else f"$${tex}$$"
        src = element.get("src") or ""
        if not src or src.startswith(
            "data:image/gif;base64,R0lGOD"
        ):  # empty lazy-loading placeholder
            return ""
        return self.image_ref(src, " ".join((element.get("alt") or "").split()))


def html_to_markdown(markup: str, image_ref: ImageRef | None = None) -> str:
    """Return Markdown for an HTML fragment of an article or comment."""
    return MarkdownConverter(image_ref).convert(markup)


class _EmbeddedImages:
    """Reference-style images.

    ``![caption][img-N]`` in the text, ``[img-N]: data:...`` at the end.
    """

    def __init__(self, images: ImageStore | None, embed: bool) -> None:
        self.images = images if embed else None
        self.labels: dict[str, str] = {}

    def __call__(self, url: str, alt: str) -> str:
        image = self.images.get(url) if self.images is not None else None
        if image is None or not image.mime.startswith("image/"):
            return f"![{escape_text(alt)}]({_destination(url)})"
        label = self.labels.setdefault(url, f"img-{len(self.labels) + 1}")
        return f"![{escape_text(alt)}][{label}]"

    def definitions(self) -> str:
        if self.images is None:
            return ""
        return "\n".join(
            f"[{label}]: {self.images.get(url).data_uri}" for url, label in self.labels.items()
        )


def _yaml(value: str) -> str:
    """Return a double-quoted YAML string: JSON escaping is a subset of YAML."""
    return json.dumps(value, ensure_ascii=False)


def _front_matter(article: Article) -> str:
    lines = [
        "---",
        f"title: {_yaml(article.title)}",
        f"url: {_yaml(article.url)}",
        f"id: {_yaml(article.id)}",
        f"author: {_yaml(article.author.alias)}",
    ]
    if article.published:
        lines.append(f"published: {article.published.isoformat()}")
    if article.company:
        lines.append(f"company: {_yaml(article.company.title)}")
    lines.append(f"lang: {_yaml(article.lang)}")
    for key, values in (("hubs", [h.title for h in article.hubs]), ("tags", article.tags)):
        lines.append(f"{key}: [{', '.join(_yaml(v) for v in values)}]")
    if article.is_translation:
        lines.append("translation: true")
        if article.original_url:
            lines.append(f"original_url: {_yaml(article.original_url)}")
    lines.append("---")
    return "\n".join(lines)


def _user_link(alias: str, url: str) -> str:
    return f"[@{escape_text(alias)}]({_destination(url)})"


def _header(article: Article, tz: tzinfo | None) -> list[str]:
    parts = [f"# {escape_text(' '.join(article.title.split()))}"]
    byline = [_user_link(article.author.alias, article.author.url)]
    if article.author.fullname:
        byline[0] += f" ({escape_text(article.author.fullname)})"
    if article.published:
        byline.append(format_datetime(article.published, tz))
    if article.company:
        byline.append(
            f"блог компании [{escape_text(article.company.title)}]"
            f"({_destination(article.company.url)})"
        )
    parts.append(" · ".join(byline))

    meta = []
    if article.post_type == "news":
        meta.append("Новость")
    if article.is_translation:
        meta.append("Перевод")
    if article.complexity:
        meta.append(f"Уровень сложности: {escape_text(article.complexity)}")
    if article.reading_time:
        meta.append(f"Время на прочтение: {article.reading_time} мин")
    if article.statistics.views:
        meta.append(f"Просмотры: {format_count(article.statistics.views)}")
    if article.hubs:
        star = "\\*"
        hubs = (
            f"[{escape_text(h.title)}{star if h.is_profiled else ''}]({_destination(h.url)})"
            for h in article.hubs
        )
        meta.append("Хабы: " + ", ".join(hubs))
    if article.is_translation and (article.original_author or article.original_url):
        note = "Автор оригинала: " + escape_text(article.original_author or "—")
        if article.original_url:
            note += f" · [Оригинал]({_destination(article.original_url)})"
        meta.append(note)
    if meta:
        parts.append("\\\n".join(meta))
    parts.append(f"Источник: <{article.url}>")
    return parts


def _footer(article: Article) -> list[str]:
    lines = []
    if article.tags:
        lines.append("**Теги:** " + ", ".join(escape_text(t) for t in article.tags))
    stats = article.statistics
    lines.append(
        f"**Рейтинг:** {format_score(stats.score)} · **Просмотры:** {format_count(stats.views)} · "
        f"**В закладках:** {stats.favorites} · **Комментарии:** {stats.comments}"
    )
    return ["---", "\\\n".join(lines)]


def _comment(
    comment: Comment, converter: MarkdownConverter, tz: tzinfo | None, depth: int
) -> list[str]:
    head = [
        f"**{_user_link(comment.author.alias, comment.author.url)}**"
        if comment.author
        else "**НЛО**"
    ]
    if comment.is_post_author:
        head[0] += " (автор)"
    if comment.published:
        head.append(format_datetime(comment.published, tz))
    head.append(f"рейтинг {format_score(comment.score)}")
    if comment.deleted and not comment.html:
        body = "*НЛО прилетело и опубликовало эту надпись здесь*"
    else:
        body = converter.convert(comment.html)
    text = " · ".join(head) + ("\n\n" + body if body else "")
    for _ in range(depth):
        text = _quote(text)
    blocks = [text]
    for child in comment.children:
        blocks += _comment(child, converter, tz, min(depth + 1, MAX_QUOTE_DEPTH))
    return blocks


def render_markdown(
    article: Article, options: ExportOptions, images: ImageStore | None = None
) -> str:
    """Render the article as Markdown: front matter, header, text, footer, comments, image data."""
    refs = _EmbeddedImages(images, options.image_mode is ImageMode.EMBED)
    converter = MarkdownConverter(refs)
    parts = [_front_matter(article), *_header(article, options.tz)]
    body = converter.convert(article.body_html)
    if body:
        parts.append(body)
    parts += _footer(article)
    if options.include_comments:
        parts.append(f"## Комментарии ({article.comments_total})")
        if not article.comments:
            parts.append("*Комментариев пока нет*")
        for root in article.comments:
            parts += _comment(root, converter, options.tz, 0)
    if refs.labels:
        parts.append(refs.definitions())
    return "\n\n".join(parts) + "\n"


class MarkdownExporter:
    """Exporter to ``.md``."""

    extension = ".md"
    title = "Markdown"

    def export(
        self, article: Article, path: Path, options: ExportOptions, images: ImageStore | None
    ) -> Path:
        path = Path(path)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(render_markdown(article, options, images))
        return path
