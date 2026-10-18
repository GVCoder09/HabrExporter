"""UI: themes, widgets, clipboard, "open / show in folder", batch export log."""

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PyQt6.QtCore import QEvent, QSettings, Qt
from PyQt6.QtGui import QFocusEvent, QFontMetrics, QGuiApplication
from PyQt6.QtWidgets import QApplication, QSpinBox, QWidget

from conftest import API, FakeResponse
from habr_exporter.batch import ArticleRef, BatchItem, BatchSource, ItemStatus, SourceKind
from habr_exporter.exporters import ExportFormat
from habr_exporter.gui import system, theme
from habr_exporter.gui.main_window import ArticleCard, MainWindow
from habr_exporter.gui.theme import ThemeMode
from habr_exporter.gui.widgets import (
    ElidedLabel,
    Field,
    FormatTile,
    IconLabel,
    SegmentedControl,
    Stepper,
    ToggleSwitch,
    elide_lines,
    set_prop,
)
from habr_exporter.images import ImageMode
from habr_exporter.models import Article, Statistics, User

URL = "https://habr.com/ru/companies/acme/articles/1000001/"


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def window(qtbot, client, settings):
    win = MainWindow(client, with_preview=False, settings=settings)
    win.show_error_dialogs = False
    qtbot.addWidget(win)
    return win


def _load(qtbot, window, url=URL):
    window.url_edit.setText(url)
    with qtbot.waitSignal(window.loaded, timeout=10_000):
        window.load_button.click()
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)


# --- Theme -----------------------------------------------------------------------------


def test_theme_mode_cycle_and_labels():
    assert [m.next() for m in ThemeMode] == [ThemeMode.LIGHT, ThemeMode.DARK, ThemeMode.SYSTEM]
    assert ThemeMode.SYSTEM.label == "Как в системе"
    assert theme.resolve(ThemeMode.LIGHT) is theme.LIGHT
    assert theme.resolve(ThemeMode.DARK) is theme.DARK


def test_stylesheet_and_palette(qapp):
    for colors in (theme.LIGHT, theme.DARK):
        qss = theme.stylesheet(colors)
        assert "$" not in qss
        assert colors.accent in qss
        assert theme.palette(colors).window().color().name() == colors.window


def test_icon_pixmaps(qapp):
    for name in theme.ICONS:
        pixmap = theme.icon_pixmap(name, "#123456", 16, 2.0, bg="#ffffff")
        assert (pixmap.width(), pixmap.height()) == (32, 32)
        assert pixmap.devicePixelRatio() == 2.0
    logo = theme.logo_pixmap(theme.DARK, 32)
    assert not logo.toImage().isNull()


def test_theme_follows_system_and_is_stored(window, settings, monkeypatch):
    assert window.theme_mode is ThemeMode.SYSTEM
    assert window.theme_button.text() == "Как в системе"
    assert theme.of(window.save_button) is theme.LIGHT  # offscreen platform: light system theme
    monkeypatch.setattr(theme, "system_is_dark", lambda: True)
    window._on_system_scheme()
    assert window.property("habrTheme") == "dark"
    assert theme.of(window.comments_check) is theme.DARK

    window.cycle_theme()
    assert window.theme_mode is ThemeMode.LIGHT
    assert window.property("habrTheme") == "light"
    assert settings.value("theme") == "light"
    window._on_system_scheme()  # an explicit user choice is not overridden by the system
    assert window.property("habrTheme") == "light"
    window.cycle_theme()
    assert window.theme_button.text() == "Тёмная"
    assert theme.DARK.window in window.styleSheet()


def test_theme_restored_from_settings(qtbot, client, settings):
    settings.setValue("theme", "dark")
    win = MainWindow(client, with_preview=False, settings=settings)
    qtbot.addWidget(win)
    assert win.theme_mode is ThemeMode.DARK
    assert win.property("habrTheme") == "dark"
    settings.setValue("theme", "rainbow")
    assert MainWindow(client, with_preview=False, settings=settings).theme_mode is ThemeMode.SYSTEM


def test_theme_of_widget_outside_window(qapp):
    theme.apply(QWidget(), theme.DARK)
    assert theme.of(QWidget()) is theme.DARK  # without a themed window, the last applied theme
    theme.apply(QWidget(), theme.LIGHT)


