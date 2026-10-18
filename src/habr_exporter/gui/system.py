"""System integration: link from the clipboard, open a file, show a file in its folder."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices

from ..url import InvalidArticleUrlError, parse_article_id

#: Links are never longer than this; longer clipboard text is definitely not a link.
_MAX_URL_LENGTH = 500


def article_url_from_clipboard(text: str | None) -> str | None:
    """Return a Habr article link if the clipboard contains exactly that (on one line).

    A bare number does not count: a number in the clipboard is usually unrelated to Habr.
    """
    text = (text or "").strip()
    if (
        not text
        or len(text) > _MAX_URL_LENGTH
        or any(c.isspace() for c in text)
        or "habr" not in text.lower()
    ):
        return None
    try:
        parse_article_id(text)
    except InvalidArticleUrlError:
        return None
    return text


def short_url(url: str) -> str:
    """Return the link without the scheme and ``www.``, for display in the UI."""
    for prefix in ("https://", "http://"):
        url = url.removeprefix(prefix)
    return url.removeprefix("www.")


def open_path(path: str | Path) -> bool:
    """Open a file or folder with the default application."""
    return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def reveal_path(path: str | Path) -> None:
    """Show the file selected in Explorer (Finder); where that is impossible, open its folder."""
    path = Path(path)
    if not path.exists():
        open_path(path.parent)
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    else:
        open_path(path.parent)
