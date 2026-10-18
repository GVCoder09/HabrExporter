import threading
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings

from conftest import listing_url
from habr_exporter.batch import BatchSource, SourceKind
from habr_exporter.exporters import ExportFormat, ExportOptions
from habr_exporter.exporters.pdf import PdfExportError
from habr_exporter.gui.batch_panel import MainThreadPdfPrinter
from habr_exporter.gui.main_window import MainWindow
from habr_exporter.gui.workers import TaskThread
from habr_exporter.images import ImageMode


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def window(qtbot, client, settings):
    win = MainWindow(client, with_preview=False, settings=settings)
    win.show_error_dialogs = False
    qtbot.addWidget(win)
    return win


def _load(qtbot, window, url="https://habr.com/ru/companies/acme/articles/1000001/"):
    window.url_edit.setText(url)
    with qtbot.waitSignal(window.loaded, timeout=10_000):
        window.load_button.click()
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)


def test_format_buttons_order_and_beta(window):
    assert list(window.format_buttons) == [
        ExportFormat.PDF,
        ExportFormat.HTML,
        ExportFormat.DOCX,
        ExportFormat.EPUB,
        ExportFormat.FB2,
        ExportFormat.MARKDOWN,
        ExportFormat.JSON,
    ]
    labels = [b.label for b in window.format_buttons.values()]
    assert labels == [
        "PDF",
        "HTML",
        "DOCX (beta)",
        "EPUB (beta)",
        "FB2 (beta)",
        "MD (beta)",
        "JSON",
    ]
    for fmt in (ExportFormat.DOCX, ExportFormat.EPUB, ExportFormat.FB2, ExportFormat.MARKDOWN):
        assert "Бета" in window.format_buttons[fmt].toolTip()
    for fmt in (ExportFormat.PDF, ExportFormat.HTML, ExportFormat.JSON):
        assert "Бета" not in window.format_buttons[fmt].toolTip()
    assert list(window.batch_panel.format_buttons) == list(ExportFormat)
    assert [b.label for b in window.batch_panel.format_buttons.values()] == labels


def test_legacy_mhtml_setting_falls_back_to_pdf(qtbot, client, settings):
    settings.setValue("format", "mhtml")
    win = MainWindow(client, with_preview=False, settings=settings)
    qtbot.addWidget(win)
    assert win.selected_format() is ExportFormat.PDF


def test_initial_state(window):
    assert not window.load_button.isEnabled()
    assert not window.save_button.isEnabled()
    assert window.selected_format() is ExportFormat.PDF
    assert window.selected_image_mode() is ImageMode.EMBED
    assert window.comments_check.isChecked()


def test_url_validation_toggles_load_button(window):
    window.url_edit.setText("https://example.com/")
    assert not window.load_button.isEnabled()
    window.url_edit.setText("https://habr.com/ru/articles/1000001/")
    assert window.load_button.isEnabled()


def test_load_shows_article_info(qtbot, window):
    _load(qtbot, window)
    assert window.article is not None
    card = window.article_card
    assert card.article_title.startswith("Тестовая статья")
    assert card.title_label.toolTip() == card.article_title
    assert "ACME" in card.byline.text()
    assert card.comments_label.text() == "7"
    assert card.score_label.text() == "+17"
    assert card.reading_label.text() == "7 мин"
    assert not card.content.isHidden()
    assert card.empty.isHidden()
    assert window.comments_info.text() == "7 комментариев, деревом"
    assert window.article_badge.text() == "Статья 1000001"
    assert not window.article_badge.isHidden()
    assert not window.habr_link.isHidden()
    assert window.save_button.isEnabled()
    assert "Загружено" in window.status_label.text()
    window.url_edit.setText("https://habr.com/ru/articles/42/")
    assert window.article_badge.isHidden()  # the badge is only for a loaded article


def test_load_error_is_reported(qtbot, window):
    window.url_edit.setText("https://habr.com/ru/articles/404/")
    with qtbot.waitSignal(window.error, timeout=10_000) as blocker:
        window.load()
    assert "Не найдено" in blocker.args[0]
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)
    assert window.article is None
    assert "Ошибка" in window.status_label.text()


@pytest.mark.parametrize(
    ("fmt", "mode"),
    [
        (ExportFormat.DOCX, ImageMode.EMBED),
        (ExportFormat.HTML, ImageMode.LINK),
        (ExportFormat.PDF, ImageMode.EMBED),
        (ExportFormat.EPUB, ImageMode.EMBED),
        (ExportFormat.FB2, ImageMode.EMBED),
        (ExportFormat.JSON, ImageMode.LINK),
    ],
)
def test_export(qtbot, window, tmp_path, fmt, mode):
    _load(qtbot, window)
    target = tmp_path / f"out{fmt.extension}"
    with qtbot.waitSignal(window.saved, timeout=60_000) as blocker:
        window.export_to(target, ExportOptions(fmt, mode))
    assert blocker.args[0] == str(target)
    assert target.stat().st_size > 1000
    assert "Сохранено" in window.status_label.text()
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)
    assert window.save_button.isEnabled()
    if mode is ImageMode.EMBED:
        assert window.images is not None  # the image cache is reused on the next save


