"""Main window.

A header with modes, language and theme, and the "Single article" and "Batch" tabs.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import (
    QCoreApplication,
    QEvent,
    QLibraryInfo,
    QLocale,
    QSettings,
    Qt,
    QTranslator,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QShortcut,
    QShowEvent,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..client import HabrClient
from ..exporters import ExportFormat, ExportOptions, PdfExporter, collect_image_urls, get_exporter
from ..exporters.pdf import PdfPrinter
from ..formatting import format_count, format_score, format_short_datetime
from ..i18n import Bindings, Language, set_language, tr, trn
from ..images import ImageMode, ImageStore
from ..models import Article
from ..render_html import render_article
from ..service import download_images, load_article, suggest_filename
from ..url import InvalidArticleUrlError, parse_article_id
from . import theme
from .batch_panel import BatchPanel
from .system import article_url_from_clipboard, open_path, reveal_path, short_url
from .theme import ThemeMode
from .widgets import (
    Card,
    ElidedLabel,
    Field,
    FormatTile,
    IconLabel,
    RichButton,
    ScrollColumn,
    SegmentedControl,
    ToggleSwitch,
    elide_lines,
    label,
    separator,
    set_prop,
)
from .workers import TaskThread

#: System languages for which the UI defaults to Russian; English for all others.
_RUSSIAN_LOCALES = {
    QLocale.Language.Russian,
    QLocale.Language.Belarusian,
    QLocale.Language.Ukrainian,
    QLocale.Language.Kazakh,
}


def system_language() -> Language:
    """Return the default UI language based on the system language (``QLocale``)."""
    return Language.RU if QLocale().language() in _RUSSIAN_LOCALES else Language.EN


#: Translator for Qt's own texts (context menus of input fields, dialog buttons); ``None`` for
#: English.
_qt_translator: QTranslator | None = None


def install_qt_translation(language: Language) -> None:
    """Translate Qt's built-in texts into ``language``: Qt itself is English only."""
    global _qt_translator  # noqa: PLW0603 - like the UI language, the translator is process-wide
    app = QCoreApplication.instance()
    if app is None:
        return
    if _qt_translator is not None:
        app.removeTranslator(_qt_translator)
        _qt_translator = None
    if language is Language.EN:
        return
    translator = QTranslator(app)
    path = QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)
    if translator.load(f"qtbase_{language.value}", path):
        app.installTranslator(translator)
        _qt_translator = translator


def _format_hint(fmt: ExportFormat) -> str:
    """Return the tooltip for a format tile; tile order follows :class:`ExportFormat`."""
    hint = tr(f"format.hint.{fmt.value}")
    return f"{hint}\n{tr('format.beta_hint')}" if fmt.beta else hint