# --- Widgets ---------------------------------------------------------------------------


def test_elide_lines(qapp):
    metrics = QFontMetrics(QWidget().font())
    text = (
        "Когда Docker Compose уже мало, а Kubernetes еще слишком много: "
        "знакомство с Uncloud. Часть 1"
    )
    two = elide_lines(text, metrics, 200, 2)
    assert two.count("\n") == 1
    assert two.endswith("…")
    assert elide_lines("коротко", metrics, 200) == "коротко"
    assert elide_lines("", metrics, 200) == ""
    assert elide_lines(text, metrics, 0) == text
    assert "\n" not in elide_lines(text, metrics, 100, 1)


def test_segmented_control(qtbot):
    control = SegmentedControl()
    qtbot.addWidget(control)
    control.add("a", "А")
    control.add("b", "Б", "подсказка")
    assert control.value() is None
    with qtbot.waitSignal(control.changed) as blocker:
        control.set_value("b")
    assert blocker.args == ["b"]
    assert control.value() == "b"
    assert control.buttons["b"].toolTip() == "подсказка"


def test_toggle_switch_and_labels_paint(qtbot):
    switch = ToggleSwitch("Комментарии")
    qtbot.addWidget(switch)
    assert switch.accessibleName() == "Комментарии"
    assert switch.size().width() == 42
    for checked, enabled in ((True, True), (False, True), (True, False)):
        switch.setChecked(checked)
        switch.setEnabled(enabled)
        assert not switch.grab().isNull()
    switch.setEnabled(True)
    qtbot.mouseClick(switch, Qt.MouseButton.LeftButton)
    assert not switch.isChecked()

    elided = ElidedLabel("очень длинная строка, которая не помещается", "small")
    qtbot.addWidget(elided)
    elided.resize(60, 20)
    assert not elided.grab().isNull()
    assert elided.text().startswith("очень")


def test_format_tile_and_icon_label(qtbot):
    tile = FormatTile("DOCX", "Word, LibreOffice", beta=True)
    qtbot.addWidget(tile)
    assert tile.label == "DOCX (beta)"
    assert tile.sizeHint().height() >= 28
    assert not tile.minimumSizeHint().isEmpty()
    tile.show()
    assert tile.check.isHidden()
    tile.setChecked(True)
    assert not tile.check.isHidden()

    icon = IconLabel("check", "ok", 14)
    qtbot.addWidget(icon)
    icon.set_icon("cross", "err")
    assert icon.icon_name == "cross"
    assert not icon.pixmap().isNull()


def test_field_focus_property(qtbot):
    field = Field("link", large=True)
    qtbot.addWidget(field)
    QApplication.sendEvent(field.edit, QFocusEvent(QEvent.Type.FocusIn))
    assert field.property("focused") is True
    QApplication.sendEvent(field.edit, QFocusEvent(QEvent.Type.FocusOut))
    assert field.property("focused") is False
    set_prop(field, "focused", False)  # same value: no extra repaint
    assert field.property("focused") is False


def test_article_card_without_company(qtbot):
    card = ArticleCard()
    qtbot.addWidget(card)
    article = Article(
        id="1",
        url="https://habr.com/ru/articles/1/",
        title="Заголовок",
        body_html="",
        author=User(alias="bob", fullname="Боб"),
        published=None,
        statistics=Statistics(score=-3, views=1290),
    )
    card.show_article(article)
    assert card.byline.text() == "Боб · @bob"
    assert card.initials.text() == "Б"
    assert card.score_label.text() == "-3"
    assert card.score_label.property("tone") == "err"
    assert card.score_icon.icon_name == "down"
    assert card.views_label.text() == "1.3K"
    assert card.reading_label.text() == "—"
    article.author = User(alias="anon")
    card.show_article(article)
    assert card.byline.text() == "@anon"


# --- Single article --------------------------------------------------------------------


def test_format_tiles_update_image_choice_and_button(window):
    assert window.save_button.text() == "Сохранить PDF…"
    assert window.image_control.isEnabled()
    window.format_buttons[ExportFormat.JSON].setChecked(True)
    assert window.save_button.text() == "Сохранить JSON…"
    assert not window.image_control.isEnabled()
    assert "JSON" in window.image_hint.text()
    window.format_buttons[ExportFormat.HTML].setChecked(True)
    window.image_buttons[ImageMode.LINK].setChecked(True)
    assert window.image_control.isEnabled()
    assert "интернет" in window.image_hint.text()


