"""Window styling: light and dark palettes, the Qt stylesheet (QSS) and icons.

Colors come from the mockup: neutral background and cards, and a Habr-blue accent darkened so that
white text on it stays readable (contrast of at least 4.5:1). The "system" theme follows the OS
setting (``QStyleHints.colorScheme``) and switches on the fly when the user changes it.

Icons are short SVG paths (as in the mockup) drawn in the required color via ``QSvgRenderer``.
QSS cannot recolor images, so widgets with icons redraw them when the palette changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from string import Template

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QColor, QGuiApplication, QPainter, QPalette, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QWidget

from ..i18n import tr


class ThemeMode(StrEnum):
    """User's choice; stored in settings."""

    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"

    @property
    def label(self) -> str:
        return tr(f"theme.{self.value}")

    def next(self) -> ThemeMode:
        """Return the next mode in a cycle: system -> light -> dark -> system."""
        order = list(ThemeMode)
        return order[(order.index(self) + 1) % len(order)]


@dataclass(frozen=True, slots=True)
class Theme:
    """Color set of one theme (names are roles, not shades)."""

    name: str
    dark: bool
    window: str
    surface: str
    surface2: str
    border: str
    divider: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_text: str
    accent_soft: str
    accent_ink: str
    chip: str
    beta_bg: str
    beta_text: str
    ok: str
    ok_soft: str
    err: str
    err_soft: str
    skip: str
    track: str
    preview: str
    disabled_bg: str
    disabled_text: str
    running_row: str


LIGHT = Theme(
    name="light",
    dark=False,
    window="#eef1f4",
    surface="#ffffff",
    surface2="#f4f6f8",
    border="#d8dfe5",
    divider="#edf0f3",
    text="#1c2731",
    muted="#566573",
    accent="#2f7396",
    accent_hover="#28627f",
    accent_text="#ffffff",
    accent_soft="#e4eff5",
    accent_ink="#245d7a",
    chip="#e6ebef",
    beta_bg="#fbefd5",
    beta_text="#774c00",
    ok="#2b6e35",
    ok_soft="#e2f2e5",
    err="#a8281f",
    err_soft="#fbe7e5",
    skip="#8d9aa5",
    track="#dbe2e8",
    preview="#e9edf1",
    disabled_bg="#dfe5ea",
    disabled_text="#7d8a95",
    running_row="#f5f9fb",
)

DARK = Theme(
    name="dark",
    dark=True,
    window="#101418",
    surface="#181d22",
    surface2="#20262c",
    border="#2b333b",
    divider="#22292f",
    text="#e4e9ed",
    muted="#9ba8b4",
    accent="#5aa9cf",
    accent_hover="#6db6d8",
    accent_text="#07202c",
    accent_soft="#163040",
    accent_ink="#86c4e2",
    chip="#2a323a",
    beta_bg="#3a2d12",
    beta_text="#f0c56b",
    ok="#86d392",
    ok_soft="#163220",
    err="#f39189",
    err_soft="#3a1a18",
    skip="#7b8894",
    track="#2b333b",
    preview="#12171b",
    disabled_bg="#262d33",
    disabled_text="#6f7c87",
    running_row="#1b252c",
)

_current = LIGHT


def current() -> Theme:
    """Return the most recently applied theme, used by icons and custom-painted widgets."""
    return _current


def system_is_dark() -> bool:
    """Tell whether the system uses a dark theme (False without ``QGuiApplication`` or on an unknown platform)."""
    if QGuiApplication.instance() is None:
        return False
    return QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark


def resolve(mode: ThemeMode) -> Theme:
    """Return the theme for the selected mode."""
    if mode is ThemeMode.LIGHT:
        return LIGHT
    if mode is ThemeMode.DARK:
        return DARK
    return DARK if system_is_dark() else LIGHT