def test_settings_roundtrip(qtbot, client, settings):
    first = MainWindow(client, with_preview=False, settings=settings)
    qtbot.addWidget(first)
    first.format_buttons[ExportFormat.DOCX].setChecked(True)
    first.image_buttons[ImageMode.LINK].setChecked(True)
    first.comments_check.setChecked(False)
    first._store_settings()
    second = MainWindow(client, with_preview=False, settings=settings)
    qtbot.addWidget(second)
    assert second.export_options() == ExportOptions(
        ExportFormat.DOCX, ImageMode.LINK, include_comments=False
    )


def test_bad_settings_fall_back_to_defaults(qtbot, client, settings):
    settings.setValue("format", "xls")
    settings.setValue("image_mode", "???")
    win = MainWindow(client, with_preview=False, settings=settings)
    qtbot.addWidget(win)
    assert win.selected_format() is ExportFormat.PDF
    assert win.selected_image_mode() is ImageMode.EMBED


def test_preview_is_rendered(qtbot, client, settings):
    win = MainWindow(client, with_preview=True, settings=settings)
    win.show_error_dialogs = False
    qtbot.addWidget(win)
    assert win.preview is not None
    with qtbot.waitSignal(win.preview.loadFinished, timeout=20_000):
        _load(qtbot, win)


def test_task_thread_signals(qtbot):
    ok = TaskThread(lambda progress: (progress("шаг", 1, 2), 42)[1])
    with qtbot.waitSignals([ok.succeeded, ok.progress], timeout=5000) as blocker:
        ok.start()
    assert blocker.all_signals_and_args[0].args in ((42,), ("шаг", 1, 2))
    ok.wait()

    def boom(progress):
        raise ValueError("сломалось")

    bad = TaskThread(boom)
    with qtbot.waitSignal(bad.failed, timeout=5000) as blocker:
        bad.start()
    assert blocker.args == ["сломалось"]
    bad.wait()


# --- "Batch" tab ---------------------------------------------------------------------


def _setup_batch(window, folder, fmt=ExportFormat.HTML, mode=ImageMode.LINK, source="alice"):
    panel = window.batch_panel
    panel.source_buttons[SourceKind.BOOKMARKS].setChecked(True)
    panel.source_edit.setText(source)
    panel.folder_edit.setText(str(folder))
    panel.format_buttons[fmt].setChecked(True)
    panel.image_buttons[mode].setChecked(True)
    panel.delay_spin.setValue(0)
    return panel


def test_batch_tab_initial_state(window):
    assert window.tabs.count() == 2
    assert window.tabs.tabText(1) == "Несколько"
    window.nav.set_value("batch")
    assert window.tabs.currentIndex() == 1
    window.nav.set_value("single")
    assert window.tabs.currentIndex() == 0
    panel = window.batch_panel
    assert not panel.empty_state.isHidden()
    assert panel.log.isHidden()
    assert panel.title_label.text() == "Пакетная выгрузка"
    assert panel.selected_kind() is SourceKind.BOOKMARKS
    assert panel.delay_spin.value() == 1.0
    assert panel.limit_control.value() == "all"
    assert panel.limit() is None
    assert not panel.limit_stepper.isEnabled()
    assert not panel.start_button.isEnabled()  # login not entered
    assert not panel.cancel_button.isEnabled()
    panel.source_buttons[SourceKind.FILE].setChecked(True)
    assert "файл" in panel.source_edit.placeholderText()
    assert not panel.browse_file_button.isHidden()
    panel.source_buttons[SourceKind.AUTHOR].setChecked(True)
    assert panel.browse_file_button.isHidden()


def test_batch_html_bookmarks(qtbot, window, batch_session, tmp_path):
    panel = _setup_batch(window, tmp_path / "backup")
    assert panel.start_button.isEnabled()
    with qtbot.waitSignal(panel.finished, timeout=30_000) as blocker:
        panel.start_button.click()
        assert window.busy
        assert not window.load_button.isEnabled()
    report = blocker.args[0]
    assert (report.saved, report.skipped, report.failed) == (3, 0, 0)
    qtbot.waitUntil(lambda: not panel.running, timeout=5000)
    assert panel.progress.value() == panel.progress.maximum() == 3
    assert panel.progress.text() == "3 из 3"
    assert [row.status for row in panel.log_rows()] == ["saved"] * 3
    assert panel.log_rows()[0].ref.id == "1000003"  # newest first
    assert panel.summary_label.text() == "Сохранено 3 статьи"
    assert panel.banner_title.text().startswith("Готово за ")
    assert not panel.banner.isHidden()
    assert not panel.index_button.isHidden()
    assert panel.saved_count_label.text() == "Сохранено 3"
    assert (tmp_path / "backup" / "index.html").exists()
    assert panel.start_button.isEnabled()
    assert panel.start_button.text() == "Начать снова"
    assert not window.busy

    # Retry: everything is already there.
    with qtbot.waitSignal(panel.finished, timeout=30_000) as blocker:
        panel.start()
    assert blocker.args[0].skipped == 3
    assert [row.status for row in panel.log_rows()] == ["skipped"] * 3
    assert panel.summary_label.text() == "Сохранено 0 статей, 3 уже были в папке"


