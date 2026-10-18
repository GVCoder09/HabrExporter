"""Background GUI tasks: networking and heavy document preparation do not block the window."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal

from ..service import Progress


class TaskThread(QThread):
    """Runs a function in a separate thread.

    The function receives a progress callback as its first argument. The result or the error text is
    delivered via the ``succeeded`` / ``failed`` signals (already in the GUI thread).
    """

    progress = pyqtSignal(str, int, int)
    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn: Callable[[Progress], Any], parent: Any = None) -> None:
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn(self.progress.emit)
        except Exception as exc:  # noqa: BLE001 - any error is shown to the user
            self.failed.emit(str(exc) or exc.__class__.__name__)
        else:
            self.succeeded.emit(result)