def palette(theme: Theme) -> QPalette:
    """Return a palette for what QSS does not cover: popup menus, scroll bars, dialogs."""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: theme.window,
        QPalette.ColorRole.WindowText: theme.text,
        QPalette.ColorRole.Base: theme.surface,
        QPalette.ColorRole.AlternateBase: theme.surface2,
        QPalette.ColorRole.Text: theme.text,
        QPalette.ColorRole.Button: theme.surface2,
        QPalette.ColorRole.ButtonText: theme.text,
        QPalette.ColorRole.Highlight: theme.accent,
        QPalette.ColorRole.HighlightedText: theme.accent_text,
        QPalette.ColorRole.PlaceholderText: theme.muted,
        QPalette.ColorRole.ToolTipBase: theme.surface,
        QPalette.ColorRole.ToolTipText: theme.text,
        QPalette.ColorRole.Link: theme.accent_ink,
        QPalette.ColorRole.Mid: theme.border,
    }
    for role, color in roles.items():
        pal.setColor(role, QColor(color))
    for role in (
        QPalette.ColorRole.Text,
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.ButtonText,
    ):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(theme.disabled_text))
    return pal


_QSS = Template("""
QWidget { color: $text; font-size: 10pt; }
QWidget#page { background: $window; }
QTabWidget::pane { border: none; background: $window; }
QScrollArea#column { background: transparent; border: none; }
QScrollArea#column > QWidget#qt_scrollarea_viewport, QWidget#columnBody { background: transparent; }
QToolTip { background: $surface; color: $text; border: 1px solid $border; padding: 4px 6px; }
QLabel { background: transparent; }
QLabel[role="muted"] { color: $muted; }
QLabel[role="small"] { color: $muted; font-size: 9pt; }
QLabel[role="tiny"] { color: $muted; font-size: 8pt; }
QLabel[role="section"] { color: $muted; font-size: 8pt; font-weight: 600; }
QLabel[role="strong"] { font-weight: 600; }
QLabel[role="title"] { font-size: 11pt; font-weight: 600; }
QLabel[role="heading"] { font-size: 13pt; font-weight: 600; }
QLabel[role="big"] { font-size: 22pt; font-weight: 600; }
QLabel[role="appname"] { font-size: 11pt; font-weight: 600; }
QLabel[role="ink"] { color: $accent_ink; }
QLabel[role="mono"] { color: $muted; font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", monospace; font-size: 9pt; }
QLabel[tone="ok"] { color: $ok; }
QLabel[tone="err"] { color: $err; }
QLabel#beta { background: $beta_bg; color: $beta_text; border-radius: 4px; padding: 0 4px; font-size: 7pt; font-weight: 600; }
QLabel#badge { background: $accent; color: $accent_text; border-radius: 8px; padding: 0 6px; font-size: 8pt; font-weight: 600; }
QLabel#okBadge { background: $ok_soft; color: $ok; border-radius: 10px; padding: 2px 8px; font-size: 9pt; }
QLabel#initials { background: $chip; color: $text; border-radius: 6px; font-size: 7pt; font-weight: 700; }
QLabel#statusIcon { border-radius: 14px; }
QLabel#statusIcon[tone="saved"] { background: $ok_soft; }
QLabel#statusIcon[tone="skipped"] { background: $chip; }
QLabel#statusIcon[tone="failed"] { background: $err_soft; }
QLabel#statusIcon[tone="running"] { background: $accent_soft; }
QLabel#emptyIcon { background: $surface; border: 1px solid $border; border-radius: 18px; }

QFrame#appbar { background: $surface; border-bottom: 1px solid $border; }
QFrame#statusbar { background: $surface; border-top: 1px solid $border; }
QFrame#card { background: $surface; border: 1px solid $border; border-radius: 14px; }
QFrame#cardHeader { border-bottom: 1px solid $border; }
QWidget#previewBody { background: $preview; border-bottom-left-radius: 14px; border-bottom-right-radius: 14px; }
QFrame#separator { background: $border; border: none; min-height: 1px; max-height: 1px; }
QFrame#banner { border-radius: 12px; }
QFrame#banner[tone="ok"] { background: $ok_soft; }
QFrame#banner[tone="info"] { background: $accent_soft; }
QFrame#banner[tone="neutral"] { background: $surface2; }
QFrame#banner QLabel[role="ink"] { color: $accent_ink; }
QFrame#logrow { border-bottom: 1px solid $divider; }
QFrame#logrow[running="true"] { background: $running_row; }

QFrame#field { background: $surface; border: 2px solid $border; border-radius: 10px; }
QFrame#field[focused="true"] { border-color: $accent; }
QFrame#field[variant="large"] { border-radius: 12px; }
QFrame#field:disabled { background: $surface2; }
QLineEdit { background: transparent; border: none; color: $text; padding: 0; selection-background-color: $accent; selection-color: $accent_text; }
QLineEdit[variant="large"] { font-size: 11pt; }
QAbstractSpinBox { background: $surface; border: 2px solid $border; border-radius: 10px; padding: 6px 8px; color: $text; selection-background-color: $accent; selection-color: $accent_text; }
QAbstractSpinBox:focus { border-color: $accent; }
QAbstractSpinBox:disabled { background: $surface2; color: $disabled_text; }
QFrame#stepper { background: $surface; border: 2px solid $border; border-radius: 10px; }
QFrame#stepper:disabled { background: $surface2; }
QAbstractSpinBox#stepValue { background: transparent; border: none; padding: 0; font-weight: 600; }
QAbstractSpinBox#stepValue:disabled { color: $disabled_text; }
QPushButton#stepButton { background: transparent; border: none; border-radius: 7px; color: $accent_ink; font-size: 14pt; font-weight: 600; padding: 0; }
QPushButton#stepButton:hover { background: $accent_soft; }
QPushButton#stepButton:disabled { color: $disabled_text; background: transparent; }

QPushButton { background: $surface2; color: $text; border: 1px solid $border; border-radius: 8px; padding: 6px 14px; }
QPushButton:hover { border-color: $accent; }
QPushButton:pressed { background: $accent_soft; }
QPushButton:disabled { color: $disabled_text; background: $disabled_bg; border-color: $disabled_bg; }
QPushButton:focus { outline: none; }
QPushButton[kind="primary"] { background: $accent; color: $accent_text; border: none; border-radius: 12px; font-weight: 600; padding: 0 22px; min-height: 44px; }
QPushButton[kind="primary"]:hover { background: $accent_hover; }
QPushButton[kind="primary"]:disabled { background: $disabled_bg; color: $disabled_text; }
QPushButton[kind="primary"][variant="small"] { border-radius: 8px; min-height: 30px; padding: 0 12px; }
QPushButton[kind="large"] { font-size: 11pt; }
QPushButton[kind="danger"] { background: $surface; color: $err; border: 2px solid $err; border-radius: 12px; font-weight: 600; min-height: 40px; }
QPushButton[kind="danger"]:disabled { color: $disabled_text; border-color: $border; background: $surface; }
QPushButton[kind="link"] { background: transparent; border: none; color: $accent_ink; padding: 2px 0; font-weight: 500; }
QPushButton[kind="link"]:hover { text-decoration: underline; }
QPushButton[kind="ghost"] { background: transparent; border: 1px solid $border; border-radius: 8px; padding: 4px 12px; }
QPushButton[kind="ghost"][tone="err"] { color: $err; border-color: $err; }
QPushButton[kind="ghost"][tone="ink"] { color: $accent_ink; }
QPushButton[kind="close"] { background: transparent; border: none; padding: 4px; border-radius: 6px; }
QPushButton[kind="close"]:hover { background: $surface; }

QFrame#segmented { background: $surface2; border: 1px solid $border; border-radius: 10px; }
QPushButton#segment { background: transparent; border: 1px solid transparent; border-radius: 7px; color: $muted; padding: 5px 12px; font-weight: 500; }
QPushButton#segment:hover { color: $text; }
QPushButton#segment:checked { background: $surface; border-color: $border; color: $text; }
QPushButton#segment:disabled { color: $disabled_text; background: transparent; border-color: transparent; }
QPushButton#tile { background: $surface; border: 2px solid $border; border-radius: 10px; padding: 0; }
QPushButton#tile:hover { border-color: $muted; }
QPushButton#tile:checked { background: $accent_soft; border-color: $accent; }
QPushButton#chip { background: $surface; border: 2px solid $border; border-radius: 16px; padding: 0; font-weight: 600; }
QPushButton#chip:hover { border-color: $muted; }
QPushButton#chip:checked { background: $accent_soft; border-color: $accent; }
QLabel#chipText { font-weight: 600; }

QProgressBar { background: $track; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; color: transparent; }
QProgressBar[variant="thin"] { border-radius: 3px; min-height: 6px; max-height: 6px; }
QProgressBar::chunk { background: $accent; border-radius: 4px; }

QListWidget#log { background: transparent; border: none; outline: 0; }
QListWidget#log::item { border: none; padding: 0; }
QListWidget#log::item:selected, QListWidget#log::item:hover { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: $border; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QMessageBox { background: $surface; }
""")