def test_clipboard_banner_paste_and_dismiss(qtbot, window):
    clipboard = QGuiApplication.clipboard()
    clipboard.setText("просто текст")
    window.check_clipboard()
    assert window.clipboard_banner.isHidden()

    clipboard.setText(URL)
    window.check_clipboard()
    assert not window.clipboard_banner.isHidden()
    assert "habr.com/ru/companies/acme/articles/1000001/" in window.clipboard_label.text()
    with qtbot.waitSignal(window.loaded, timeout=10_000):
        window.paste_button.click()
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)
    assert window.url_edit.text() == URL
    assert window.clipboard_banner.isHidden()

    window.url_edit.clear()
    window.check_clipboard()
    assert not window.clipboard_banner.isHidden()
    window.dismiss_clipboard()
    window.check_clipboard()
    assert window.clipboard_banner.isHidden()  # a dismissed link is not offered again
    window._clipboard_url = None
    window.paste_from_clipboard()  # nothing to paste, nothing happens
    clipboard.clear()


def test_saved_file_buttons(qtbot, window, tmp_path, monkeypatch):
    opened, revealed = [], []
    monkeypatch.setattr("habr_exporter.gui.main_window.open_path", opened.append)
    monkeypatch.setattr("habr_exporter.gui.main_window.reveal_path", revealed.append)
    window.open_saved()
    window.reveal_saved()
    assert opened == revealed == []
    _load(qtbot, window)
    target = tmp_path / "a.json"
    with qtbot.waitSignal(window.saved, timeout=10_000):
        window.export_to(target, window.export_options().__class__(ExportFormat.JSON))
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)
    assert not window.open_file_button.isHidden()
    assert not window.reveal_button.isHidden()
    assert window.status_label.toolTip() == str(target)
    window.open_file_button.click()
    window.reveal_button.click()
    assert opened == revealed == [str(target)]


def test_focus_shortcut_and_habr_link(qtbot, window, monkeypatch):
    window.nav.set_value("batch")
    window._focus_url()
    assert window.tabs.currentIndex() == 0
    opened = []
    monkeypatch.setattr("habr_exporter.gui.main_window.QDesktopServices.openUrl", opened.append)
    window._open_on_habr()
    assert opened == []
    _load(qtbot, window)
    window.habr_link.click()
    assert opened[0].toString() == URL


def test_error_status_tone(qtbot, window):
    window.url_edit.setText("https://habr.com/ru/articles/404/")
    with qtbot.waitSignal(window.error, timeout=10_000):
        window.load()
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)
    assert window.status_label.property("tone") == "err"


# --- Batch -----------------------------------------------------------------------------


def _setup_batch(window, folder):
    panel = window.batch_panel
    panel.source_edit.setText("alice")
    panel.folder_edit.setText(str(folder))
    panel.format_buttons[ExportFormat.HTML].setChecked(True)
    panel.image_buttons[ImageMode.LINK].setChecked(True)
    panel.delay_spin.setValue(0)
    return panel


def test_batch_header_follows_source_and_options(window):
    panel = window.batch_panel
    panel.source_edit.setText("https://habr.com/ru/users/alice/")
    assert panel.title_label.text() == "Закладки @alice"
    panel.source_buttons[panel.selected_kind().AUTHOR].setChecked(True)
    assert panel.title_label.text() == "Статьи @alice"
    panel.source_edit.setText("не логин!")
    assert panel.title_label.text() == "Статьи @не логин!"
    panel.source_buttons[panel.selected_kind().FILE].setChecked(True)
    panel.source_edit.setText(r"C:\data\links.txt")
    assert panel.title_label.text().startswith("Ссылки из ")
    assert panel.source_field.prefix.isHidden()
    panel.format_buttons[ExportFormat.JSON].setChecked(True)
    assert not panel.image_control.isEnabled()
    assert panel.subtitle_label.text() == "JSON · только адреса картинок · с комментариями"
    panel.format_buttons[ExportFormat.DOCX].setChecked(True)
    panel.image_buttons[ImageMode.LINK].setChecked(True)
    panel.comments_check.setChecked(False)
    assert panel.subtitle_label.text() == "DOCX · картинки ссылками · без комментариев"


