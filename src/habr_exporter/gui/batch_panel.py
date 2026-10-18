"""The "Batch" tab.

A user's bookmarks, an author's articles or links from a file, saved to a folder.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, QSettings, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..batch import (
    DEFAULT_DELAY,
    ArticleRef,
    BatchItem,
    BatchReport,
    BatchSource,
    ItemStatus,
    SourceKind,
    normalize_alias,
    retry_item,
    run_batch,
)
from ..client import HabrClient
from ..exporters import ExportFormat, ExportOptions
from ..exporters.pdf import PdfExportError, PdfPrinter
from ..formatting import format_duration, format_eta, format_short_datetime
from ..i18n import Bindings, tr
from ..images import ImageMode
from .system import open_path
from .widgets import (
    Card,
    Chip,
    ElidedLabel,
    Field,
    IconLabel,
    ScrollColumn,
    SegmentedControl,
    Stepper,
    ToggleSwitch,
    label,
    set_prop,
)
from .workers import TaskThread

#: Log row status -> icon and its color.
_ROW_ICONS = {
    "saved": ("check", "ok"),
    "skipped": ("skip", "muted"),
    "failed": ("cross", "err"),
    "running": ("spinner", "accent"),
}
#: How many recent articles the remaining time estimate considers.
_ETA_WINDOW = 10


class MainThreadPdfPrinter(QObject):
    """Prints PDFs in the main thread on request from a worker thread.

    The worker thread calls :meth:`print_html` and waits: the request goes by signal to the GUI
    thread, :class:`PdfPrinter` prints it there, and the result comes back through a
    ``threading.Event``. This way the batch export prints articles one by one without touching
    WebEngine from another thread.
    """

    _requested = pyqtSignal(str, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._requested.connect(self._print)
        self._done = threading.Event()
        self._result: str | None = None
        self._error: str | None = None
        self._printer: PdfPrinter | None = None
        self._aborted = False

    def print_html(self, html: str, path: Path) -> Path:
        """Print HTML; called from a worker thread, which is blocked until printing finishes."""
        if self._aborted:
            raise PdfExportError(tr("error.pdf_aborted"))
        self._done.clear()
        self._result = self._error = None
        self._requested.emit(html, str(path))
        self._done.wait()
        if self._error is not None:
            raise PdfExportError(self._error)
        return Path(self._result or path)

    def abort(self) -> None:
        """Release the waiting thread and refuse new requests (when the window closes)."""
        self._aborted = True
        self._error = tr("error.pdf_aborted")
        self._done.set()

    def _print(self, html: str, path: str) -> None:
        try:
            self._printer = PdfPrinter(self)
        except (
            PdfExportError
        ) as exc:  # pragma: no cover - the main thread and QApplication always exist
            self._finish(None, str(exc))
            return
        self._printer.finished.connect(lambda result: self._finish(result, None))
        self._printer.failed.connect(lambda message: self._finish(None, message))
        self._printer.print_html(html, path)

    def _finish(self, result: str | None, error: str | None) -> None:
        if self._printer is not None:
            self._printer.deleteLater()
            self._printer = None
        self._result, self._error = result, error
        self._done.set()


class LogRow(QFrame):
    """Log row: status icon, title, details and an "Open" / "Retry" button."""

    action = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("logrow")
        self.setFixedHeight(52)
        self.item: BatchItem | None = None
        self.ref: ArticleRef | None = None
        self.status = "running"
        row = QHBoxLayout(self)
        row.setContentsMargins(20, 0, 20, 0)
        row.setSpacing(12)
        self.icon_box = QLabel()
        self.icon_box.setObjectName("statusIcon")
        self.icon_box.setFixedSize(28, 28)
        inner = QVBoxLayout(self.icon_box)
        inner.setContentsMargins(0, 0, 0, 0)
        self.icon = IconLabel("spinner", "accent", 14, stroke=3)
        inner.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.icon_box)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        self.title = ElidedLabel("")
        self.meta = ElidedLabel("", "small")
        texts.addWidget(self.title)
        texts.addWidget(self.meta)
        row.addLayout(texts, 1)
        self.action_button = QPushButton()
        self.action_button.setProperty("kind", "ghost")
        self.action_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.action_button.clicked.connect(lambda: self.action.emit(self))
        self.action_button.hide()
        row.addWidget(self.action_button)

    def _set_status(self, status: str) -> None:
        self.status = status
        name, tone = _ROW_ICONS[status]
        self.icon.set_icon(name, tone)
        set_prop(self.icon_box, "tone", status)
        set_prop(self, "running", status == "running")

    def set_running(self, ref: ArticleRef, note: str | None = None) -> None:
        self.item, self.ref = None, ref
        self._set_status("running")
        self.title.setText(ref.title or ref.url)
        self.set_meta(note or tr("batch.row.loading"))
        self.action_button.hide()

    def set_meta(self, text: str, tone: str | None = None) -> None:
        self.meta.setText(text)
        self.meta.setToolTip(text)
        set_prop(self.meta, "tone", tone)

    def set_item(self, item: BatchItem) -> None:
        self.item, self.ref = item, item.ref
        ref = item.ref
        self._set_status(item.status.value)
        self.title.setText(ref.title or (item.path.name if item.path else ref.url))
        self.title.setToolTip(ref.url)
        details = [f"@{ref.author}"] if ref.author else []
        if ref.published:
            details.append(format_short_datetime(ref.published))
        if item.status is ItemStatus.FAILED:
            self.set_meta(item.error, "err")
            self.action_button.setText(tr("batch.row.retry"))
            set_prop(self.action_button, "tone", "err")
            self.action_button.setVisible(bool(ref.id))
            return
        details.append(
            tr("batch.row.exists")
            if item.status is ItemStatus.SKIPPED
            else item.path.name
            if item.path
            else ""
        )
        self.set_meta(" · ".join(d for d in details if d))
        self.action_button.setText(tr("batch.row.open"))
        set_prop(self.action_button, "tone", "ink")
        self.action_button.setVisible(item.path is not None)


class BatchPanel(QWidget):
    """Batch export with overall "N of M" progress, a log, a time estimate and cancellation."""

    #: The export finished (including by cancellation): :class:`BatchReport`.
    finished = pyqtSignal(object)
    #: The export could not start: no such user, no file, etc.
    error = pyqtSignal(str)
    #: Background work started or finished; the window should update button states.
    running_changed = pyqtSignal(bool)
    #: An article was processed (from the worker thread): ``(BatchItem, number, total)``.
    item_done = pyqtSignal(object, int, int)
    #: An article started loading (from the worker thread): ``(ArticleRef, number, total)``.
    item_started = pyqtSignal(object, int, int)
    #: Overall progress ``(done, total)``, for the tab counter.
    progress_changed = pyqtSignal(int, int)
    #: An article retry finished: the new :class:`BatchItem`.
    retried = pyqtSignal(object)

    def __init__(
        self,
        client: HabrClient,
        settings: QSettings,
        *,
        blocked: Callable[[], bool] = lambda: False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("page")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.client = client
        self.settings = settings
        self._blocked = blocked
        self._task: TaskThread | None = None
        self._cancel = threading.Event()
        self._pdf = MainThreadPdfPrinter(self)
        self.report: BatchReport | None = None
        self.show_error_dialogs = True
        self._options = ExportOptions()
        self._rows: list[LogRow] = []
        self._current_row: LogRow | None = None
        self._started_at = 0.0
        self._item_started = 0.0
        self._durations: list[float] = []
        self._kind: SourceKind | None = None
        self._source_texts: dict[str, str] = {}
        self.t = Bindings()
        #: Re-renders the progress line in the new language (``None`` means waiting for a message
        #: from the work).
        self._status_render: Callable[[], None] | None = None
        self._total = 0
        self._eta: float | None = None
        self._elapsed = 0.0
        self._build_ui()
        self._restore_settings()
        self.item_done.connect(self._on_item)
        self.item_started.connect(self._on_item_started)
        self._show_state("idle")
        self._update_header()
        self.update_state()

    # --- UI ----------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(16)
        left = QWidget()
        left.setFixedWidth(400)
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.setSpacing(12)
        # Cards scroll on a short screen; the "Start" / "Stop" buttons are always visible.
        self.settings_column = column = ScrollColumn(400)
        column.box.addWidget(self._build_source_card())
        column.box.addWidget(self._build_options_card(), 1)
        left_box.addWidget(column, 1)

        buttons = QGridLayout()
        buttons.setSpacing(10)
        self.start_button = QPushButton()
        self.start_button.setProperty("kind", "primary")
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.start)
        self.cancel_button = self.t.text(QPushButton(), "batch.stop")
        self.cancel_button.setProperty("kind", "danger")
        self.cancel_button.setMinimumHeight(44)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.t.tip(self.cancel_button, "batch.stop_tip")
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.start_button, 0, 0)
        buttons.addWidget(self.cancel_button, 0, 1)
        left_box.addLayout(buttons)
        root.addWidget(left)
        root.addWidget(self._build_progress_card(), 1)

    def _build_source_card(self) -> Card:
        self.source_box = card = Card(spacing=0)
        card.box.addWidget(self.t.text(label("", "section"), "batch.section.source"))
        card.box.addSpacing(8)
        self.source_control = SegmentedControl()
        self.t.name(self.source_control, "batch.source_name")
        for kind in SourceKind:
            button = self.source_control.add(kind, "")
            self.t.text(button, f"batch.kind.{kind.value}")
            self.t.tip(button, f"batch.kind_tip.{kind.value}")
        self.source_buttons = self.source_control.buttons
        self.source_control.changed.connect(self._on_kind_changed)
        card.box.addWidget(self.source_control)
        card.box.addSpacing(12)
        self.source_label = label("")
        card.box.addWidget(self.source_label)
        card.box.addSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.source_field = Field(prefix="@")
        self.source_edit = self.source_field.edit
        self.source_edit.setClearButtonEnabled(True)
        self.source_edit.textChanged.connect(self.update_state)
        self.source_edit.textChanged.connect(self._update_header)
        self.source_edit.textChanged.connect(self._update_source_hint)
        row.addWidget(self.source_field, 1)
        self.browse_file_button = self.t.text(QPushButton(), "batch.browse")
        self.browse_file_button.setMinimumHeight(40)
        self.browse_file_button.clicked.connect(self._browse_file)
        row.addWidget(self.browse_file_button)
        card.box.addLayout(row)
        card.box.addSpacing(6)
        self.source_hint = label("", "small", wrap=True)
        card.box.addWidget(self.source_hint)
        return card

    def _build_options_card(self) -> Card:
        self.options_box = card = Card(spacing=0)
        card.box.addWidget(self.t.text(label("", "section"), "batch.section.options"))
        card.box.addSpacing(8)
        card.box.addWidget(self.t.text(label(""), "batch.folder"))
        card.box.addSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        folder = Field("folder")
        self.folder_edit = folder.edit
        self.t.name(self.folder_edit, "batch.folder_name")
        self.folder_edit.textChanged.connect(self.update_state)
        row.addWidget(folder, 1)
        browse = self.t.text(QPushButton(), "batch.browse")
        browse.setMinimumHeight(40)
        browse.clicked.connect(self._browse_folder)
        row.addWidget(browse)
        card.box.addLayout(row)

        card.box.addSpacing(14)
        card.box.addWidget(self.t.text(label(""), "batch.format"))
        card.box.addSpacing(6)
        chips = QGridLayout()
        chips.setSpacing(6)
        self.format_group = QButtonGroup(self)
        self.format_buttons: dict[ExportFormat, Chip] = {}
        for index, fmt in enumerate(ExportFormat):
            chip = Chip(fmt.value.upper(), beta=fmt.beta)
            chip.setAccessibleName(fmt.label)
            chip.toggled.connect(lambda on: on and self._on_options_changed())
            self.format_group.addButton(chip)
            self.format_buttons[fmt] = chip
            chips.addWidget(chip, index // 3, index % 3)
        card.box.addLayout(chips)

        card.box.addSpacing(14)
        row = QHBoxLayout()
        row.setSpacing(12)
        images = QVBoxLayout()
        images.setSpacing(6)
        images.addWidget(self.t.text(label(""), "batch.images"))
        self.image_control = SegmentedControl()
        self.t.name(self.image_control, "batch.images")
        for mode in ImageMode:
            self.t.text(self.image_control.add(mode, ""), f"batch.images.{mode.value}")
        self.image_buttons = self.image_control.buttons
        self.image_control.changed.connect(self._on_options_changed)
        images.addWidget(self.image_control)
        row.addLayout(images, 1)
        comments = QHBoxLayout()
        comments.setSpacing(10)
        comments.addWidget(self.t.text(label(""), "comments.title"))
        self.comments_check = self.t.name(ToggleSwitch(), "comments.toggle")
        self.comments_check.toggled.connect(self._on_options_changed)
        comments.addWidget(self.comments_check)
        row.addLayout(comments)
        row.setAlignment(comments, Qt.AlignmentFlag.AlignBottom)
        card.box.addLayout(row)

        card.box.addSpacing(14)
        card.box.addWidget(self.t.text(label(""), "batch.limit"))
        card.box.addSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.limit_control = SegmentedControl()
        self.t.name(self.limit_control, "batch.limit")
        for key, text in (("all", "batch.limit.all"), ("limit", "batch.limit.max")):
            button = self.limit_control.add(key, "")
            self.t.text(button, text)
            self.t.tip(button, f"{text}_tip")
        row.addWidget(self.limit_control)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 100_000)
        self.limit_spin.setValue(10)
        self.t.tip(self.limit_spin, "batch.limit.spin_tip")
        self.limit_stepper = Stepper(self.limit_spin, tr("batch.limit.stepper"))
        self.t.call(lambda: self.limit_stepper.retranslate(tr("batch.limit.stepper")))
        row.addWidget(self.limit_stepper, 1)
        card.box.addLayout(row)
        # Entering a number selects "At most" by itself, so the user need not switch the mode first.
        self.limit_spin.valueChanged.connect(lambda _value: self.limit_control.set_value("limit"))
        self.limit_control.changed.connect(
            lambda mode: self.limit_stepper.setEnabled(mode == "limit")
        )
        self.limit_control.set_value("all")

        card.box.addSpacing(14)
        card.box.addWidget(self.t.text(label(""), "batch.delay"))
        card.box.addSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 30.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setDecimals(1)
        self.t.call(lambda: self.delay_spin.setSuffix(tr("batch.delay.suffix")))
        self.t.tip(self.delay_spin, "batch.delay.tip")
        self.delay_stepper = Stepper(self.delay_spin, tr("batch.delay.stepper"))
        self.t.call(lambda: self.delay_stepper.retranslate(tr("batch.delay.stepper")))
        self.delay_stepper.setFixedWidth(170)
        row.addWidget(self.delay_stepper)
        row.addWidget(self.t.text(label("", "small", wrap=True), "batch.delay.note"), 1)
        card.box.addLayout(row)
        card.box.addSpacing(12)
        card.box.addWidget(self.t.text(label("", "small", wrap=True), "batch.note"))
        card.box.addStretch(1)
        return card

    def _build_progress_card(self) -> Card:
        card = Card(margins=(0, 0, 0, 0), spacing=0)
        header = QFrame()
        header.setObjectName("cardHeader")
        box = QVBoxLayout(header)
        box.setContentsMargins(20, 16, 20, 14)
        box.setSpacing(12)
        titles = QHBoxLayout()
        titles.setSpacing(10)
        self.title_label = label("", "heading")
        self.subtitle_label = label("", "small")
        titles.addWidget(self.title_label)
        titles.addWidget(self.subtitle_label, 1, Qt.AlignmentFlag.AlignBottom)
        box.addLayout(titles)

        self.progress_box = QWidget()
        progress = QVBoxLayout(self.progress_box)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(8)
        counts = QHBoxLayout()
        counts.setSpacing(8)
        self.count_label = label("0", "big")
        self.of_label = label("", "muted")
        self.eta_label = label("", "small")
        counts.addWidget(self.count_label, 0, Qt.AlignmentFlag.AlignBottom)
        counts.addWidget(self.of_label, 0, Qt.AlignmentFlag.AlignBottom)
        counts.addStretch(1)
        counts.addWidget(self.eta_label, 0, Qt.AlignmentFlag.AlignBottom)
        progress.addLayout(counts)
        self.progress = QProgressBar()
        self.t.call(lambda: self.progress.setFormat(tr("batch.progress_format")))
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        progress.addWidget(self.progress)
        self.status_label = ElidedLabel("", "small")
        progress.addWidget(self.status_label)
        box.addWidget(self.progress_box)

        self.banner = QFrame()
        self.banner.setObjectName("banner")
        self.banner.setProperty("tone", "ok")
        banner = QHBoxLayout(self.banner)
        banner.setContentsMargins(14, 12, 14, 12)
        banner.setSpacing(14)
        self.banner_icon = IconLabel("check-dot", "ok", 34, bg="ok_soft")
        banner.addWidget(self.banner_icon)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.banner_title = label("", "strong")
        self.summary_label = label("", wrap=True)
        texts.addWidget(self.banner_title)
        texts.addWidget(self.summary_label)
        banner.addLayout(texts, 1)
        self.index_button = self.t.text(QPushButton(), "batch.open_index")
        self.index_button.setProperty("kind", "primary")
        self.index_button.setProperty("variant", "small")
        self.index_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.index_button.clicked.connect(self.open_index)
        banner.addWidget(self.index_button)
        self.folder_button = self.t.text(QPushButton(), "batch.folder")
        self.folder_button.setProperty("kind", "ghost")
        self.folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.folder_button.clicked.connect(self.open_folder)
        banner.addWidget(self.folder_button)
        box.addWidget(self.banner)
        card.box.addWidget(header)

        self.empty_state = QWidget()
        empty = QVBoxLayout(self.empty_state)
        empty.setSpacing(10)
        empty.addStretch(1)
        tile = QLabel()
        tile.setObjectName("emptyIcon")
        tile.setFixedSize(64, 64)
        inner = QVBoxLayout(tile)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.addWidget(
            IconLabel("bookmark", "accent", 30, stroke=1.8), 0, Qt.AlignmentFlag.AlignCenter
        )
        empty.addWidget(tile, 0, Qt.AlignmentFlag.AlignHCenter)
        title = self.t.text(label("", "title"), "batch.empty.title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.addWidget(title)
        hint = label("", "small", wrap=True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setFixedWidth(420)

        def update_hint() -> None:
            hint.setText(tr("batch.empty.hint"))
            hint.setMinimumHeight(
                hint.heightForWidth(420)
            )  # word wrap does not grow the height by itself in a centering layout

        self.t.call(update_hint)
        empty.addWidget(hint, 0, Qt.AlignmentFlag.AlignHCenter)
        empty.addStretch(2)
        card.box.addWidget(self.empty_state, 1)

        self.toolbar = QFrame()
        self.toolbar.setObjectName("cardHeader")
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(20, 10, 20, 10)
        bar.setSpacing(16)
        self.filter_control = SegmentedControl()
        self.t.name(self.filter_control, "batch.filter_name")
        self.filter_control.add("all", "")
        self.filter_control.add("errors", "")
        self.filter_control.set_value("all")
        self.filter_control.changed.connect(self._apply_filter)
        bar.addWidget(self.filter_control)
        bar.addStretch(1)
        self.saved_count_label = self._counter(bar, "ok")
        self.skipped_count_label = self._counter(bar, "skip")
        self.failed_count_label = self._counter(bar, "err")
        self.t.call(self._update_counters)
        card.box.addWidget(self.toolbar)

        self.log = QListWidget()
        self.log.setObjectName("log")
        self.log.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.log.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.log.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        card.box.addWidget(self.log, 1)

        # In the error state everything below the header is hidden: keep the header at the top.
        self.error_spacer = QWidget()
        card.box.addWidget(self.error_spacer, 1)
        return card

    @staticmethod
    def _counter(row: QHBoxLayout, tone: str) -> QLabel:
        pair = QHBoxLayout()
        pair.setSpacing(6)
        pair.addWidget(IconLabel("dot", tone, 10))
        text = label("")
        pair.addWidget(text)
        row.addLayout(pair)
        return text

    # --- State -------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None

    def selected_kind(self) -> SourceKind:
        return next(k for k, b in self.source_buttons.items() if b.isChecked())

    def source(self) -> BatchSource:
        return BatchSource(self.selected_kind(), self.source_edit.text().strip())

    def selected_format(self) -> ExportFormat:
        return next(f for f, b in self.format_buttons.items() if b.isChecked())

    def export_options(self) -> ExportOptions:
        return ExportOptions(
            format=self.selected_format(),
            image_mode=self.image_control.value() or ImageMode.EMBED,
            include_comments=self.comments_check.isChecked(),
        )

    def limit(self) -> int | None:
        """How many articles to save: ``None`` means all."""
        return self.limit_spin.value() if self.limit_control.value() == "limit" else None

    def log_rows(self) -> list[LogRow]:
        """Log rows from top to bottom (newest first)."""
        return [self.log.itemWidget(self.log.item(i)) for i in range(self.log.count())]  # type: ignore[misc]

    def update_state(self) -> None:
        ready = (
            bool(self.source_edit.text().strip() and self.folder_edit.text().strip())
            and not self._mismatch()
        )
        self.start_button.setEnabled(ready and not self.running and not self._blocked())
        self.cancel_button.setEnabled(
            self.running and self.report is None and not self._cancel.is_set()
        )
        for widget in (self.source_box, self.options_box):
            widget.setEnabled(not self.running)
        if self.running:
            self.start_button.setText(tr("batch.running"))
        else:
            self.start_button.setText(
                tr("batch.restart" if self.report is not None else "batch.start")
            )

    def _show_state(self, state: str) -> None:
        """Show a state.

        ``idle`` (not started yet), ``running``, ``done`` (result), ``error`` (could not start).
        """
        self.empty_state.setVisible(state == "idle")
        self.progress_box.setVisible(state in ("running", "error"))
        self.banner.setVisible(state == "done")
        self.toolbar.setVisible(state in ("running", "done"))
        self.log.setVisible(state in ("running", "done"))
        self.error_spacer.setVisible(state == "error")

    def _on_kind_changed(self, *_args: object) -> None:
        if not any(b.isChecked() for b in self.source_buttons.values()):
            return
        kind = self.selected_kind()
        # The login is shared by bookmarks and author articles, the file path is separate: do not
        # mix them up.
        if self._kind is not None and _source_slot(self._kind) != _source_slot(kind):
            self._source_texts[_source_slot(self._kind)] = self.source_edit.text()
            self.source_edit.setText(self._source_texts.get(_source_slot(kind), ""))
        self._kind = kind
        self._update_source_texts()
        self.source_field.prefix.setVisible(kind is not SourceKind.FILE)
        self.browse_file_button.setVisible(kind is SourceKind.FILE)
        self._update_header()

    def _mismatch(self) -> str:
        """Return an error text if the input does not fit the source.

        For example, a link instead of a file.
        """
        if not any(b.isChecked() for b in self.source_buttons.values()):
            return ""
        text = self.source_edit.text().strip()
        if not text:
            return ""
        if self.selected_kind() is SourceKind.FILE:
            if not _looks_like_file(text):
                return tr("batch.mismatch.not_file")
        elif _looks_like_file(text):
            return tr("batch.mismatch.file")
        return ""

    def _update_source_texts(self) -> None:
        if not any(b.isChecked() for b in self.source_buttons.values()):
            return
        kind = self.selected_kind().value
        self.source_edit.setPlaceholderText(tr(f"batch.placeholder.{kind}"))
        self.source_label.setText(tr(f"batch.source_label.{kind}"))
        self._update_source_hint()

    def _update_source_hint(self, *_args: object) -> None:
        if not any(b.isChecked() for b in self.source_buttons.values()):
            return
        problem = self._mismatch()
        self.source_hint.setText(problem or tr(f"batch.source_hint.{self.selected_kind().value}"))
        set_prop(self.source_hint, "tone", "err" if problem else None)

    def _on_options_changed(self, *_args: object) -> None:
        if not any(b.isChecked() for b in self.format_buttons.values()):
            return
        self.image_control.setEnabled(self.selected_format().embeds_images)
        self._update_header()

    def _update_header(self, *_args: object) -> None:
        if self.running or not any(b.isChecked() for b in self.source_buttons.values()):
            return
        source = self.source()
        if not source.value:
            title = tr("batch.header.default")
        elif source.kind is SourceKind.FILE:
            title = tr("batch.header.file", name=Path(source.value).name)
        else:
            try:
                alias = normalize_alias(source.value)
            except ValueError:
                alias = source.value
            title = tr(f"batch.header.{source.kind.value}", alias=alias)
        self.title_label.setText(title)
        if not any(b.isChecked() for b in self.format_buttons.values()):
            return
        options = self.export_options()
        if not options.format.embeds_images:
            images = tr("batch.sub.urls_only")
        else:
            images = tr(
                "batch.sub.embed" if options.image_mode is ImageMode.EMBED else "batch.sub.link"
            )
        comments = tr(
            "batch.sub.with_comments" if options.include_comments else "batch.sub.no_comments"
        )
        self.subtitle_label.setText(f"{options.format.value.upper()} · {images} · {comments}")

    def _restore_settings(self) -> None:
        s = self.settings
        kind = _enum(
            SourceKind, s.value("batch/source", SourceKind.BOOKMARKS.value), SourceKind.BOOKMARKS
        )
        texts = {slot: s.value(f"batch/{slot}", "", type=str) for slot in ("user", "file")}
        # Old versions stored one value for all sources: distribute it by meaning.
        legacy = s.value("batch/value", "", type=str).strip()
        if legacy and not any(texts.values()):
            texts["file" if _looks_like_file(legacy) else "user"] = legacy
        self._source_texts = texts
        self.source_buttons[kind].setChecked(True)
        self.source_edit.setText(texts[_source_slot(kind)])
        default_folder = str(Path.home() / "Documents" / "Habr")
        self.folder_edit.setText(s.value("batch/folder", default_folder, type=str))
        fmt = _enum(
            ExportFormat,
            s.value("batch/format", s.value("format", ExportFormat.PDF.value)),
            ExportFormat.PDF,
        )
        self.format_buttons[fmt].setChecked(True)
        mode = _enum(ImageMode, s.value("batch/image_mode", ImageMode.EMBED.value), ImageMode.EMBED)
        self.image_buttons[mode].setChecked(True)
        self.comments_check.setChecked(s.value("batch/comments", True, type=bool))
        self.delay_spin.setValue(s.value("batch/delay", DEFAULT_DELAY, type=float))

    def _store_settings(self) -> None:
        s, options = self.settings, self.export_options()
        kind = self.selected_kind()
        s.setValue("batch/source", kind.value)
        self._source_texts[_source_slot(kind)] = self.source_edit.text().strip()
        for slot in ("user", "file"):
            s.setValue(f"batch/{slot}", self._source_texts.get(slot, "").strip())
        s.remove("batch/value")
        s.setValue("batch/folder", self.folder_edit.text().strip())
        s.setValue("batch/format", options.format.value)
        s.setValue("batch/image_mode", options.image_mode.value)
        s.setValue("batch/comments", options.include_comments)
        s.setValue("batch/delay", self.delay_spin.value())

    def _browse_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("batch.file_dialog"), self.source_edit.text(), tr("batch.file_filter")
        )
        if path:
            self.source_edit.setText(path)

    def _browse_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, tr("batch.folder_name"), self.folder_edit.text()
        )
        if path:
            self.folder_edit.setText(path)

    # --- Export ------------------------------------------------------------------

    def start(self) -> None:
        if self.running or not self.start_button.isEnabled():
            return
        self._store_settings()
        self._update_header()
        source, options = self.source(), self.export_options()
        folder = Path(self.folder_edit.text().strip())
        limit = self.limit()
        delay = self.delay_spin.value()
        client, cancel = self.client, self._cancel
        print_pdf = self._pdf.print_html if options.format is ExportFormat.PDF else None

        def job(progress):  # type: ignore[no-untyped-def]
            return run_batch(
                source,
                folder,
                options,
                client=client,
                delay=delay,
                limit=limit,
                progress=progress,
                cancelled=cancel.is_set,
                print_pdf=print_pdf,
                on_item=self.item_done.emit,
                on_start=self.item_started.emit,
            )

        cancel.clear()
        self.report = None
        self._options = options
        self.log.clear()
        self._rows = []
        self._current_row = None
        self._durations = []
        self._started_at = time.monotonic()
        self.count_label.setText("0")
        self._total, self._eta = 0, None
        self.of_label.clear()
        self.eta_label.clear()
        self._update_counters()
        self.progress.setRange(0, 0)
        self._run(job, self._on_finished)
        self._status_render = None
        self.status_label.setText(tr("status.preparing"))
        set_prop(self.status_label, "tone", None)
        self._show_state("running")

    def _run(self, job, on_success) -> None:  # type: ignore[no-untyped-def]
        self._task = TaskThread(job, self)
        self._task.progress.connect(self._on_progress)
        self._task.succeeded.connect(on_success)
        self._task.failed.connect(self._on_failed)
        self._task.finished.connect(self._on_task_finished)
        self.update_state()
        self.running_changed.emit(True)
        self._task.start()

    def cancel(self) -> None:
        if not self.running or self.report is not None:
            return
        self._cancel.set()
        self.status_label.setText(tr("batch.cancelling"))
        self.update_state()

    def _on_progress(self, message: str, done: int, total: int) -> None:
        self.status_label.setText(message)
        if self._current_row is not None and " · " in message:
            self._current_row.set_meta(message.rsplit(" · ", 1)[-1])
        if self.report is not None:
            return  # retrying one article: leave the overall progress alone
        # total == 0 arrives only while the article list is being collected: their number is not
        # known yet.
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.count_label.setText(str(done))
            self._total = total
            self.of_label.setText(tr("batch.of", total=total))
            self.progress_changed.emit(done, total)
        else:
            self.progress.setRange(0, 0)

    def _insert_row(self) -> LogRow:
        row = LogRow()
        row.action.connect(self._on_row_action)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, row.height()))
        self.log.insertItem(0, item)
        self.log.setItemWidget(item, row)
        self._rows.insert(0, row)
        return row

    def _on_item_started(self, ref: ArticleRef, _number: int, _total: int) -> None:
        self._current_row = self._insert_row()
        self._current_row.set_running(ref)
        self._item_started = time.monotonic()
        self._apply_filter()

    def _on_item(self, item: BatchItem, number: int, total: int) -> None:
        row = (
            self._current_row
            if self._current_row is not None and self._current_row.ref is item.ref
            else None
        )
        row = row or self._insert_row()
        row.set_item(item)
        self._current_row = None
        if item.status is not ItemStatus.SKIPPED and self._item_started:
            self._durations.append(time.monotonic() - self._item_started)
            self._item_started = 0.0
        left = total - number
        if self._durations and left:
            recent = self._durations[-_ETA_WINDOW:]
            self._eta = sum(recent) / len(recent) * left
        else:
            self._eta = None
        self._update_eta()
        self._update_counters()
        self._apply_filter()

    def _counts(self) -> dict[str, int]:
        counts = {"saved": 0, "skipped": 0, "failed": 0}
        for row in self._rows:
            if row.status in counts:
                counts[row.status] += 1
        return counts

    def _update_eta(self) -> None:
        if self._eta is None:
            self.eta_label.clear()
        else:
            self.eta_label.setText(tr("batch.eta", eta=format_eta(self._eta)))

    def _update_counters(self) -> None:
        counts = self._counts()
        self.saved_count_label.setText(tr("batch.count.saved", n=counts["saved"]))
        self.skipped_count_label.setText(tr("batch.count.skipped", n=counts["skipped"]))
        self.failed_count_label.setText(tr("batch.count.failed", n=counts["failed"]))
        self.filter_control.buttons["all"].setText(tr("batch.filter.all", n=sum(counts.values())))
        self.filter_control.buttons["errors"].setText(tr("batch.filter.errors", n=counts["failed"]))

    def _apply_filter(self, *_args: object) -> None:
        errors_only = self.filter_control.value() == "errors"
        for index in range(self.log.count()):
            row = self.log.itemWidget(self.log.item(index))
            self.log.item(index).setHidden(errors_only and getattr(row, "status", "") != "failed")

    def _on_finished(self, report: BatchReport) -> None:
        self.report = report
        self.progress.setRange(0, max(report.total, 1))
        self.progress.setValue(len(report.items))
        self._status_render = lambda: self.status_label.setText(
            tr("batch.cancelled" if report.cancelled else "batch.done")
        )
        self._status_render()
        self._elapsed = time.monotonic() - self._started_at
        self._update_banner_title()
        tone = "neutral" if report.cancelled else "ok"
        set_prop(self.banner, "tone", tone)
        set_prop(self.banner_title, "tone", None if report.cancelled else "ok")
        self.banner_icon.set_icon(
            "check-dot" if not report.cancelled else "skip", "ok" if tone == "ok" else "muted"
        )
        self.summary_label.setText(report.localized_summary())
        self.index_button.setVisible(report.index_path is not None)
        self._show_state("done")
        self.finished.emit(report)

    def _update_banner_title(self) -> None:
        if self.report is None:
            return
        key = "batch.stopped_after" if self.report.cancelled else "batch.done_in"
        self.banner_title.setText(tr(key, elapsed=format_duration(self._elapsed)))

    def _on_failed(self, message: str) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._status_render = lambda: self.status_label.setText(tr("status.error", message=message))
        self._status_render()
        set_prop(self.status_label, "tone", "err")
        if self.report is None:
            self._show_state("error")
        self.error.emit(message)
        if self.show_error_dialogs:
            QMessageBox.critical(self, tr("dialog.error"), message)

    def _on_task_finished(self) -> None:
        if self._task is not None:
            self._task.deleteLater()
        self._task = None
        self._current_row = None
        self.update_state()
        self.running_changed.emit(False)

    # --- Log: open and retry -----------------------------------------------------

    def _on_row_action(self, row: LogRow) -> None:
        if row.item is None:
            return
        if row.item.status is ItemStatus.FAILED:
            self.retry(row)
        elif row.item.path is not None:
            open_path(row.item.path)

    def retry(self, row: LogRow) -> None:
        """Save a failed article again and update its log row and index.html."""
        report, item = self.report, row.item
        if self.running or self._blocked() or report is None or item is None:
            return
        options, client = self._options, self.client
        print_pdf = self._pdf.print_html if options.format is ExportFormat.PDF else None
        row.set_running(item.ref, tr("batch.row.retrying"))
        self._current_row = row

        def job(progress):  # type: ignore[no-untyped-def]
            return retry_item(
                report, item, options, client=client, progress=progress, print_pdf=print_pdf
            )

        def done(result: BatchItem) -> None:
            row.set_item(result)
            self._update_counters()
            self._apply_filter()
            self.summary_label.setText(report.localized_summary())
            self.retried.emit(result)

        self._run(job, done)

    def retranslate(self) -> None:
        """Translate the tab into the current UI language.

        See :meth:`MainWindow.apply_language`.
        """
        self.t.refresh()
        self._update_source_texts()
        self._update_header()
        self.update_state()
        if self._total:
            self.of_label.setText(tr("batch.of", total=self._total))
        self._update_eta()
        if self.report is not None:
            self._update_banner_title()
            self.summary_label.setText(self.report.localized_summary())
        for row in self._rows:
            if row.item is not None and row.status != "running":
                row.set_item(row.item)
        # While an export is running, its next step will send a message in the new language.
        if not self.running and self._status_render is not None:
            self._status_render()

    def open_index(self) -> None:
        if self.report is not None and self.report.index_path is not None:
            open_path(self.report.index_path)

    def open_folder(self) -> None:
        if self.report is not None:
            open_path(self.report.folder)

    def shutdown(self, timeout_ms: int = 5000) -> None:
        """Stop the export when the window closes."""
        if self._task is None:
            return
        self._cancel.set()
        self._pdf.abort()
        self._task.wait(timeout_ms)


def _looks_like_file(text: str) -> bool:
    """Tell whether the text is a file path rather than a login or a Habr link.

    Examples: ``links.txt``, ``C:/data/links``, ``~/links``.
    """
    text = text.strip()
    lowered = text.lower()
    if lowered.startswith(_LINK_STARTS) or any(start.startswith(lowered) for start in _LINK_STARTS):
        return False  # a link, or one still being typed (``htt``, ``https:``)
    if re.match(r"[a-z][a-z0-9+.-]+:", lowered):
        return False  # some other scheme; a drive letter (``C:``) is a single character
    return lowered.endswith(".txt") or any(sep in text for sep in ("\\", "/")) or ":" in text


_LINK_STARTS = ("http://", "https://", "www.", "habr.com", "habrahabr.ru", "@")


def _source_slot(kind: SourceKind) -> str:
    return "file" if kind is SourceKind.FILE else "user"


def _enum(enum_cls, value, default):  # type: ignore[no-untyped-def]
    try:
        return enum_cls(value)
    except ValueError:
        return default
