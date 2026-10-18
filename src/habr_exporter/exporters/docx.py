"""DOCX export (Microsoft Word, LibreOffice Writer).

The article HTML is walked recursively and converted into Word paragraphs, runs, tables and
pictures. Supported are headings, lists (including nested ones), quotes, highlighted code
blocks, tables, spoilers, formulas, links and images. The comment tree is shown with indents and
a vertical line on the left, as on the site.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace
from datetime import tzinfo
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.shape import CT_Inline
from docx.shared import Cm, Emu, Pt, RGBColor
from docx.table import _Cell
from docx.text.paragraph import Paragraph
from lxml import etree
from lxml import html as lhtml

from ..formatting import format_count, format_datetime, format_number, format_score, plural
from ..highlight import highlight, language_from_classes
from ..images import DOCX_NATIVE, ImageData, ImageMode, ImageStore, Raster, raster_for_docx
from ..models import Article, Comment, Company, User
from .base import ExportOptions

TEXT = RGBColor(0x33, 0x33, 0x33)
MUTED = RGBColor(0x92, 0x9C, 0xA5)
LINK = RGBColor(0x54, 0x8E, 0xAA)
POSITIVE = RGBColor(0x7E, 0xA9, 0x6C)
NEGATIVE = RGBColor(0xD0, 0x6B, 0x6B)
ACCENT_HEX = "65A3BE"
THREAD_HEX = "D5DEE4"
CODE_BG_HEX = "F7F7F7"

BODY_FONT = "Arial"
CODE_FONT = "Consolas"

PAGE_CONTENT_WIDTH = Cm(17)
#: Width of the article column on the site; images are scaled relative to it.
SITE_COLUMN_PX = 780
COMMENT_INDENT = Cm(0.6)
MAX_COMMENT_INDENT_LEVEL = 8

_BLOCK_TAGS = {
    "p", "div", "section", "article", "figure", "blockquote", "pre", "ul", "ol", "li", "table",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "details", "summary", "figcaption", "dl", "dt", "dd",
}  # fmt: skip
_HEADING_SIZES = {1: 22, 2: 18, 3: 15, 4: 13, 5: 12, 6: 11}
_WS_RE = re.compile(r"\s+")


# --- Low-level OOXML helpers ---------------------------------------------------


_PPR_AFTER_SHD = (
    "w:tabs",
    "w:suppressAutoHyphens",
    "w:kinsoku",
    "w:wordWrap",
    "w:overflowPunct",
    "w:topLinePunct",
    "w:autoSpaceDE",
    "w:autoSpaceDN",
    "w:bidi",
    "w:adjustRightInd",
    "w:snapToGrid",
    "w:spacing",
    "w:ind",
    "w:contextualSpacing",
    "w:mirrorIndents",
    "w:suppressOverlap",
    "w:jc",
    "w:textDirection",
    "w:textAlignment",
    "w:textboxTightWrap",
    "w:outlineLvl",
    "w:divId",
    "w:cnfStyle",
    "w:rPr",
    "w:sectPr",
    "w:pPrChange",
)
_PPR_AFTER_PBDR = ("w:shd", *_PPR_AFTER_SHD)
_RPR_AFTER_SHD = (
    "w:fitText",
    "w:vertAlign",
    "w:rtl",
    "w:cs",
    "w:em",
    "w:lang",
    "w:eastAsianLayout",
    "w:specVanish",
    "w:oMath",
)
_TCPR_AFTER_SHD = (
    "w:noWrap",
    "w:tcMar",
    "w:textDirection",
    "w:tcFitText",
    "w:vAlign",
    "w:hideMark",
)
_TBLPR_AFTER_BORDERS = (
    "w:shd",
    "w:tblLayout",
    "w:tblCellMar",
    "w:tblLook",
    "w:tblCaption",
    "w:tblDescription",
)


def _shading(fill: str) -> Any:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    return shd


def _set_cell_shading(cell: _Cell, fill: str) -> None:
    cell._tc.get_or_add_tcPr().insert_element_before(_shading(fill), *_TCPR_AFTER_SHD)


def _run_shading(run: Any, fill: str) -> None:
    run._r.get_or_add_rPr().insert_element_before(_shading(fill), *_RPR_AFTER_SHD)


def _paragraph_shading(paragraph: Paragraph, fill: str) -> None:
    paragraph._p.get_or_add_pPr().insert_element_before(_shading(fill), *_PPR_AFTER_SHD)


def _paragraph_border(
    paragraph: Paragraph, side: str, color: str, size: int = 12, space: int = 8
) -> None:
    """Return a paragraph border on one side (``top``, ``left``, ``bottom``).

    ``size`` is in 1/8 pt.
    """
    ppr = paragraph._p.get_or_add_pPr()
    borders = ppr.find(qn("w:pBdr"))
    if borders is None:
        borders = ppr.insert_element_before(OxmlElement("w:pBdr"), *_PPR_AFTER_PBDR)
    if borders.find(qn(f"w:{side}")) is not None:
        return
    edge = OxmlElement(f"w:{side}")
    edge.set(qn("w:val"), "single")
    edge.set(qn("w:sz"), str(size))
    edge.set(qn("w:space"), str(space))
    edge.set(qn("w:color"), color)
    order = ("top", "left", "bottom", "right", "between", "bar")
    successors = [qn(f"w:{name}") for name in order[order.index(side) + 1 :]]
    for existing in borders:
        if existing.tag in successors:
            existing.addprevious(edge)
            break
    else:
        borders.append(edge)


def _table_without_borders(table: Any) -> None:
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
        edge = OxmlElement(f"w:{side}")
        edge.set(qn("w:val"), "nil")
        borders.append(edge)
    table._tbl.tblPr.insert_element_before(borders, *_TBLPR_AFTER_BORDERS)


def add_hyperlink(paragraph: Paragraph, url: str) -> Any:
    """Create an empty ``<w:hyperlink>`` and return it; runs are appended to it."""
    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    paragraph._p.append(link)
    return link


# --- Run formatting ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fmt:
    """Set of inline formatting flags."""

    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    code: bool = False
    sup: bool = False
    sub: bool = False
    color: RGBColor | None = None
    size: float | None = None
    href: str | None = None


PLAIN = Fmt()


@dataclass(slots=True)
class Ctx:
    """Block output context: indent and the "branch line" for comments."""

    indent: int = 0  # EMU
    thread_line: bool = False
    quote: bool = False
    list_stack: list[str] = field(default_factory=list)
    font_size: float | None = None


class DocxBuilder:
    """Builds the DOCX document of an article."""

    def __init__(self, options: ExportOptions, images: ImageStore | None) -> None:
        self.options = options
        self.images = images
        self.link_images = options.image_mode is ImageMode.LINK
        self.tz: tzinfo | None = options.tz
        self.doc = Document()
        self._setup_document()

    # --- Styles ----------------------------------------------------------------

    def _setup_document(self) -> None:
        section = self.doc.sections[0]
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.left_margin = section.right_margin = Cm(2)
        section.top_margin = section.bottom_margin = Cm(1.8)

        normal = self.doc.styles["Normal"]
        normal.font.name = BODY_FONT
        normal.font.size = Pt(11)
        normal.font.color.rgb = TEXT
        rfonts = normal.element.rPr.rFonts
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rfonts.set(qn(attr), BODY_FONT)
        normal.paragraph_format.space_after = Pt(6)
        normal.paragraph_format.line_spacing = 1.2

        for level, size in _HEADING_SIZES.items():
            style = self.doc.styles[f"Heading {level}"]
            style.font.name = BODY_FONT
            style.font.size = Pt(size)
            style.font.bold = True
            style.font.italic = False
            style.font.color.rgb = TEXT
            rpr = style.element.get_or_add_rPr()
            rfonts = rpr.find(qn("w:rFonts"))
            if rfonts is None:
                rfonts = OxmlElement("w:rFonts")
                rpr.append(rfonts)
            for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
                rfonts.set(qn(attr), BODY_FONT)
            for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:cstheme", "w:eastAsiaTheme"):
                rfonts.attrib.pop(qn(attr), None)
            style.paragraph_format.space_before = Pt(14 if level <= 2 else 10)
            style.paragraph_format.space_after = Pt(6)

    # --- Paragraphs and runs -----------------------------------------------------

    def _paragraph(self, container: Any, ctx: Ctx, style: str | None = None) -> Paragraph:
        paragraph = container.add_paragraph(style=style)
        if ctx.indent:
            paragraph.paragraph_format.left_indent = Emu(ctx.indent)
        if ctx.quote:
            _paragraph_border(paragraph, "left", ACCENT_HEX, size=18, space=10)
        elif ctx.thread_line:
            _paragraph_border(paragraph, "left", THREAD_HEX, size=8, space=10)
        return paragraph

    def _run(self, paragraph: Paragraph, text: str, fmt: Fmt, parent: Any = None) -> Any:
        if fmt.href and parent is None:
            parent = add_hyperlink(paragraph, fmt.href)
        if parent is not None:
            run_el = OxmlElement("w:r")
            parent.append(run_el)
            from docx.text.run import Run

            run = Run(run_el, paragraph)
            run.text = text
        else:
            run = paragraph.add_run(text)
        font = run.font
        font.bold = fmt.bold or None
        font.italic = fmt.italic or None
        font.strike = fmt.strike or None
        font.superscript = fmt.sup or None
        font.subscript = fmt.sub or None
        if fmt.underline:
            font.underline = True
        if fmt.code:
            font.name = CODE_FONT
            font.size = Pt((fmt.size or 11) * 0.9)
            _run_shading(run, "F0F0F0")
        elif fmt.size:
            font.size = Pt(fmt.size)
        if fmt.href:
            font.color.rgb = LINK
        elif fmt.color is not None:
            font.color.rgb = fmt.color
        return run

    def add_link(self, paragraph: Paragraph, url: str, text: str, fmt: Fmt = PLAIN) -> None:
        self._run(paragraph, text, replace(fmt, href=url))

    # --- Images ----------------------------------------------------------------

    def _picture_width(self, natural_px: int | None, attr_width: str | None, max_width: int) -> int:
        width_px = natural_px or SITE_COLUMN_PX
        if attr_width and attr_width.isdigit():
            width_px = int(attr_width)
        width = int(PAGE_CONTENT_WIDTH * min(width_px, SITE_COLUMN_PX) / SITE_COLUMN_PX)
        return max(min(width, max_width), int(Cm(0.3)))

    def add_image(
        self,
        paragraph: Paragraph,
        url: str,
        *,
        attr_width: str | None = None,
        max_width: int | None = None,
        height: int | None = None,
        alt: str = "",
    ) -> bool:
        """Insert a picture into a paragraph, or a link on failure.

        Return True if the picture was inserted.
        """
        raster = None
        if self.images is not None:
            image = self.images.get(url)
            raster = raster_for_docx(image) if image is not None else None
        if raster is None:
            label = f"🖼 {alt or 'Изображение'}: {url}"
            self._run(paragraph, label, Fmt(href=url, size=9))
            return False
        run = paragraph.add_run()
        if height is not None:
            self._picture(run, image, raster, height=Emu(height))
        else:
            limit = max_width or int(PAGE_CONTENT_WIDTH)
            self._picture(
                run, image, raster, width=Emu(self._picture_width(raster.width, attr_width, limit))
            )
        return True

    def _picture(
        self,
        run: Any,
        image: ImageData,
        raster: Raster,
        *,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        """Add a picture to a run: embedded, or in link mode linked to its URL.

        A linked picture stores only the address; Word loads it from Habr when the document is
        opened. Formats Word cannot show by URL (SVG formulas, WebP) are embedded as PNG anyway.
        """
        linkable = image.url.startswith(("http://", "https://")) and image.mime in DOCX_NATIVE
        if not (self.link_images and linkable):
            run.add_picture(io.BytesIO(raster.data), width=width, height=height)
            return
        if width is None and height is None:
            width, height = Emu(raster.width * 9525), Emu(raster.height * 9525)  # 96 dpi
        elif height is None:
            height = Emu(int(width * raster.height / max(raster.width, 1)))
        elif width is None:
            width = Emu(int(height * raster.width / max(raster.height, 1)))
        part = run.part
        r_id = part.relate_to(image.url, RT.IMAGE, is_external=True)
        filename = image.url.rsplit("/", 1)[-1].split("?", 1)[0] or "image"
        inline = CT_Inline.new_pic_inline(part.next_id, r_id, filename, width, height)
        blip = inline.xpath(".//a:blip")[0]
        del blip.attrib[qn("r:embed")]
        blip.set(qn("r:link"), r_id)
        run._r.add_drawing(inline)

    def _avatar_run(self, paragraph: Paragraph, user: User | None, size: Cm) -> None:
        if self.images is None or user is None or not user.avatar_url:
            return
        image = self.images.get(user.avatar_url)
        raster = raster_for_docx(image) if image else None
        if raster is not None:
            self._picture(paragraph.add_run(), image, raster, width=size, height=size)
            paragraph.add_run("  ")

    # --- HTML traversal ----------------------------------------------------------

    def html(self, container: Any, markup: str, ctx: Ctx | None = None, base: Fmt = PLAIN) -> None:
        """Output an HTML fragment into a container (the document or a table cell)."""
        if not markup or not markup.strip():
            return
        root = lhtml.fragment_fromstring(markup, create_parent="div")
        self.blocks(container, root, ctx or Ctx(), base)

    def blocks(self, container: Any, element: etree._Element, ctx: Ctx, fmt: Fmt) -> None:
        """Process the content of a block element: a mix of text, inline and block elements."""
        current: list[Paragraph | None] = [None]

        def para() -> Paragraph:
            if current[0] is None:
                current[0] = self._paragraph(container, ctx)
            return current[0]

        def close() -> None:
            current[0] = None

        if element.text and element.text.strip():
            self._text(para, element.text, fmt)
        for child in element:
            if not isinstance(child.tag, str):  # HTML comments
                if child.tail and child.tail.strip():
                    self._text(para, child.tail, fmt)
                continue
            tag = child.tag.lower()
            if tag in _BLOCK_TAGS or (tag == "img" and self._is_block_image(child)):
                close()
                self.block(container, child, ctx, fmt)
            elif tag == "br":
                if current[0] is not None:
                    current[0].add_run().add_break()
                else:
                    para()
            else:
                self.inline(para, child, fmt)
            if child.tail and child.tail.strip():
                self._text(para, child.tail, fmt)
            elif child.tail and current[0] is not None and tag not in _BLOCK_TAGS:
                self._text(para, " ", fmt)

    @staticmethod
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
        }:
            text = (parent.text or "") + "".join((c.tail or "") for c in parent)
            return not text.strip()
        return False

    def _text(self, para: Any, text: str, fmt: Fmt) -> None:
        text = _WS_RE.sub(" ", text)
        paragraph = para()
        if not paragraph.runs and not paragraph._p.findall(qn("w:hyperlink")):
            text = text.lstrip()
        if text:
            self._run(paragraph, text, fmt)

    def inline(self, para: Any, element: etree._Element, fmt: Fmt) -> None:
        tag = element.tag.lower() if isinstance(element.tag, str) else ""
        if tag in {"b", "strong"}:
            fmt = replace(fmt, bold=True)
        elif tag in {"i", "em", "cite", "dfn"}:
            fmt = replace(fmt, italic=True)
        elif tag in {"u", "ins"}:
            fmt = replace(fmt, underline=True)
        elif tag in {"s", "strike", "del"}:
            fmt = replace(fmt, strike=True)
        elif tag in {"code", "kbd", "samp", "tt"}:
            fmt = replace(fmt, code=True)
        elif tag == "sup":
            fmt = replace(fmt, sup=True)
        elif tag == "sub":
            fmt = replace(fmt, sub=True)
        elif tag == "a":
            href = element.get("href")
            if href and not href.startswith("#"):
                fmt = replace(fmt, href=href)
        elif tag == "img":
            src = element.get("src")
            if src:
                is_formula = "formula" in (element.get("class") or "")
                height = int(Cm(0.45)) if is_formula else None
                self.add_image(
                    para(),
                    src,
                    attr_width=element.get("width"),
                    height=height,
                    max_width=int(PAGE_CONTENT_WIDTH),
                    alt=element.get("alt") or "",
                )
            return
        elif tag == "br":
            para().add_run().add_break()
            return
        elif tag in _BLOCK_TAGS:  # a block inside an inline element is output as text
            pass

        if element.text:
            self._text(para, element.text, fmt)
        for child in element:
            if isinstance(child.tag, str):
                self.inline(para, child, fmt)
            if child.tail:
                self._text(para, child.tail, fmt)

    def block(self, container: Any, element: etree._Element, ctx: Ctx, fmt: Fmt) -> None:
        tag = element.tag.lower()
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = min(int(tag[1]) + (1 if tag == "h1" else 0), 6)
            paragraph = self._paragraph(container, ctx, style=f"Heading {level}")
            self.inline(lambda: paragraph, _strip_tag(element), fmt)
        elif tag == "p":
            paragraph = self._paragraph(container, ctx)
            if (element.get("class") or "") == "habr-embed":
                fmt = replace(fmt, size=10)
            if element.get("align") == "center" or "center" in (element.get("style") or ""):
                paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            self.inline(lambda: paragraph, _strip_tag(element), fmt)
            if (
                not paragraph.runs
                and not paragraph._p.findall(qn("w:hyperlink"))
                and not len(element)
            ):
                # Keep an empty <p> as spacing, but without extra paragraph spacing.
                paragraph.paragraph_format.space_after = Pt(0)
        elif tag == "pre":
            self.code_block(container, element, ctx)
        elif tag == "blockquote":
            self.blocks(
                container,
                element,
                replace(ctx, quote=True, indent=ctx.indent + int(Cm(0.3))),
                replace(fmt, color=RGBColor(0x55, 0x55, 0x55)),
            )
        elif tag in {"ul", "ol"}:
            self.list_block(container, element, ctx, fmt, ordered=tag == "ol")
        elif tag == "li":
            self.list_block(container, element, ctx, fmt, ordered=False, single=True)
        elif tag == "img":
            paragraph = self._paragraph(container, ctx)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            self.add_image(
                paragraph,
                element.get("src") or "",
                attr_width=element.get("width"),
                max_width=int(PAGE_CONTENT_WIDTH) - ctx.indent,
                alt=element.get("alt") or "",
            )
        elif tag == "figure":
            self.blocks(container, element, ctx, fmt)
        elif tag == "figcaption":
            paragraph = self._paragraph(container, ctx)
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            self.inline(lambda: paragraph, _strip_tag(element), replace(fmt, color=MUTED, size=9))
        elif tag == "hr":
            paragraph = self._paragraph(container, ctx)
            _paragraph_border(paragraph, "bottom", "DEDEDE", size=6, space=1)
        elif tag == "table":
            self.table(container, element, ctx, fmt)
        elif tag == "details" or "spoiler" in (element.get("class") or "").split():
            self.spoiler(container, element, ctx, fmt)
        elif tag == "summary":
            paragraph = self._paragraph(container, ctx)
            self.inline(lambda: paragraph, _strip_tag(element), replace(fmt, bold=True, color=LINK))
        elif tag in {"dt"}:
            paragraph = self._paragraph(container, ctx)
            self.inline(lambda: paragraph, _strip_tag(element), replace(fmt, bold=True))
        elif tag in {"dd"}:
            self.blocks(container, element, replace(ctx, indent=ctx.indent + int(Cm(0.8))), fmt)
        else:  # div, section, dl and other containers
            if "spoiler_title" in (element.get("class") or ""):
                paragraph = self._paragraph(container, ctx)
                self.inline(
                    lambda: paragraph, _strip_tag(element), replace(fmt, bold=True, color=LINK)
                )
                return
            self.blocks(container, element, ctx, fmt)

    def code_block(self, container: Any, element: etree._Element, ctx: Ctx) -> None:
        code_el = element.find("code")
        language = language_from_classes(
            code_el.get("class") if code_el is not None else element.get("class")
        )
        source = (code_el if code_el is not None else element).text_content().strip("\n")
        paragraph = self._paragraph(container, ctx)
        _paragraph_shading(paragraph, CODE_BG_HEX)
        # A border in the background color gives inner padding, like <pre> on the site.
        for side in ("top", "left", "bottom", "right"):
            _paragraph_border(paragraph, side, CODE_BG_HEX, size=4, space=4)
        pf = paragraph.paragraph_format
        pf.space_before = Pt(4)
        pf.space_after = Pt(10)
        pf.line_spacing = 1.0
        for span in highlight(source, language):
            lines = span.text.split("\n")
            for index, line in enumerate(lines):
                if index:
                    paragraph.add_run().add_break(WD_BREAK.LINE)
                if not line:
                    continue
                run = paragraph.add_run(line)
                run.font.name = CODE_FONT
                run.font.size = Pt(9)
                if span.color:
                    run.font.color.rgb = RGBColor.from_string(span.color.upper())
                run.font.bold = span.bold or None
                run.font.italic = span.italic or None

    def list_block(
        self,
        container: Any,
        element: etree._Element,
        ctx: Ctx,
        fmt: Fmt,
        *,
        ordered: bool,
        single: bool = False,
    ) -> None:
        items = (
            [element]
            if single
            else [c for c in element if isinstance(c.tag, str) and c.tag == "li"]
        )
        depth = len(ctx.list_stack)
        start = (
            int(element.get("start", "1"))
            if ordered and (element.get("start") or "").isdigit()
            else 1
        )
        indent = ctx.indent + int(Cm(0.75))
        for number, item in enumerate(items, start=start):
            bullet = f"{number}." if ordered else ("•", "◦", "▪")[depth % 3]
            item_ctx = replace(
                ctx, indent=indent, list_stack=[*ctx.list_stack, "ol" if ordered else "ul"]
            )
            first_index = len(_paragraphs(container))
            self.blocks(container, item, item_ctx, fmt)
            paragraphs = _paragraphs(container)
            first = (
                paragraphs[first_index]
                if len(paragraphs) > first_index
                else self._paragraph(container, item_ctx)
            )
            # The bullet goes at the start of the item's first paragraph, with a hanging indent.
            marker = OxmlElement("w:r")
            text = OxmlElement("w:t")
            text.set(qn("xml:space"), "preserve")
            text.text = bullet + "\t"
            marker.append(text)
            ppr = first._p.find(qn("w:pPr"))
            if ppr is not None:
                ppr.addnext(marker)
            else:
                first._p.insert(0, marker)
            pf = first.paragraph_format
            pf.left_indent = Emu(indent)
            pf.first_line_indent = Emu(-int(Cm(0.6)))
            pf.tab_stops.add_tab_stop(Emu(indent))
            pf.space_after = Pt(3)

    def table(self, container: Any, element: etree._Element, ctx: Ctx, fmt: Fmt) -> None:
        rows = element.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
        if not rows:
            return
        grid: list[list[tuple[etree._Element, int]]] = []
        for row in rows:
            cells = [
                (
                    c,
                    max(int(c.get("colspan", "1") or 1), 1)
                    if (c.get("colspan") or "1").isdigit()
                    else 1,
                )
                for c in row
                if isinstance(c.tag, str) and c.tag in {"td", "th"}
            ]
            grid.append(cells)
        columns = max((sum(span for _, span in r) for r in grid), default=0)
        if not columns:
            return
        table = container.add_table(rows=len(grid), cols=columns)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for r, cells in enumerate(grid):
            col = 0
            for cell_el, span in cells:
                if col >= columns:
                    break
                cell = table.cell(r, col)
                if span > 1:
                    cell = cell.merge(table.cell(r, min(col + span, columns) - 1))
                if cell_el.tag == "th":
                    _set_cell_shading(cell, "F7F9FB")
                self._fill_cell(cell, cell_el, fmt, header=cell_el.tag == "th")
                col += span
        container.add_paragraph()

    def _fill_cell(
        self, cell: _Cell, element: etree._Element, fmt: Fmt, header: bool = False
    ) -> None:
        first = cell.paragraphs[0]
        before = len(cell.paragraphs)
        self.blocks(cell, element, Ctx(), replace(fmt, bold=fmt.bold or header, size=10))
        if len(cell.paragraphs) > before and not first.runs:
            first._p.getparent().remove(first._p)

    def spoiler(self, container: Any, element: etree._Element, ctx: Ctx, fmt: Fmt) -> None:
        summary = element.find("summary")
        if summary is None:
            summary = next(iter(element.xpath("./*[contains(@class, 'spoiler_title')]")), None)
        title = (
            _WS_RE.sub(" ", summary.text_content()).strip() if summary is not None else "Спойлер"
        )
        paragraph = self._paragraph(container, ctx)
        self._run(paragraph, "▸ " + (title or "Спойлер"), replace(fmt, bold=True, color=LINK))
        inner = replace(ctx, indent=ctx.indent + int(Cm(0.4)), quote=True)
        body = lhtml.Element("div")
        body.text = element.text if summary is None else summary.tail
        for child in element:
            if child is summary:
                continue
            body.append(_clone(child))
        self.blocks(container, body, inner, fmt)

    # --- Document sections -------------------------------------------------------

    def header(self, article: Article) -> None:
        doc = self.doc
        meta = doc.add_paragraph()
        self._avatar_run(meta, article.author, Cm(0.6))
        self.add_link(meta, article.author.url, article.author.alias, Fmt(bold=True, size=10))
        self._run(
            meta, "   " + format_datetime(article.published, self.tz), Fmt(color=MUTED, size=10)
        )

        title = doc.add_paragraph()
        self._run(title, article.title, Fmt(bold=True, size=20))
        title.paragraph_format.space_after = Pt(8)

        details = []
        if article.complexity:
            details.append(f"Уровень сложности: {article.complexity}")
        if article.reading_time:
            details.append(f"Время на прочтение: {article.reading_time} мин")
        if article.statistics.views:
            details.append(f"Количество просмотров: {format_count(article.statistics.views)}")
        if details:
            self._run(doc.add_paragraph(), "     ".join(details), Fmt(color=MUTED, size=9))

        if article.hubs:
            hubs = doc.add_paragraph()
            for i, hub in enumerate(article.hubs):
                if i:
                    self._run(hubs, ", ", Fmt(color=MUTED, size=9))
                self.add_link(
                    hubs, hub.url, hub.title + ("*" if hub.is_profiled else ""), Fmt(size=9)
                )

        labels = (["Новость"] if article.post_type == "news" else []) + (
            ["Перевод"] if article.is_translation else []
        )
        if labels:
            self._run(
                doc.add_paragraph(),
                "  ".join(f"[{x}]" for x in labels),
                Fmt(color=LINK, size=9, bold=True),
            )
        if article.is_translation and (article.original_author or article.original_url):
            p = doc.add_paragraph()
            self._run(
                p, f"Автор оригинала: {article.original_author or ''}  ", Fmt(color=MUTED, size=9)
            )
            if article.original_url:
                self.add_link(p, article.original_url, "Оригинал", Fmt(size=9))

    def footer(self, article: Article) -> None:
        doc = self.doc
        for caption, items in (
            ("Теги: ", [(t, None) for t in article.tags]),
            ("Хабы: ", [(h.title, h.url) for h in article.hubs]),
        ):
            if not items:
                continue
            p = doc.add_paragraph()
            self._run(p, caption, Fmt(color=MUTED, size=9))
            for i, (text, url) in enumerate(items):
                if i:
                    self._run(p, ", ", Fmt(color=MUTED, size=9))
                if url:
                    self.add_link(p, url, text, Fmt(size=9))
                else:
                    self._run(p, text, Fmt(color=LINK, size=9))
        stats = article.statistics
        p = doc.add_paragraph()
        _paragraph_border(p, "top", "EEEEEE", size=6, space=6)
        self._run(p, "Рейтинг ", Fmt(color=MUTED, size=10))
        self._run(
            p, format_score(stats.score), Fmt(bold=True, size=10, color=_score_color(stats.score))
        )
        self._run(
            p,
            f"     Просмотры {format_count(stats.views)}     В закладках {stats.favorites}"
            f"     Комментарии {stats.comments}",
            Fmt(color=MUTED, size=10),
        )

    def _card(
        self,
        *,
        logo_url: str | None,
        title: str,
        title_url: str,
        alias: str | None,
        lines: list[list[tuple[str, str | None]]],
        counters: list[tuple[str, str, RGBColor]],
    ) -> None:
        table = self.doc.add_table(rows=1, cols=3)
        _table_without_borders(table)
        widths = (Cm(1.8), Cm(11.2), Cm(4))
        for cell, width in zip(table.rows[0].cells, widths, strict=True):
            cell.width = width
        logo_cell, main_cell, counter_cell = table.rows[0].cells

        if self.images is not None and logo_url and (image := self.images.get(logo_url)):
            raster = raster_for_docx(image)
            if raster:
                self._picture(
                    logo_cell.paragraphs[0].add_run(), image, raster, width=Cm(1.3), height=Cm(1.3)
                )

        head = main_cell.paragraphs[0]
        self.add_link(head, title_url, title, Fmt(bold=True, size=13))
        if alias:
            self._run(head, f"  @{alias}", Fmt(color=MUTED, size=10))
        for line in lines:
            p = main_cell.add_paragraph()
            p.paragraph_format.space_after = Pt(2)
            for i, (text, href) in enumerate(line):
                if i:
                    self._run(p, " · ", Fmt(color=MUTED, size=9))
                if href:
                    self.add_link(p, href, text, Fmt(size=9))
                else:
                    self._run(p, text, Fmt(size=9 if i or len(line) > 1 else 10))

        for i, (value, label, color) in enumerate(counters):
            counter_par = counter_cell.paragraphs[0] if i == 0 else counter_cell.add_paragraph()
            counter_par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            counter_par.paragraph_format.space_after = Pt(0)
            self._run(counter_par, value, Fmt(bold=True, size=14, color=color))
            self._run(counter_par, f" {label}", Fmt(color=MUTED, size=8))
        self.doc.add_paragraph()

    def company_card(self, company: Company) -> None:
        lines: list[list[tuple[str, str | None]]] = []
        if company.description:
            lines.append([(company.description, None)])
        links = [("Сайт", company.site_url)] if company.site_url else []
        links += [(c.title, c.url) for c in company.contacts if c.url != company.site_url]
        if links:
            lines.append(links)
        details = [x for x in (company.location, company.staff) if x]
        if details:
            lines.append([(" · ".join(details), None)])
        counters = []
        if company.rating is not None:
            counters.append((format_number(company.rating), "Рейтинг", RGBColor(0x6C, 0x9F, 0xBB)))
        if company.subscribers is not None:
            counters.append((format_count(company.subscribers), "Подписчики", TEXT))
        self._card(
            logo_url=company.logo_url,
            title=company.title,
            title_url=company.url,
            alias=None,
            lines=lines,
            counters=counters,
        )

    def user_card(self, user: User) -> None:
        lines: list[list[tuple[str, str | None]]] = []
        if user.speciality:
            lines.append([(user.speciality, None)])
        sub = []
        if user.followers is not None:
            sub.append(
                (
                    f"{user.followers} "
                    f"{plural(user.followers, 'подписчик', 'подписчика', 'подписчиков')}",
                    None,
                )
            )
        if user.location:
            sub.append((user.location, None))
        if user.registered:
            sub.append(
                ("На Хабре с " + format_datetime(user.registered, self.tz).split(" в ")[0], None)
            )
        if sub:
            lines.append(sub)
        counters = [
            (format_number(user.karma), "Карма", _score_color(user.karma) or POSITIVE),
            (format_number(user.rating), "Рейтинг", RGBColor(0x6C, 0x9F, 0xBB)),
        ]
        self._card(
            logo_url=user.avatar_url,
            title=user.fullname or user.alias,
            title_url=user.url,
            alias=user.alias,
            lines=lines,
            counters=counters,
        )

    def comment(self, comment: Comment, depth: int) -> None:
        indent = int(COMMENT_INDENT) * min(depth, MAX_COMMENT_INDENT_LEVEL)
        ctx = Ctx(indent=indent, thread_line=depth > 0, font_size=10)
        head = self._paragraph(self.doc, ctx)
        head.paragraph_format.space_before = Pt(8)
        head.paragraph_format.space_after = Pt(2)
        head.paragraph_format.keep_with_next = True
        self._avatar_run(head, comment.author, Cm(0.45))
        if comment.author:
            self.add_link(head, comment.author.url, comment.author.alias, Fmt(bold=True, size=9.5))
        else:
            self._run(head, "НЛО", Fmt(bold=True, size=9.5))
        if comment.is_post_author:
            self._run(head, "  автор", Fmt(color=LINK, size=8))
        if comment.published:
            self._run(
                head, "   " + format_datetime(comment.published, self.tz), Fmt(color=MUTED, size=9)
            )

        if comment.deleted and not comment.html:
            body = self._paragraph(self.doc, ctx)
            self._run(
                body,
                "НЛО прилетело и опубликовало эту надпись здесь",
                Fmt(italic=True, color=MUTED, size=10),
            )
        else:
            self.html(self.doc, comment.html, ctx, Fmt(size=10))

        foot = self._paragraph(self.doc, ctx)
        foot.paragraph_format.space_after = Pt(4)
        self._run(foot, "Рейтинг ", Fmt(color=MUTED, size=8.5))
        score_fmt = Fmt(bold=True, size=8.5, color=_score_color(comment.score) or MUTED)
        self._run(foot, format_score(comment.score), score_fmt)
        for child in comment.children:
            self.comment(child, depth + 1)

    def build(self, article: Article) -> Any:
        doc = self.doc
        doc.core_properties.title = article.title
        doc.core_properties.author = article.author.fullname or article.author.alias
        doc.core_properties.subject = article.url
        doc.core_properties.keywords = ", ".join(article.tags)

        self.header(article)
        self.html(doc, article.body_html, Ctx())
        self.footer(article)

        heading = doc.add_paragraph()
        _paragraph_border(heading, "top", "DEDEDE", size=6, space=6)
        heading.paragraph_format.space_before = Pt(12)
        if article.company:
            self.company_card(article.company)
        self.user_card(article.author)

        if self.options.include_comments:
            title = doc.add_paragraph(style="Heading 2")
            title.add_run("Комментарии ")
            title.add_run(str(article.comments_total)).font.color.rgb = MUTED
            if not article.comments:
                self._run(
                    doc.add_paragraph(), "Комментариев пока нет", Fmt(italic=True, color=MUTED)
                )
            for comment in article.comments:
                self.comment(comment, 0)

        saved = doc.add_paragraph()
        saved.alignment = WD_ALIGN_PARAGRAPH.CENTER
        saved.paragraph_format.space_before = Pt(18)
        self._run(saved, "Сохранено с ", Fmt(color=MUTED, size=8))
        self.add_link(saved, article.url, article.url, Fmt(size=8))
        return doc


def _score_color(value: float | int | None) -> RGBColor | None:
    if not value:
        return None
    return POSITIVE if value > 0 else NEGATIVE


def _strip_tag(element: etree._Element) -> etree._Element:
    """Return a copy of the element as a neutral ``span`` (so inline() does not apply its tag)."""
    copy = _clone(element)
    copy.tag = "span"
    return copy


def _clone(element: etree._Element) -> etree._Element:
    return lhtml.fromstring(lhtml.tostring(element, encoding="unicode", with_tail=False))


def _paragraphs(container: Any) -> list[Paragraph]:
    return list(container.paragraphs)


class DocxExporter:
    """Exporter to ``.docx``."""

    extension = ".docx"
    title = "DOCX (Microsoft Word)"

    def export(
        self, article: Article, path: Path, options: ExportOptions, images: ImageStore | None
    ) -> Path:
        document = DocxBuilder(options, images).build(article)
        document.save(str(path))
        return Path(path)