def stylesheet(theme: Theme) -> str:
    """Return the window stylesheet for the theme."""
    return _QSS.substitute(asdict(theme))


def apply(widget: QWidget, theme: Theme) -> None:
    """Apply the theme to a window: palette and stylesheet; icons redraw on ``PaletteChange``."""
    global _current  # noqa: PLW0603 - default theme for widgets outside a window
    _current = theme
    widget.setProperty("habrTheme", theme.name)
    widget.setPalette(palette(theme))
    widget.setStyleSheet(stylesheet(theme))


def of(widget: QWidget) -> Theme:
    """Return the theme of the window containing the widget (the last applied one before it is added)."""
    name = widget.window().property("habrTheme")
    return {LIGHT.name: LIGHT, DARK.name: DARK}.get(name, _current)


# --- Icons ------------------------------------------------------------------------------

#: Icon paths in a 24x24 square (as in the mockup). ``currentColor`` is the icon color, ``BG`` the backdrop.
ICONS = {
    "link": '<path d="M10 13a5 5 0 0 0 7.07 0l3-3a5 5 0 0 0-7.07-7.07l-1 1"/>'
    '<path d="M14 11a5 5 0 0 0-7.07 0l-3 3a5 5 0 0 0 7.07 7.07l1-1"/>',
    "theme": '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 0 0 17z" fill="currentColor"/>',
    "language": '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/>'
    '<path d="M12 3.5c2.4 2.3 3.6 5.1 3.6 8.5s-1.2 6.2-3.6 8.5c-2.4-2.3-3.6-5.1-3.6-8.5s1.2-6.2 3.6-8.5z"/>',
    "check": '<path d="M5 12l5 5L20 7"/>',
    "check-dot": '<circle cx="12" cy="12" r="11" fill="currentColor" stroke="none"/>'
    '<path d="M7 12.5l3.2 3.2L17 9" stroke="BG" stroke-width="3"/>',
    "download": '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    "external": '<path d="M14 4h6v6M20 4l-9 9"/><path d="M19 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5"/>',
    "folder": '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "clipboard": '<rect x="8" y="3" width="8" height="4" rx="1"/>'
    '<path d="M16 5h2a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h2"/>',
    "clock": '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
    "up": '<path d="M12 19V5M6 11l6-6 6 6"/>',
    "down": '<path d="M12 5v14M6 13l6 6 6-6"/>',
    "eye": '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    "comment": '<path d="M4 5h16v11H9l-5 4z"/>',
    "skip": '<path d="M4 17a8 8 0 0 1 14-6"/><path d="M19 5v6h-6"/>',
    "cross": '<path d="M6 6l12 12M18 6L6 18"/>',
    "spinner": '<path d="M12 3a9 9 0 1 1-9 9"/>',
    "document": '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
    "bookmark": '<path d="M6 3h12v18l-6-4-6 4z"/>',
    "play": '<path d="M7 4.5v15l12-7.5z" fill="currentColor" stroke="none"/>',
    "dot": '<circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/>',
    "logo": '<path d="M6 4v9M14 4v9M6 8.5h8"/><path d="M10 15v5M7 17.5l3 3 3-3"/><path d="M18 9v6"/>',
}


def icon_pixmap(
    name: str,
    color: str,
    size: int = 16,
    ratio: float = 1.0,
    *,
    stroke: float = 2.0,
    bg: str = "none",
) -> QPixmap:
    """Return the icon ``name`` in ``color`` at ``size`` logical pixels."""
    body = ICONS[name].replace("currentColor", color).replace("BG", bg)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">{body}</svg>'
    )
    pixels = max(1, round(size * ratio))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode())).render(painter, QRectF(0, 0, pixels, pixels))
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def logo_pixmap(theme: Theme, size: int = 32, ratio: float = 1.0) -> QPixmap:
    """Return the application logo (``assets/logo.png``) scaled to the size and screen density."""
    from pathlib import Path

    pixels = max(1, round(size * ratio))
    source = QPixmap(str(Path(__file__).resolve().parent.parent / "assets" / "logo.png"))
    pixmap = source.scaled(
        pixels,
        pixels,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    pixmap.setDevicePixelRatio(ratio)
    return pixmap
