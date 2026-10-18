import string
from datetime import UTC, datetime

import pytest
from PyQt6.QtCore import QCoreApplication, QLocale, QSettings

from habr_exporter import i18n
from habr_exporter.formatting import format_duration, format_eta, format_short_datetime
from habr_exporter.gui.main_window import MainWindow
from habr_exporter.i18n import Language, set_language, tr, trn
from habr_exporter.url import InvalidArticleUrlError, parse_article_id

URL = "https://habr.com/ru/companies/acme/articles/1000001/"
#: Number of plural forms per language: 1/2/5 in Russian, one/other in English.
PLURAL_FORMS = {Language.RU: 3, Language.EN: 2}


def _fields(value):
    parts = value if isinstance(value, tuple) else (value,)
    return {name for part in parts for _, name, _, _ in string.Formatter().parse(part) if name}


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def window(qtbot, client, settings):
    win = MainWindow(client, with_preview=False, settings=settings)
    win.show_error_dialogs = False
    qtbot.addWidget(win)
    return win


# --- Dictionaries ----------------------------------------------------------------------


def test_dictionaries_have_the_same_keys():
    assert set(i18n.STRINGS[Language.EN]) == set(i18n.STRINGS[Language.RU])


@pytest.mark.parametrize("key", sorted(i18n.STRINGS[Language.RU]))
def test_translations_keep_placeholders_and_shape(key):
    ru, en = i18n.STRINGS[Language.RU][key], i18n.STRINGS[Language.EN][key]
    assert _fields(ru) == _fields(en)
    assert isinstance(ru, tuple) == isinstance(en, tuple)
    if isinstance(ru, tuple) and key != "months":
        assert len(ru) == PLURAL_FORMS[Language.RU]
        assert len(en) == PLURAL_FORMS[Language.EN]


def test_tr_and_plural_forms():
    assert tr("status.saved", path="a.pdf") == "Сохранено: a.pdf"
    assert [trn("count.comments", n) for n in (1, 3, 5, 11, 21)] == [
        "1 комментарий",
        "3 комментария",
        "5 комментариев",
        "11 комментариев",
        "21 комментарий",
    ]
    set_language(Language.EN)
    assert tr("status.saved", path="a.pdf") == "Saved: a.pdf"
    assert [trn("count.comments", n) for n in (0, 1, 2, 21)] == [
        "0 comments",
        "1 comment",
        "2 comments",
        "21 comments",
    ]


def test_missing_translation_falls_back_to_russian_then_key(monkeypatch):
    monkeypatch.setitem(i18n.STRINGS[Language.RU], "test.only_ru", "только по-русски")
    set_language(Language.EN)
    assert tr("test.only_ru") == "только по-русски"
    assert tr("test.nowhere") == "test.nowhere"


def test_language_cycle_and_labels():
    assert [lang.next() for lang in Language] == [Language.EN, Language.RU]
    assert Language.RU.label == "Русский"
    assert Language.EN.label == "English"


def test_ui_formatting_follows_language():
    when = datetime(2026, 10, 16, 12, 30, tzinfo=UTC)
    now = datetime(2026, 12, 1, tzinfo=UTC)
    assert format_short_datetime(when, UTC, now) == "16 окт, 12:30"
    set_language(Language.EN)
    assert format_short_datetime(when, UTC, now) == "Oct 16, 12:30"
    assert format_short_datetime(when, UTC, datetime(2027, 1, 1, tzinfo=UTC)) == "Oct 16, 2026"
    assert format_duration(460) == "7 min 40 s"
    assert format_eta(30) == "less than a minute"
    assert format_eta(4800) == "about 1 h 20 min"


def test_core_errors_follow_language():
    set_language(Language.EN)
    with pytest.raises(InvalidArticleUrlError, match="Not a Habr link"):
        parse_article_id("https://example.com/articles/1/")


# --- Window ----------------------------------------------------------------------------


def test_default_language_follows_system_locale(qtbot, client, settings):
    QLocale.setDefault(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
    win = MainWindow(client, with_preview=False, settings=settings)
    qtbot.addWidget(win)
    assert win.language is Language.EN
    assert win.load_button.text() == "Load"
    assert win.language_button.text() == "English"


def test_language_button_switches_ui_on_the_fly(window, settings):
    assert window.language is Language.RU
    assert window.language_button.text() == "Русский"
    assert window.load_button.text() == "Загрузить"

    window.language_button.click()

    assert window.language is Language.EN
    assert settings.value("language") == "en"
    assert window.language_button.text() == "English"
    assert "Interface language" in window.language_button.toolTip()
    assert window.windowTitle() == "Habr Exporter"
    assert window.load_button.text() == "Load"
    assert window.nav_batch_label.text() == "Multiple"
    assert window.theme_button.text() == "System"
    assert window.save_button.text() == "Save PDF…"
    assert window.status_label.text() == "Ready"
    assert window.format_buttons[window.selected_format()].hint_label.text() == "For printing"
    panel = window.batch_panel
    assert panel.start_button.text() == "Start"
    assert panel.title_label.text() == "Batch export" or panel.title_label.text().startswith(
        "Bookmarks of @"
    )
    assert panel.limit_stepper.minus.toolTip() == "Less"
    assert panel.delay_spin.suffix() == " s"

    window.language_button.click()
    assert window.language is Language.RU
    assert window.load_button.text() == "Загрузить"
    assert panel.start_button.text() == "Начать"


def test_qt_texts_follow_language(window):
    # Qt's own texts (input field context menus, dialog buttons) are English unless a translator is
    # installed.
    assert QCoreApplication.translate("QLineEdit", "&Copy") != "&Copy"
    window.language_button.click()
    assert QCoreApplication.translate("QLineEdit", "&Copy") == "&Copy"
    window.language_button.click()
    assert QCoreApplication.translate("QLineEdit", "&Copy") != "&Copy"


def test_saved_language_is_restored(qtbot, client, settings):
    settings.setValue("language", "en")
    win = MainWindow(client, with_preview=False, settings=settings)
    qtbot.addWidget(win)
    assert win.language is Language.EN
    assert win.nav_single_label.text() == "Single article"


def test_loaded_article_texts_are_retranslated(qtbot, window):
    window.url_edit.setText(URL)
    with qtbot.waitSignal(window.loaded, timeout=10_000):
        window.load_button.click()
    qtbot.waitUntil(lambda: not window.busy, timeout=5000)
    assert window.status_label.text().startswith("Загружено: ")

    window.apply_language(Language.EN)

    assert window.status_label.text().startswith("Loaded: ")
    assert window.article_badge.text() == f"Article {window.article.id}"
    assert "comment" in window.comments_info.text() or window.comments_info.text() == "No comments"
    if window.article.reading_time:
        assert window.article_card.reading_label.text().endswith(" min")