def test_batch_retry_failed_article(qtbot, window, batch_session, tmp_path, monkeypatch):
    article_route = batch_session.routes[f"{API}/articles/1000002/"]
    batch_session.routes[f"{API}/articles/1000002/"] = FakeResponse(
        404, payload={"httpCode": 404, "message": "x"}
    )
    panel = _setup_batch(window, tmp_path)
    with qtbot.waitSignal(panel.finished, timeout=30_000) as blocker:
        panel.start()
    report = blocker.args[0]
    assert (report.saved, report.failed) == (2, 1)
    qtbot.waitUntil(lambda: not panel.running, timeout=5000)
    assert panel.failed_count_label.text() == "Ошибок 1"
    assert panel.filter_control.buttons["errors"].text() == "Ошибки 1"
    assert panel.summary_label.text() == "Сохранено 2 статьи, 1 не удалось сохранить"

    panel.filter_control.set_value("errors")
    visible = [panel.log.item(i).isHidden() for i in range(panel.log.count())]
    assert visible.count(False) == 1
    panel.filter_control.set_value("all")

    failed = next(row for row in panel.log_rows() if row.status == "failed")
    assert failed.action_button.text() == "Повторить"
    assert failed.meta.property("tone") == "err"
    batch_session.routes[f"{API}/articles/1000002/"] = article_route
    with qtbot.waitSignal(panel.retried, timeout=30_000) as retried:
        failed.action_button.click()
        assert failed.status == "running"
    assert retried.args[0].status is ItemStatus.SAVED
    qtbot.waitUntil(lambda: not panel.running, timeout=5000)
    assert failed.status == "saved"
    assert report.failed == 0
    assert panel.summary_label.text() == "Сохранено 3 статьи"
    assert "Не удалось сохранить" not in (tmp_path / "index.html").read_text(encoding="utf-8")

    opened = []
    monkeypatch.setattr("habr_exporter.gui.batch_panel.open_path", opened.append)
    failed.action_button.click()  # now it is "Open"
    panel.open_index()
    panel.open_folder()
    assert opened == [retried.args[0].path, report.index_path, report.folder]


def test_batch_eta_and_running_row(qtbot, window, monkeypatch):
    panel = window.batch_panel
    clock = iter([100.0, 160.0])
    monkeypatch.setattr("habr_exporter.gui.batch_panel.time.monotonic", lambda: next(clock))
    ref = ArticleRef(id="5", url="https://habr.com/ru/articles/5/", title="Пятая", author="bob",
                     published=datetime(2025, 3, 5, tzinfo=UTC))  # fmt: skip
    panel._show_state("running")
    panel._on_item_started(ref, 1, 11)
    row = panel.log_rows()[0]
    assert row.status == "running"
    assert row.property("running") is True
    panel._on_progress("1 из 11 · Загрузка комментариев…", 0, 11)
    assert row.meta.text() == "Загрузка комментариев…"
    assert panel.count_label.text() == "0"
    assert panel.of_label.text() == "из 11"
    panel._on_item(BatchItem(ref, ItemStatus.SAVED, path=Path("5 - Пятая.html")), 1, 11)
    assert panel.log.count() == 1  # the "in progress" row became the final one
    assert row.status == "saved"
    assert row.meta.text() == "@bob · 5 мар 2025 · 5 - Пятая.html"
    assert panel.eta_label.text() == "осталось примерно 10 мин"
    other = ArticleRef(id="", url="не ссылка", error="Не похоже на ссылку")
    panel._on_item(BatchItem(other, ItemStatus.FAILED, error=other.error), 11, 11)
    assert panel.eta_label.text() == ""
    assert panel.log_rows()[
        0
    ].action_button.isHidden()  # a file line without an id cannot be retried


def test_batch_progress_badge_in_nav(window):
    panel = window.batch_panel
    panel.progress_changed.emit(3, 10)
    assert window.nav_badge.text() == "3/10"
    assert window.nav_badge.isHidden()  # no export running, so no badge
    window._update_state()
    assert window.nav_badge.text() == ""


def test_batch_retry_ignored_without_report(window):
    panel = window.batch_panel
    row = panel._insert_row()
    panel.retry(row)
    panel._on_row_action(row)
    assert not panel.running