def test_batch_pdf_prints_in_main_thread(qtbot, window, batch_session, tmp_path):
    panel = _setup_batch(window, tmp_path, fmt=ExportFormat.PDF, mode=ImageMode.EMBED)
    panel.limit_spin.setValue(2)
    with qtbot.waitSignal(panel.finished, timeout=120_000) as blocker:
        panel.start()
    assert blocker.args[0].saved == 2
    pdfs = sorted(tmp_path.glob("*.pdf"))
    assert [p.name[:7] for p in pdfs] == ["1000001", "1000002"]
    assert all(p.read_bytes().startswith(b"%PDF") for p in pdfs)
    qtbot.waitUntil(lambda: not panel.running, timeout=5000)


def test_batch_error_is_reported(qtbot, window, batch_session, tmp_path):
    panel = _setup_batch(window, tmp_path, source="nobody")
    with qtbot.waitSignal(panel.error, timeout=10_000) as blocker:
        panel.start()
    assert "@nobody не найден" in blocker.args[0]
    qtbot.waitUntil(lambda: not panel.running, timeout=5000)
    assert "Ошибка" in panel.status_label.text()
    assert panel.report is None


def test_batch_cancel(qtbot, window, batch_session, tmp_path):
    gate = threading.Event()
    first_page = batch_session.routes[listing_url("alice", 1, bookmarks=True)]

    def slow_listing():
        gate.wait(10)
        return first_page

    batch_session.routes[listing_url("alice", 1, bookmarks=True)] = slow_listing
    panel = _setup_batch(window, tmp_path)
    panel.start()
    assert panel.cancel_button.isEnabled()
    with qtbot.waitSignal(panel.finished, timeout=30_000) as blocker:
        panel.cancel_button.click()
        assert not panel.cancel_button.isEnabled()
        assert "Отмена" in panel.status_label.text()
        gate.set()
    report = blocker.args[0]
    assert report.cancelled
    assert report.items == []
    assert "отменена" in panel.status_label.text()


def test_batch_shutdown_releases_worker(qtbot, client, settings, batch_session, tmp_path):
    gate = threading.Event()
    first_page = batch_session.routes[listing_url("alice", 1, bookmarks=True)]
    batch_session.routes[listing_url("alice", 1, bookmarks=True)] = lambda: (
        gate.wait(10),
        first_page,
    )[1]
    win = MainWindow(client, with_preview=False, settings=settings)
    win.show_error_dialogs = False
    qtbot.addWidget(win)
    panel = _setup_batch(win, tmp_path)
    panel.start()
    gate.set()
    win.close()
    assert not panel._task or panel._task.isFinished()


def test_batch_settings_roundtrip(qtbot, client, settings, tmp_path):
    first = MainWindow(client, with_preview=False, settings=settings)
    qtbot.addWidget(first)
    panel = first.batch_panel
    panel.source_buttons[SourceKind.AUTHOR].setChecked(True)
    panel.source_edit.setText("bob")
    panel.folder_edit.setText(str(tmp_path))
    panel.format_buttons[ExportFormat.DOCX].setChecked(True)
    panel.image_buttons[ImageMode.LINK].setChecked(True)
    panel.comments_check.setChecked(False)
    panel.delay_spin.setValue(2.5)
    panel._store_settings()
    second = MainWindow(client, with_preview=False, settings=settings).batch_panel
    qtbot.addWidget(second)
    assert second.source() == BatchSource(SourceKind.AUTHOR, "bob")
    assert second.folder_edit.text() == str(tmp_path)
    assert second.export_options() == ExportOptions(
        ExportFormat.DOCX, ImageMode.LINK, include_comments=False
    )
    assert second.delay_spin.value() == 2.5


def test_pdf_bridge_abort(qtbot):
    bridge = MainThreadPdfPrinter()
    bridge.abort()
    with pytest.raises(PdfExportError, match="прервана"):
        bridge.print_html("<p>x</p>", Path("x.pdf"))


def test_pdf_bridge_reports_failure(qtbot, tmp_path):
    bridge = MainThreadPdfPrinter()
    outcome = {}

    def worker():
        try:
            bridge.print_html("<p>x</p>", tmp_path / "missing-dir" / "deeper" / "x.pdf")
        except PdfExportError as exc:
            outcome["error"] = str(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    qtbot.waitUntil(lambda: not thread.is_alive(), timeout=60_000)
    assert "PDF" in outcome["error"]
