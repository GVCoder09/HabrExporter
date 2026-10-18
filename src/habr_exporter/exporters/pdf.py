"""PDF export via the Chromium engine (Qt WebEngine).

The page is printed by the same browser engine as the site, so the PDF looks like the original
article: layout, fonts, code highlighting, comment tree. Qt WebEngine objects live only in the GUI
thread, so printing (:class:`PdfPrinter`, :func:`html_to_pdf`) runs in the main thread with a
``QApplication`` created, while preparing the HTML (:meth:`PdfExporter.render`) can happen
anywhere.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PyQt6.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QMarginsF,
    QObject,
    QThread,
    QTimer,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import QGuiApplication, QPageLayout, QPageSize

from ..i18n import tr
from ..images import ImageMode, ImageStore
from ..models import Article
from ..render_html import ImageSrc, render_article
from .base import ExportOptions


class PdfExportError(RuntimeError):
    """Failed to print the PDF."""


def make_image_src(mode: ImageMode, images: ImageStore | None) -> ImageSrc:
    """Return the PDF image policy.

    * ``EMBED`` embeds images as ``data:`` URIs (a link is shown if the download failed);
    * ``LINK`` keeps the original URL: Chromium loads the image from Habr while printing, so the
      PDF still contains it, but nothing is downloaded in advance.
    """

    def resolve(url: str) -> str | None:
        if mode is ImageMode.LINK:
            return url if url.startswith(("http://", "https://")) else None
        if images is None:
            return None
        image = images.get(url)
        return image.data_uri if image is not None else None

    return resolve


def page_layout() -> QPageLayout:
    """Return the page layout: A4, portrait, 12-14 mm margins."""
    return QPageLayout(
        QPageSize(QPageSize.PageSizeId.A4),
        QPageLayout.Orientation.Portrait,
        QMarginsF(12, 14, 12, 14),
        QPageLayout.Unit.Millimeter,
    )


class PdfPrinter(QObject):
    """Asynchronously prints HTML to PDF. The result is delivered via signals."""

    finished = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None, *, timeout_ms: int = 120_000) -> None:
        super().__init__(parent)
        if QGuiApplication.instance() is None:
            raise PdfExportError(tr("error.pdf_no_app"))
        app_thread = QGuiApplication.instance().thread()
        if QThread.currentThread() != app_thread:
            raise PdfExportError(tr("error.pdf_thread"))
        self.timeout_ms = timeout_ms
        self._page = None
        self._tmp: tempfile.TemporaryDirectory[str] | None = None
        self._target = ""
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._fail(tr("error.pdf_timeout")))

    def print_html(self, html: str, path: str | Path) -> None:
        from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings

        self._cleanup()
        self._target = str(path)
        # setHtml() is limited to 2 MB, and pages with embedded images are larger, so load from a
        # file.
        self._tmp = tempfile.TemporaryDirectory(prefix="habr-export-")
        source = Path(self._tmp.name) / "article.html"
        source.write_text(html, encoding="utf-8")

        page = QWebEnginePage(self)
        settings = page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.PrintElementBackgrounds, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        page.loadFinished.connect(self._on_loaded)
        page.pdfPrintingFinished.connect(self._on_printed)
        self._page = page
        self._timer.start(self.timeout_ms)
        page.load(QUrl.fromLocalFile(str(source)))

    def _on_loaded(self, ok: bool) -> None:
        if not ok:
            self._fail(tr("error.pdf_load"))
            return
        self._page.printToPdf(self._target, page_layout())

    def _on_printed(self, file_path: str, ok: bool) -> None:
        self._timer.stop()
        self._cleanup()
        if ok and Path(file_path).exists():
            self.finished.emit(file_path)
        else:
            self.failed.emit(tr("error.pdf_write", path=file_path))

    def _fail(self, reason: str) -> None:
        self._timer.stop()
        self._cleanup()
        self.failed.emit(tr("error.pdf_failed", reason=reason))

    def _cleanup(self) -> None:
        if self._page is not None:
            self._page.loadFinished.disconnect()
            self._page.pdfPrintingFinished.disconnect()
            self._page.deleteLater()
            self._page = None
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


def html_to_pdf(html: str, path: Path, *, timeout_ms: int = 120_000) -> Path:
    """Print HTML to PDF synchronously, spinning the event loop around :class:`PdfPrinter`."""
    printer = PdfPrinter(timeout_ms=timeout_ms)
    loop = QEventLoop()
    outcome: dict[str, str] = {}
    printer.finished.connect(lambda p: (outcome.setdefault("ok", p), loop.quit()))
    printer.failed.connect(lambda m: (outcome.setdefault("error", m), loop.quit()))
    printer.print_html(html, path)
    if not outcome:
        loop.exec()
    printer.deleteLater()

    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    if "error" in outcome:
        raise PdfExportError(outcome["error"])
    return Path(outcome["ok"])


class PdfExporter:
    """Exporter to ``.pdf``."""

    extension = ".pdf"
    title = "PDF"

    def render(self, article: Article, options: ExportOptions, images: ImageStore | None) -> str:
        """Return the HTML for printing (thread-safe)."""
        return render_article(
            article,
            make_image_src(options.image_mode, images),
            tz=options.tz,
            include_comments=options.include_comments,
            for_print=True,
        )

    def export(
        self, article: Article, path: Path, options: ExportOptions, images: ImageStore | None
    ) -> Path:
        return html_to_pdf(self.render(article, options, images), Path(path))