# --- System ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (URL, URL),
        ("  https://habr.com/ru/articles/123/ \n", "https://habr.com/ru/articles/123/"),
        ("habr.com/p/123/", "habr.com/p/123/"),
        ("123", None),
        ("https://example.com/articles/1/", None),
        ("https://habr.com/ru/users/alice/", None),
        ("смотри https://habr.com/ru/articles/123/", None),
        ("https://habr.com/" + "x" * 600, None),
        ("", None),
        (None, None),
    ],
)
def test_article_url_from_clipboard(text, expected):
    assert system.article_url_from_clipboard(text) == expected


def test_short_url():
    assert system.short_url("https://www.habr.com/ru/articles/1/") == "habr.com/ru/articles/1/"
    assert system.short_url("http://habr.com/") == "habr.com/"


@pytest.mark.parametrize(
    ("platform", "expected"),
    [("win32", ["explorer", "/select,"]), ("darwin", ["open", "-R"]), ("linux", None)],
)
def test_reveal_path(monkeypatch, tmp_path, platform, expected):
    target = tmp_path / "a.pdf"
    target.write_bytes(b"x")
    calls, opened = [], []
    monkeypatch.setattr(system.sys, "platform", platform)
    monkeypatch.setattr(system.subprocess, "Popen", calls.append)
    monkeypatch.setattr(system, "open_path", opened.append)
    system.reveal_path(target)
    if expected is None:
        assert calls == []
        assert opened == [tmp_path]
    else:
        assert calls == [[*expected, str(target)]]
    system.reveal_path(tmp_path / "missing.pdf")
    assert opened[-1] == tmp_path


def test_open_path(monkeypatch, tmp_path):
    urls = []
    monkeypatch.setattr(system.QDesktopServices, "openUrl", lambda url: urls.append(url) or True)
    assert system.open_path(tmp_path)
    assert urls[0].toLocalFile() == str(tmp_path).replace("\\", "/")


def test_eta_window_uses_recent_items(window, monkeypatch):
    panel = window.batch_panel
    panel._durations = [1000.0] * 5 + [6.0] * 10
    monkeypatch.setattr("habr_exporter.gui.batch_panel.time.monotonic", time.monotonic)
    ref = ArticleRef(id="1", url="u", title="t")
    panel._item_started = 0.0
    panel._on_item(BatchItem(ref, ItemStatus.SKIPPED, path=Path("1 - t.pdf")), 1, 21)
    assert panel.eta_label.text() == "осталось примерно 2 мин"  # 20 articles at ~6 s each


def test_low_window_scrolls_instead_of_squashing(qtbot, window):
    window.show()
    window.resize(960, 600)
    window.nav.set_value("batch")
    panel = window.batch_panel
    qtbot.waitUntil(lambda: panel.settings_column.verticalScrollBar().maximum() > 0, timeout=3000)
    for card in (panel.source_box, panel.options_box, window.article_card):
        assert card.height() >= card.minimumSizeHint().height()
    for spin in (panel.limit_spin, panel.delay_spin):
        assert spin.height() >= spin.minimumSizeHint().height()
    assert not panel.start_button.visibleRegion().isEmpty()  # the buttons do not scroll away


def test_window_fits_screen(window):
    available = window.screen().availableGeometry()
    assert window.width() <= max(available.width() - 48, window.minimumWidth())
    assert window.height() <= max(available.height() - 48, window.minimumHeight())


def test_source_text_kept_per_kind(window):
    panel = window.batch_panel
    kinds = panel.selected_kind().__class__
    panel.source_buttons[kinds.BOOKMARKS].setChecked(True)
    panel.source_edit.setText("alice")
    panel.source_buttons[kinds.AUTHOR].setChecked(True)
    assert (
        panel.source_edit.text() == "alice"
    )  # the login is shared by bookmarks and author articles
    panel.source_buttons[kinds.FILE].setChecked(True)
    assert panel.source_edit.text() == ""
    panel.source_edit.setText(r"C:\links.txt")
    panel.source_buttons[kinds.BOOKMARKS].setChecked(True)
    assert panel.source_edit.text() == "alice"
    panel.source_buttons[kinds.FILE].setChecked(True)
    assert panel.source_edit.text() == r"C:\links.txt"


