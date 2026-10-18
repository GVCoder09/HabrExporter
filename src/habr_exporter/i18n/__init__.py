"""UI languages: Russian and English.

Texts live in plain Python dictionaries, :mod:`.ru` and :mod:`.en`, mapping keys to strings. A
string may contain ``{name}`` placeholders for :func:`tr`; for numbers it is stored as a tuple of
forms: three for Russian (1 статья, 2 статьи, 5 статей) and two for English (1 article,
2 articles), see :func:`trn`. If a translation is missing, the Russian text is used, and if that is
missing too, the key itself.

The language is process-wide: the GUI picks it at startup and changes it on the fly, the CLI
switches to English, and the library default is Russian. Saved files (articles, ``index.html``)
are not translated: they are Habr content, not UI.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any

from . import en, ru

Text = str | tuple[str, ...]


class Language(StrEnum):
    """UI language; stored in settings."""

    RU = "ru"
    EN = "en"

    @property
    def label(self) -> str:
        """Return the language name in that language.

        Recognizable without knowing the current UI language.
        """
        return {Language.RU: "Русский", Language.EN: "English"}[self]

    def next(self) -> Language:
        """Return the next language in a cycle."""
        order = list(Language)
        return order[(order.index(self) + 1) % len(order)]


STRINGS: dict[Language, dict[str, Text]] = {Language.RU: ru.STRINGS, Language.EN: en.STRINGS}
DEFAULT = Language.RU

_language = DEFAULT


def language() -> Language:
    """Return the current UI language."""
    return _language


def set_language(value: Language) -> None:
    global _language  # noqa: PLW0603 - the language is shared by the whole process
    _language = Language(value)


def _lookup(key: str) -> tuple[Language, Text]:
    for lang in (_language, DEFAULT):
        value = STRINGS[lang].get(key)
        if value is not None:
            return lang, value
    return DEFAULT, key


def text(key: str) -> Text:
    """Return the value as is: a string or a tuple (number forms, month names)."""
    return _lookup(key)[1]


def tr(key: str, /, **kwargs: Any) -> str:
    """Translate ``key``, substituting ``kwargs``: ``tr("status.saved", path=path)``."""
    value = text(key)
    if isinstance(value, tuple):
        value = value[0]
    return value.format(**kwargs) if kwargs else value


def trn(key: str, n: int, /, **kwargs: Any) -> str:
    """Return the form for number ``n`` (substituted as ``{n}``).

    Example: ``trn("count.articles", 5)`` -> ``5 статей``.
    """
    lang, forms = _lookup(key)
    if isinstance(forms, str):
        forms = (forms,)
    index = _plural_index(lang, n)
    return forms[min(index, len(forms) - 1)].format(n=n, **kwargs)


def _plural_index(lang: Language, n: int) -> int:
    n = abs(n)
    if lang is Language.EN:
        return 0 if n == 1 else 1
    n %= 100
    if 11 <= n <= 19:
        return 2
    n %= 10
    if n == 1:
        return 0
    return 1 if 2 <= n <= 4 else 2


class Bindings:
    """Window labels that must be recomputed when the language changes.

    Each binding sets the text immediately and is remembered; :meth:`refresh` repeats all of them.
    Methods return the widget so a binding can be written right where the widget is created.
    """

    def __init__(self) -> None:
        self._updates: list[Callable[[], object]] = []

    def call(self, update: Callable[[], object]) -> None:
        self._updates.append(update)
        update()

    def text(self, widget: Any, key: str, /, **kwargs: Any) -> Any:
        self.call(lambda: widget.setText(tr(key, **kwargs)))
        return widget

    def tip(self, widget: Any, key: str, /, **kwargs: Any) -> Any:
        self.call(lambda: widget.setToolTip(tr(key, **kwargs)))
        return widget

    def name(self, widget: Any, key: str, /, **kwargs: Any) -> Any:
        self.call(lambda: widget.setAccessibleName(tr(key, **kwargs)))
        return widget

    def placeholder(self, widget: Any, key: str, /, **kwargs: Any) -> Any:
        self.call(lambda: widget.setPlaceholderText(tr(key, **kwargs)))
        return widget

    def refresh(self) -> None:
        for update in self._updates:
            update()
