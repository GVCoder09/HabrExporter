"""Exporters that save an article in various file formats."""

from __future__ import annotations

from .base import Exporter, ExportFormat, ExportOptions, collect_image_urls
from .docx import DocxExporter
from .epub import EpubExporter
from .fb2 import Fb2Exporter
from .html import HtmlExporter
from .json import JsonExporter
from .markdown import MarkdownExporter
from .pdf import PdfExporter

__all__ = [
    "DocxExporter",
    "EpubExporter",
    "ExportFormat",
    "ExportOptions",
    "Exporter",
    "Fb2Exporter",
    "HtmlExporter",
    "JsonExporter",
    "MarkdownExporter",
    "PdfExporter",
    "collect_image_urls",
    "get_exporter",
]


def get_exporter(fmt: ExportFormat) -> Exporter:
    """Return the exporter for the given format."""
    return {
        ExportFormat.PDF: PdfExporter,
        ExportFormat.HTML: HtmlExporter,
        ExportFormat.DOCX: DocxExporter,
        ExportFormat.EPUB: EpubExporter,
        ExportFormat.FB2: Fb2Exporter,
        ExportFormat.MARKDOWN: MarkdownExporter,
        ExportFormat.JSON: JsonExporter,
    }[fmt]()