def test_stepper_buttons(qtbot):
    spin = QSpinBox()
    spin.setRange(1, 3)
    stepper = Stepper(spin, "Сколько")
    qtbot.addWidget(stepper)
    assert spin.accessibleName() == "Сколько"
    assert spin.buttonSymbols() == QSpinBox.ButtonSymbols.NoButtons
    assert not stepper.minus.isEnabled()  # at the minimum there is nothing to decrease
    stepper.plus.click()
    stepper.plus.click()
    assert spin.value() == 3
    assert not stepper.plus.isEnabled()
    assert stepper.minus.isEnabled()
    stepper.minus.click()
    assert spin.value() == 2
    assert stepper.plus.isEnabled()


def test_batch_limit_and_delay_controls(window):
    panel = window.batch_panel
    assert panel.limit() is None
    panel.limit_control.set_value("limit")
    assert panel.limit_stepper.isEnabled()
    assert panel.limit() == 10
    panel.limit_stepper.plus.click()
    assert panel.limit() == 11
    panel.limit_control.set_value("all")
    assert panel.limit() is None
    assert not panel.limit_stepper.isEnabled()
    panel.limit_spin.setValue(5)  # entering a number enables "At most" by itself
    assert panel.limit_control.value() == "limit"
    assert panel.limit() == 5

    delay = panel.delay_spin.value()
    panel.delay_stepper.plus.click()
    assert panel.delay_spin.value() == delay + 0.5
    panel.delay_spin.setValue(0)
    assert not panel.delay_stepper.minus.isEnabled()


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        (
            "https://habr.com/ru/users/Lord_of_Rings/",
            ("https://habr.com/ru/users/Lord_of_Rings/", ""),
        ),
        ("@alice", ("@alice", "")),
        (r"C:\Users\me\links.txt", ("", r"C:\Users\me\links.txt")),
    ],
)
def test_legacy_source_value_goes_to_its_field(qtbot, client, settings, legacy, expected):
    user, file = expected
    settings.setValue("batch/source", "file")
    settings.setValue("batch/value", legacy)
    panel = MainWindow(client, with_preview=False, settings=settings).batch_panel
    qtbot.addWidget(panel)
    assert panel.source_edit.text() == file  # a profile link is no longer put into the file field
    panel.source_buttons[SourceKind.BOOKMARKS].setChecked(True)
    assert panel.source_edit.text() == user
    panel._store_settings()
    assert settings.value("batch/value") is None
    assert (settings.value("batch/user"), settings.value("batch/file")) == (user, file)


def test_source_fields_roundtrip(qtbot, client, settings):
    first = MainWindow(client, with_preview=False, settings=settings).batch_panel
    qtbot.addWidget(first)
    first.source_edit.setText("alice")
    first.source_buttons[SourceKind.FILE].setChecked(True)
    first.source_edit.setText("D:/links.txt")
    first._store_settings()
    second = MainWindow(client, with_preview=False, settings=settings).batch_panel
    qtbot.addWidget(second)
    assert second.source() == BatchSource(SourceKind.FILE, "D:/links.txt")
    second.source_buttons[SourceKind.AUTHOR].setChecked(True)
    assert second.source_edit.text() == "alice"


def test_source_mismatch_is_explained(window, tmp_path):
    panel = window.batch_panel
    panel.folder_edit.setText(str(tmp_path))
    panel.source_buttons[SourceKind.FILE].setChecked(True)
    panel.source_edit.setText("https://habr.com/ru/users/alice/")
    assert "не файл" in panel.source_hint.text()
    assert panel.source_hint.property("tone") == "err"
    assert not panel.start_button.isEnabled()
    panel.source_edit.setText("links.txt")
    assert panel.source_hint.property("tone") is None
    assert panel.start_button.isEnabled()
    panel.source_buttons[SourceKind.BOOKMARKS].setChecked(True)
    panel.source_edit.setText(r"C:\links.txt")
    assert "Файл ссылок" in panel.source_hint.text()
    assert not panel.start_button.isEnabled()
    panel.source_edit.setText("alice")
    assert panel.source_hint.text() == "Видны только публичные закладки."
    for partial in ("htt", "https:", "https:/"):  # a link still being typed is not a file
        panel.source_edit.setText(partial)
        assert panel.source_hint.property("tone") is None
