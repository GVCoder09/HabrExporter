"""GUI application entry point."""

from __future__ import annotations

import contextlib
import logging
import sys


def _set_icon(app) -> None:  # type: ignore[no-untyped-def]
    """Set the logo as the application icon (window and taskbar)."""
    from pathlib import Path

    from PyQt6.QtGui import QIcon

    if sys.platform == "win32":
        # Without its own AppUserModelID, Windows shows the python.exe icon in the taskbar.
        import ctypes

        with contextlib.suppress(AttributeError, OSError):
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "HabrExporter.HabrExporter"
            )
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "logo.png")))


def create_application(argv: list[str] | None = None):  # type: ignore[no-untyped-def]
    """Create a ``QApplication`` configured for Qt WebEngine."""
    # The WebEngine module must be imported before QApplication is created.
    import PyQt6.QtWebEngineWidgets  # noqa: F401
    from PyQt6.QtCore import QCoreApplication, Qt
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
        app = QApplication(argv if argv is not None else sys.argv)
        app.setApplicationName("Habr Exporter")
        app.setOrganizationName("HabrExporter")
        _set_icon(app)
        # Fusion renders whatever the theme stylesheet does not cover the same way on Windows, macOS
        # and Linux.
        app.setStyle("Fusion")
    return app


def main(argv: list[str] | None = None) -> int:
    """Run the GUI (the ``habr-exporter`` command)."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = create_application(argv)

    from .gui.main_window import MainWindow

    window = MainWindow()
    window.show()
    args = (argv if argv is not None else sys.argv)[1:]
    if args:
        window.url_edit.setText(args[0])
        window.load()
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