class ArticleCard(Card):
    """Summary of the loaded article: company and author, a two-line title, counters."""

    def __init__(self) -> None:
        super().__init__(margins=(16, 14, 16, 14), spacing=0)
        self.article_title = ""

        self.empty = QWidget()
        empty = QVBoxLayout(self.empty)
        empty.setContentsMargins(0, 2, 0, 2)
        empty.setSpacing(4)
        self.t = Bindings()
        self.article: Article | None = None
        empty.addWidget(self.t.text(label("", "strong"), "card.empty.title"))
        empty.addWidget(self.t.text(label("", "small", wrap=True), "card.empty.hint"))
        self.box.addWidget(self.empty)

        self.content = QWidget()
        content = QVBoxLayout(self.content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.initials = label("", name="initials")
        self.initials.setFixedSize(22, 22)
        self.initials.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head.addWidget(self.initials)
        self.byline = ElidedLabel("", "muted")
        head.addWidget(self.byline, 1)
        content.addLayout(head)
        self.title_label = label("", "title")
        content.addWidget(self.title_label)

        meta = QHBoxLayout()
        meta.setSpacing(12)
        self.date_label = label("", "small")
        meta.addWidget(self.date_label)
        self.reading_label = self._counter(meta, "clock")
        self.score_icon = IconLabel("up", "ok", 14, stroke=2.4)
        self.score_label = self._counter(meta, None, self.score_icon)
        self.views_label = self._counter(meta, "eye")
        self.comments_label = self._counter(meta, "comment")
        meta.addStretch(1)
        content.addLayout(meta)
        self.content.hide()
        self.box.addWidget(self.content)

    @staticmethod
    def _counter(row: QHBoxLayout, icon: str | None, widget: IconLabel | None = None) -> QLabel:
        pair = QHBoxLayout()
        pair.setSpacing(4)
        pair.addWidget(widget or IconLabel(icon or "dot", "muted", 14))
        text = label("", "small")
        pair.addWidget(text)
        row.addLayout(pair)
        return text

    def retranslate(self) -> None:
        self.t.refresh()
        if self.article is not None:
            self.show_article(self.article)

    def show_article(self, article: Article) -> None:
        self.article = article
        author = article.author
        name = author.fullname or f"@{author.alias}"
        if article.company:
            self.initials.setText(article.company.title[:2].upper())
            self.byline.setText(f"{article.company.title} · {name}")
        else:
            self.initials.setText((author.fullname or author.alias)[:1].upper())
            self.byline.setText(f"{name} · @{author.alias}" if author.fullname else name)
        self.article_title = article.title
        self.title_label.setToolTip(article.title)
        self._elide_title()
        self.date_label.setText(format_short_datetime(article.published))
        self.reading_label.setText(
            tr("card.minutes", n=article.reading_time) if article.reading_time else "—"
        )
        score = article.statistics.score
        self.score_icon.set_icon("up" if score >= 0 else "down", "ok" if score >= 0 else "err")
        self.score_label.setText(format_score(score))
        set_prop(self.score_label, "tone", "ok" if score >= 0 else "err")
        self.views_label.setText(format_count(article.statistics.views))
        self.comments_label.setText(str(article.comments_total))
        self.empty.hide()
        self.content.show()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._elide_title()

    def _elide_title(self) -> None:
        width = self.contentsRect().width() - 32
        self.title_label.setText(
            elide_lines(self.article_title, self.title_label.fontMetrics(), width, 2)
        )


class MainWindow(QMainWindow):
    """Window with two modes: a single article (link -> load -> save) and batch export."""

    #: The article was loaded and parsed.
    loaded = pyqtSignal(object)
    #: The file was saved (path).
    saved = pyqtSignal(str)
    #: A loading or saving error (text).
    error = pyqtSignal(str)

    def __init__(
        self,
        client: HabrClient | None = None,
        *,
        with_preview: bool = True,
        settings: QSettings | None = None,
    ) -> None:
        super().__init__()
        self.client = client or HabrClient()
        self.article: Article | None = None
        self.images: ImageStore | None = None
        self._task: TaskThread | None = None
        self._printer: PdfPrinter | None = None
        self.saved_path: str | None = None
        self._show_error_dialogs = True
        self._preview_dir = tempfile.TemporaryDirectory(prefix="habr-preview-")
        self._icon_buttons: list[tuple[QAbstractButton, str, str, int]] = []
        self._clipboard_url: str | None = None
        self._dismissed_clipboard: str | None = None
        self.settings = settings or QSettings("HabrExporter", "HabrExporter")
        self.theme_mode = _enum(
            ThemeMode, self.settings.value("theme", ThemeMode.SYSTEM.value), ThemeMode.SYSTEM
        )
        self.language = _enum(
            Language, self.settings.value("language", system_language().value), Language.RU
        )
        set_language(self.language)
        install_qt_translation(self.language)
        self.t = Bindings()
        #: Re-renders the status line in the new language (``None`` means waiting for a message from
        #: the work).
        self._status_render: Callable[[], None] | None = lambda: self.status_label.setText(
            tr("status.idle")
        )

        self.t.call(lambda: self.setWindowTitle(tr("app.window_title")))
        self.setWindowIcon(QGuiApplication.windowIcon())
        self.setMinimumSize(960, 600)
        self._fit_to_screen()
        self._build_ui(with_preview)
        self.apply_theme()
        self._restore_settings()
        self._on_options_changed()
        self._update_state()
        self._update_language_button()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._on_system_scheme)
        QGuiApplication.clipboard().dataChanged.connect(self.check_clipboard)

    # --- UI ----------------------------------------------------------------------

    def _build_ui(self, with_preview: bool) -> None:
        central = QWidget()
        central.setObjectName("page")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.tabs = QTabWidget()  # before the header: the mode buttons switch tabs
        outer.addWidget(self._build_appbar())
        self.tabs.tabBar().hide()
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(self._build_single(with_preview), "single")
        self.batch_panel = BatchPanel(self.client, self.settings, blocked=lambda: self._busy_single)
        self.batch_panel.running_changed.connect(self._update_state)
        self.batch_panel.progress_changed.connect(self._on_batch_progress)
        self.tabs.addTab(self.batch_panel, "batch")
        self.t.call(lambda: self.tabs.setTabText(0, tr("nav.single")))
        self.t.call(lambda: self.tabs.setTabText(1, tr("nav.batch")))
        outer.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        QShortcut(QKeySequence.StandardKey.Save, self, activated=self.save)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self._focus_url)

    def _build_appbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("appbar")
        bar.setFixedHeight(56)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 0, 20, 0)
        row.setSpacing(24)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        self.logo_label = QLabel()
        self.logo_label.setFixedSize(32, 32)
        brand.addWidget(self.logo_label)
        names = QVBoxLayout()
        names.setSpacing(0)
        names.addWidget(label("Habr Exporter", "appname"))
        names.addWidget(self.t.text(label("", "small"), "app.tagline"))
        brand.addLayout(names)
        row.addLayout(brand)

        self.nav = SegmentedControl()
        self.t.name(self.nav, "nav.label")
        single = RichButton("segment", margins=(16, 0, 16, 0))
        self.nav_single_label = self.t.text(single.adopt(label("")), "nav.single")
        single.box.addWidget(self.nav_single_label)
        batch = RichButton("segment", margins=(16, 0, 16, 0))
        self.nav_batch_label = self.t.text(batch.adopt(label("")), "nav.batch")
        batch.box.addWidget(self.nav_batch_label)
        self.nav_badge = batch.adopt(label("", name="badge"))
        self.nav_badge.setFixedHeight(18)
        self.nav_badge.hide()
        batch.box.addWidget(self.nav_badge)
        for key, button, text in (
            ("single", single, self.nav_single_label),
            ("batch", batch, self.nav_batch_label),
        ):
            button.setFixedHeight(32)
            button.toggled.connect(
                lambda on, text=text: set_prop(text, "role", "strong" if on else "muted")
            )
            self.nav.add(key, "", button=button)
        self.nav.changed.connect(lambda key: self.tabs.setCurrentIndex(0 if key == "single" else 1))
        self.nav.set_value("single")
        self.nav.setFixedHeight(40)
        row.addWidget(self.nav, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addStretch(1)

        switches = QHBoxLayout()
        switches.setSpacing(8)
        self.language_button = QPushButton()
        self.language_button.clicked.connect(self.cycle_language)
        self._register_icon(self.language_button, "language", "muted")
        self.theme_button = QPushButton()
        self.theme_button.clicked.connect(self.cycle_theme)
        self._register_icon(self.theme_button, "theme", "muted")
        for button in (self.language_button, self.theme_button):
            button.setProperty("kind", "ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            switches.addWidget(button)
        row.addLayout(switches)
        return bar

    def _build_single(self, with_preview: bool) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        page_box = QVBoxLayout(page)
        page_box.setContentsMargins(0, 0, 0, 0)
        page_box.setSpacing(0)
        content = QVBoxLayout()
        content.setContentsMargins(20, 16, 20, 14)
        content.setSpacing(12)

        url_row = QHBoxLayout()
        url_row.setSpacing(10)
        self.url_field = Field("link", large=True)
        self.url_edit = self.url_field.edit
        self.t.placeholder(self.url_edit, "single.url_placeholder")
        self.url_edit.setClearButtonEnabled(True)
        self.t.name(self.url_edit, "single.url_name")
        self.url_edit.returnPressed.connect(self.load)
        self.url_edit.textChanged.connect(self._on_url_changed)
        self.article_badge = label("", name="okBadge")
        self.article_badge.setFixedHeight(24)
        self.article_badge.hide()
        self.url_field.row.addWidget(self.article_badge)
        url_row.addWidget(self.url_field, 1)
        self.load_button = self.t.text(QPushButton(), "single.load")
        self.load_button.setProperty("kind", "primary")
        self.load_button.setMinimumHeight(48)
        self.load_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.load_button.clicked.connect(self.load)
        url_row.addWidget(self.load_button)
        content.addLayout(url_row)
        content.addWidget(self._build_clipboard_banner())

        body = QHBoxLayout()
        body.setSpacing(16)
        self.settings_column = left = ScrollColumn(380)
        self.article_card = ArticleCard()
        left.box.addWidget(self.article_card)
        left.box.addWidget(self._build_save_card(), 1)
        body.addWidget(left)
        body.addWidget(self._build_preview_card(with_preview), 1)
        content.addLayout(body, 1)

        page_box.addLayout(content, 1)
        page_box.addWidget(self._build_statusbar())
        return page

    def _build_clipboard_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("banner")
        banner.setProperty("tone", "info")
        row = QHBoxLayout(banner)
        row.setContentsMargins(12, 6, 6, 6)
        row.setSpacing(10)
        row.addWidget(IconLabel("clipboard", "accent_ink", 16))
        self.clipboard_label = ElidedLabel("", "ink")
        row.addWidget(self.clipboard_label, 1)
        self.paste_button = self.t.text(QPushButton(), "clipboard.paste")
        self.paste_button.setProperty("kind", "primary")
        self.paste_button.setProperty("variant", "small")
        self.paste_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.paste_button.clicked.connect(self.paste_from_clipboard)
        row.addWidget(self.paste_button)
        close = QPushButton()
        close.setProperty("kind", "close")
        self.t.tip(close, "clipboard.hide")
        self.t.name(close, "clipboard.hide_name")
        close.clicked.connect(self.dismiss_clipboard)
        self._register_icon(close, "cross", "accent_ink", 14)
        row.addWidget(close)
        banner.hide()
        self.clipboard_banner = banner
        return banner

    def _build_save_card(self) -> Card:
        card = Card(spacing=0)
        card.box.addWidget(self.t.text(label("", "section"), "single.section.format"))
        card.box.addSpacing(8)
        grid = QGridLayout()
        grid.setSpacing(8)
        self.format_group = QButtonGroup(self)
        self.format_buttons: dict[ExportFormat, FormatTile] = {}
        for index, fmt in enumerate(ExportFormat):
            tile = FormatTile(fmt.value.upper(), "", beta=fmt.beta)
            self.t.call(lambda tile=tile, fmt=fmt: tile.set_hint(tr(f"format.short.{fmt.value}")))
            self.t.call(lambda tile=tile, fmt=fmt: tile.setToolTip(_format_hint(fmt)))
            tile.setAccessibleName(fmt.label)
            tile.toggled.connect(lambda on: on and self._on_options_changed())
            self.format_group.addButton(tile)
            self.format_buttons[fmt] = tile
            grid.addWidget(tile, index // 3, index % 3)
        card.box.addLayout(grid)

        card.box.addSpacing(16)
        card.box.addWidget(self.t.text(label("", "section"), "single.section.images"))
        card.box.addSpacing(8)
        self.image_control = SegmentedControl()
        self.t.name(self.image_control, "single.images_name")
        self.image_buttons: dict[ImageMode, QPushButton] = {}
        for mode in ImageMode:
            button = self.image_buttons[mode] = self.image_control.add(mode, "")
            self.t.text(button, f"images.{mode.value}.title")
            self.t.tip(button, f"images.{mode.value}.hint")
        self.image_control.changed.connect(self._on_options_changed)
        card.box.addWidget(self.image_control)
        card.box.addSpacing(6)
        self.image_hint = label("", "small", wrap=True)
        card.box.addWidget(self.image_hint)

        card.box.addSpacing(14)
        card.box.addWidget(separator())
        card.box.addSpacing(12)
        comments = QHBoxLayout()
        texts = QVBoxLayout()
        texts.setSpacing(0)
        texts.addWidget(self.t.text(label("", "strong"), "comments.title"))
        self.comments_info = label("", "small")
        self.t.call(self._update_comments_info)
        texts.addWidget(self.comments_info)
        comments.addLayout(texts)
        comments.addStretch(1)
        self.comments_check = ToggleSwitch()
        self.comments_check.setChecked(True)
        self.t.name(self.comments_check, "comments.toggle")
        self.t.tip(self.comments_check, "comments.toggle")
        self.comments_check.toggled.connect(self._refresh_preview)
        comments.addWidget(self.comments_check)
        card.box.addLayout(comments)

        card.box.addStretch(1)
        card.box.addSpacing(12)
        self.save_button = QPushButton(tr("single.save", fmt="PDF"))
        self.save_button.setProperty("kind", "primary")
        self.save_button.setMinimumHeight(46)
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.t.tip(self.save_button, "single.save_tip")
        self.save_button.clicked.connect(self.save)
        self._register_icon(self.save_button, "download", "accent_text", 18)
        card.box.addWidget(self.save_button)
        return card

    def _build_preview_card(self, with_preview: bool) -> Card:
        card = Card(margins=(0, 0, 0, 0), spacing=0)
        header = QFrame()
        header.setObjectName("cardHeader")
        header.setFixedHeight(44)
        row = QHBoxLayout(header)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(12)
        row.addWidget(self.t.text(label("", "strong"), "preview.title"))
        row.addWidget(self.t.text(label("", "small"), "preview.subtitle"))
        row.addStretch(1)
        self.habr_link = self.t.text(QPushButton(), "preview.open_habr")
        self.habr_link.setProperty("kind", "link")
        self.habr_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.habr_link.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft
        )  # icon to the right of the text
        self.habr_link.clicked.connect(self._open_on_habr)
        self.habr_link.hide()
        self._register_icon(self.habr_link, "external", "accent_ink", 14)
        row.addWidget(self.habr_link)
        card.box.addWidget(header)

        body = QWidget()
        body.setObjectName("previewBody")
        body_box = QVBoxLayout(body)
        body_box.setContentsMargins(12, 12, 12, 12)
        self.preview_stack = QStackedWidget()
        self.preview_empty = self._build_preview_empty()
        self.preview_stack.addWidget(self.preview_empty)
        self.preview = None
        if with_preview:
            try:
                from PyQt6.QtWebEngineCore import QWebEngineSettings
                from PyQt6.QtWebEngineWidgets import QWebEngineView

                self.preview = QWebEngineView()
                # The preview loads from a local file, while images come from habrastorage.org.
                self.preview.settings().setAttribute(
                    QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True
                )
                self.preview_stack.addWidget(self.preview)
            except ImportError:  # pragma: no cover - WebEngine is a required dependency
                self.preview = None
        body_box.addWidget(self.preview_stack)
        card.box.addWidget(body, 1)
        return card

    def _build_preview_empty(self) -> QWidget:
        empty = QWidget()
        box = QVBoxLayout(empty)
        box.setSpacing(10)
        box.addStretch(1)
        tile = QLabel()
        tile.setObjectName("emptyIcon")
        tile.setFixedSize(64, 64)
        inner = QVBoxLayout(tile)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(
            IconLabel("document", "accent", 30, stroke=1.8), 0, Qt.AlignmentFlag.AlignCenter
        )
        box.addWidget(tile, 0, Qt.AlignmentFlag.AlignHCenter)
        title = self.t.text(label("", "title"), "preview.empty.title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(title)
        hint = label("", "small", wrap=True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setFixedWidth(380)

        def update_hint() -> None:
            hint.setText(tr("preview.empty.hint"))
            hint.setMinimumHeight(
                hint.heightForWidth(380)
            )  # word wrap does not grow the height by itself in a centering layout

        self.t.call(update_hint)
        box.addWidget(hint, 0, Qt.AlignmentFlag.AlignHCenter)
        box.addStretch(2)
        return empty

    def _build_statusbar(self) -> QFrame:
        bar = QFrame()
        bar.setObjectName("statusbar")
        bar.setFixedHeight(40)
        row = QHBoxLayout(bar)
        row.setContentsMargins(20, 0, 20, 0)
        row.setSpacing(10)
        self.status_icon = IconLabel("check", "ok", 16, stroke=2.6)
        self.status_icon.hide()
        row.addWidget(self.status_icon)
        self.progress = QProgressBar()
        self.progress.setProperty("variant", "thin")
        self.progress.setFixedWidth(220)
        self.progress.setTextVisible(False)
        self.progress.hide()
        row.addWidget(self.progress)
        self.status_label = ElidedLabel(tr("status.idle"), "muted")
        row.addWidget(self.status_label, 1)
        self.open_file_button = self.t.text(QPushButton(), "status.open_file")
        self.reveal_button = self.t.text(QPushButton(), "status.reveal")
        for button, slot in (
            (self.open_file_button, self.open_saved),
            (self.reveal_button, self.reveal_saved),
        ):
            button.setProperty("kind", "ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(slot)
            button.hide()
            row.addWidget(button)
        return bar

    def _fit_to_screen(self) -> None:
        """Size the window at startup.

        As in the mockup, but no larger than the available screen area.
        """
        screen = self.screen() or QGuiApplication.primaryScreen()
        if (
            screen is None
        ):  # pragma: no cover - a screen always exists, even on the offscreen platform
            self.resize(1280, 820)
            return
        available = screen.availableGeometry()
        self.resize(min(1280, available.width() - 48), min(820, available.height() - 48))

    def _register_icon(self, button: QAbstractButton, name: str, tone: str, size: int = 16) -> None:
        """Register a button icon that is recolored when the theme changes."""
        self._icon_buttons.append((button, name, tone, size))

    # --- Theme -------------------------------------------------------------------

    def apply_theme(self, mode: ThemeMode | None = None) -> None:
        if mode is not None:
            self.theme_mode = mode
        colors = theme.resolve(self.theme_mode)
        theme.apply(self, colors)
        ratio = self.devicePixelRatioF() or 1.0
        self.logo_label.setPixmap(theme.logo_pixmap(colors, 32, ratio))
        self._update_theme_button()
        for button, name, tone, size in self._icon_buttons:
            button.setIcon(QIcon(theme.icon_pixmap(name, getattr(colors, tone), size, ratio)))

    def cycle_theme(self) -> None:
        self.apply_theme(self.theme_mode.next())
        self.settings.setValue("theme", self.theme_mode.value)

    def _on_system_scheme(self, *_args: object) -> None:
        if self.theme_mode is ThemeMode.SYSTEM:
            self.apply_theme()

    def _update_theme_button(self) -> None:
        self.theme_button.setText(self.theme_mode.label)
        self.theme_button.setToolTip(tr("theme.tooltip", name=self.theme_mode.label.lower()))

    # --- Language ----------------------------------------------------------------

    def apply_language(self, language: Language) -> None:
        """Switch the window to ``language`` on the fly: labels, tooltips and current messages."""
        self.language = language
        set_language(language)
        install_qt_translation(language)
        self.t.refresh()
        self.article_card.retranslate()
        self.batch_panel.retranslate()
        self._update_theme_button()
        self._update_language_button()
        self._on_options_changed()
        if self.article is not None:
            self.article_badge.setText(tr("single.badge", id=self.article.id))
        if self._clipboard_url and self.clipboard_banner.isVisibleTo(self):
            self.clipboard_label.setText(tr("clipboard.found", url=short_url(self._clipboard_url)))
        # While work is running, its next step will send a message in the new language.
        if not self._busy_single and self._status_render is not None:
            self._status_render()

    def cycle_language(self) -> None:
        self.apply_language(self.language.next())
        self.settings.setValue("language", self.language.value)

    def _update_language_button(self) -> None:
        self.language_button.setText(self.language.label)
        self.language_button.setToolTip(tr("language.tooltip", name=self.language.label))

    # --- State -------------------------------------------------------------------

    @property
    def show_error_dialogs(self) -> bool:
        """Whether errors are shown in dialogs in both tabs (disabled in tests)."""
        return self._show_error_dialogs

    @show_error_dialogs.setter
    def show_error_dialogs(self, value: bool) -> None:
        self._show_error_dialogs = value
        self.batch_panel.show_error_dialogs = value

    @property
    def _busy_single(self) -> bool:
        return self._task is not None or self._printer is not None

    @property
    def busy(self) -> bool:
        return self._busy_single or self.batch_panel.running

    def selected_format(self) -> ExportFormat:
        return next(f for f, b in self.format_buttons.items() if b.isChecked())

    def selected_image_mode(self) -> ImageMode:
        return next(m for m, b in self.image_buttons.items() if b.isChecked())

    def export_options(self) -> ExportOptions:
        return ExportOptions(
            format=self.selected_format(),
            image_mode=self.selected_image_mode(),
            include_comments=self.comments_check.isChecked(),
        )

    def _restore_settings(self) -> None:
        fmt = self.settings.value("format", ExportFormat.PDF.value)
        mode = self.settings.value("image_mode", ImageMode.EMBED.value)
        self.format_buttons[_enum(ExportFormat, fmt, ExportFormat.PDF)].setChecked(True)
        self.image_buttons[_enum(ImageMode, mode, ImageMode.EMBED)].setChecked(True)
        self.comments_check.setChecked(self.settings.value("comments", True, type=bool))

    def _store_settings(self) -> None:
        options = self.export_options()
        self.settings.setValue("format", options.format.value)
        self.settings.setValue("image_mode", options.image_mode.value)
        self.settings.setValue("comments", options.include_comments)

    def _on_options_changed(self, *_args: object) -> None:
        if not any(b.isChecked() for b in self.format_buttons.values()):
            return
        fmt = self.selected_format()
        self.image_control.setEnabled(fmt.embeds_images)
        mode = self.image_control.value()
        if not fmt.embeds_images:
            self.image_hint.setText(tr("images.no_embed_note"))
        elif mode is not None:
            self.image_hint.setText(tr(f"images.{mode.value}.note"))
        self.save_button.setText(tr("single.save", fmt=fmt.value.upper()))

    def _update_state(self) -> None:
        text = self.url_edit.text().strip()
        try:
            valid = bool(parse_article_id(text))
        except InvalidArticleUrlError:
            valid = False
        self.load_button.setEnabled(valid and not self.busy)
        self.save_button.setEnabled(self.article is not None and not self.busy)
        self.url_field.setEnabled(not self.busy)
        self.paste_button.setEnabled(not self.busy)
        running = self.batch_panel.running
        if not running:
            self.nav_badge.clear()
        self.nav_badge.setVisible(running and bool(self.nav_badge.text()))
        self.batch_panel.update_state()

    def _on_url_changed(self, text: str) -> None:
        self._update_state()
        if text.strip():
            self.clipboard_banner.hide()
        try:
            same = self.article is not None and parse_article_id(text.strip()) == self.article.id
        except InvalidArticleUrlError:
            same = False
        self.article_badge.setVisible(same)

    def _on_batch_progress(self, done: int, total: int) -> None:
        self.nav_badge.setText(f"{done}/{total}")
        self.nav_badge.setVisible(self.batch_panel.running)

    def _set_status(self, message: str, done: int = 0, total: int = 0) -> None:
        self.status_label.setText(message)
        self.status_label.setToolTip("")
        set_prop(self.status_label, "role", "muted")
        set_prop(self.status_label, "tone", None)
        self.status_icon.hide()
        self.open_file_button.hide()
        self.reveal_button.hide()
        if self._busy_single:
            self.progress.show()
            self.progress.setRange(0, total if total else 0)
            if total:
                self.progress.setValue(done)
        else:
            self.progress.hide()

    def _start(self, fn, on_success) -> None:  # type: ignore[no-untyped-def]
        task = TaskThread(fn, self)
        task.progress.connect(self._set_status)
        task.succeeded.connect(on_success)
        task.failed.connect(self._on_failed)
        task.finished.connect(self._on_task_finished)
        self._task = task
        self._update_state()
        self._status_render = None
        self._set_status(tr("status.preparing"))
        task.start()

    def _on_task_finished(self) -> None:
        if self._task is not None:
            self._task.deleteLater()
        self._task = None
        self._update_state()
        if not self._busy_single:
            self.progress.hide()

    def _on_failed(self, message: str) -> None:
        self._printer = None
        self._set_status(tr("status.error", message=message))
        self._status_render = lambda: self.status_label.setText(tr("status.error", message=message))
        set_prop(self.status_label, "tone", "err")
        self.progress.hide()
        self._update_state()
        self.error.emit(message)
        if self.show_error_dialogs:
            QMessageBox.critical(self, tr("dialog.error"), message)

    # --- Clipboard ---------------------------------------------------------------

    def check_clipboard(self) -> None:
        """Offer to paste an article link if the clipboard holds one and the field is empty."""
        url = None
        if not self.busy and not self.url_edit.text().strip():
            url = article_url_from_clipboard(QGuiApplication.clipboard().text())
        if url is None or url == self._dismissed_clipboard:
            self.clipboard_banner.hide()
            return
        self._clipboard_url = url
        self.clipboard_label.setText(tr("clipboard.found", url=short_url(url)))
        self.clipboard_banner.show()

    def paste_from_clipboard(self) -> None:
        if not self._clipboard_url:
            return
        self.url_edit.setText(self._clipboard_url)
        self.clipboard_banner.hide()
        self.load()

    def dismiss_clipboard(self) -> None:
        self._dismissed_clipboard = self._clipboard_url
        self.clipboard_banner.hide()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self.check_clipboard()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.check_clipboard()

    def _focus_url(self) -> None:
        self.nav.set_value("single")
        self.url_edit.setFocus()
        self.url_edit.selectAll()

    # --- Loading -----------------------------------------------------------------

    def load(self) -> None:
        if self.busy or not self.load_button.isEnabled():
            return
        url = self.url_edit.text().strip()
        client = self.client
        self.clipboard_banner.hide()
        self._start(lambda progress: load_article(url, client, progress), self._on_loaded)

    def _on_loaded(self, article: Article) -> None:
        self.article = article
        self.images = None
        self.article_card.show_article(article)
        self.article_badge.setText(tr("single.badge", id=article.id))
        self._on_url_changed(self.url_edit.text())
        self.habr_link.show()
        self._update_comments_info()
        self._refresh_preview()
        images = len(collect_image_urls(article))

        def loaded_text() -> str:
            comments = trn("count.comments", article.comments_total)
            return tr(
                "single.loaded",
                title=article.title,
                comments=comments,
                images=trn("count.images", images),
            )

        self._set_status(loaded_text())
        self._status_render = lambda: self.status_label.setText(loaded_text())
        self.loaded.emit(article)

    def _update_comments_info(self) -> None:
        if self.article is None:
            self.comments_info.setText(tr("single.comments.tree"))
            return
        total = self.article.comments_total
        self.comments_info.setText(
            trn("single.comments.count", total) if total else tr("single.comments.none")
        )

    def _refresh_preview(self) -> None:
        if self.preview is None or self.article is None:
            return
        html = render_article(self.article, include_comments=self.comments_check.isChecked())
        path = Path(self._preview_dir.name) / "preview.html"
        path.write_text(html, encoding="utf-8")
        self.preview.load(QUrl.fromLocalFile(str(path)))
        self.preview_stack.setCurrentWidget(self.preview)

    def _open_on_habr(self) -> None:
        if self.article is not None:
            QDesktopServices.openUrl(QUrl(self.article.url))

    # --- Saving ------------------------------------------------------------------

    def save(self) -> None:
        if self.article is None or self.busy:
            return
        options = self.export_options()
        self._store_settings()
        last_dir = self.settings.value("last_dir", str(Path.home() / "Documents"))
        suggested = str(Path(last_dir) / suggest_filename(self.article, options.format))
        file_filter = f"{options.format.value.upper()} (*{options.format.extension})"
        path, _ = QFileDialog.getSaveFileName(
            self, tr("single.save_dialog"), suggested, file_filter
        )
        if not path:
            return
        target = Path(path)
        if target.suffix.lower() != options.format.extension:
            target = target.with_name(target.name + options.format.extension)
        self.settings.setValue("last_dir", str(target.parent))
        self.export_to(target, options)

    def export_to(self, target: Path, options: ExportOptions) -> None:
        """Save the current article without dialogs (also used by tests)."""
        assert self.article is not None
        article, client = self.article, self.client
        cached = self.images

        def job(progress):  # type: ignore[no-untyped-def]
            images = cached
            if options.downloads_images and images is None:
                images = download_images(article, client, progress)
            if options.format is ExportFormat.PDF:
                progress(tr("progress.pdf_layout"), 0, 0)
                return images, PdfExporter().render(article, options, images)
            progress(tr("progress.saving", fmt=options.format.value.upper()), 0, 0)
            get_exporter(options.format).export(article, target, options, images)
            return images, None

        def done(result: tuple[ImageStore | None, str | None]) -> None:
            images, html = result
            if images is not None:
                self.images = images
            if html is None:
                self._on_saved(str(target))
                return
            self._print_pdf(html, target)

        self._start(job, done)

    def _print_pdf(self, html: str, target: Path) -> None:
        self._printer = PdfPrinter(self)
        self._printer.finished.connect(self._on_saved)
        self._printer.failed.connect(self._on_failed)
        self._set_status(tr("progress.pdf_print"))
        self._update_state()
        self._printer.print_html(html, target)

    def _on_saved(self, path: str) -> None:
        if self._printer is not None:
            self._printer.deleteLater()
            self._printer = None
        self._update_state()
        self.progress.hide()
        self.status_label.setText(tr("status.saved", path=path))
        self._status_render = lambda: self.status_label.setText(tr("status.saved", path=path))
        set_prop(self.status_label, "role", "strong")
        set_prop(self.status_label, "tone", None)
        self.status_label.setToolTip(path)
        self.status_icon.show()
        self.open_file_button.show()
        self.reveal_button.show()
        self.saved_path = path
        self.saved.emit(path)

    def open_saved(self) -> None:
        if self.saved_path:
            open_path(self.saved_path)

    def reveal_saved(self) -> None:
        if self.saved_path:
            reveal_path(self.saved_path)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._task is not None:
            self._task.wait(3000)
        self.batch_panel.shutdown()
        self._preview_dir.cleanup()
        super().closeEvent(event)


def _enum(enum_cls, value, default):  # type: ignore[no-untyped-def]
    try:
        return enum_cls(value)
    except ValueError:
        return default
