"""Widgets of the UI: cards, format tiles, segmented control, toggle switch.

The look comes from the :mod:`.theme` stylesheet via ``objectName`` and dynamic properties
(``role``, ``kind``, ``tone``). This module only defines widget structure and behavior.
"""

from __future__ import annotations

from collections.abc import Hashable

from PyQt6.QtCore import QEvent, QObject, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFontMetrics, QPainter, QPaintEvent
from PyQt6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..i18n import tr
from . import theme


class ScrollColumn(QScrollArea):
    """Fixed-width column of cards that scrolls on a short screen instead of squashing."""

    def __init__(self, width: int, spacing: int = 12) -> None:
        super().__init__()
        self.setObjectName("column")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFixedWidth(width)
        self.body = QWidget()
        self.body.setObjectName("columnBody")
        self.box = QVBoxLayout(self.body)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(spacing)
        self.setWidget(self.body)
        self.viewport().setAutoFillBackground(False)


def repolish(widget: QWidget) -> None:
    """Recompute the style after a dynamic property changes."""
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def set_prop(widget: QWidget, name: str, value: object) -> None:
    """Change a property that QSS depends on and apply the style immediately."""
    if widget.property(name) != value:
        widget.setProperty(name, value)
        repolish(widget)


def label(
    text: str = "", role: str | None = None, *, wrap: bool = False, name: str | None = None
) -> QLabel:
    """Create a label with a stylesheet role (``muted``, ``small``, ``section``, ``title``...)."""
    widget = QLabel(text)
    if role:
        widget.setProperty("role", role)
    if name:
        widget.setObjectName(name)
    widget.setWordWrap(wrap)
    return widget


def separator() -> QFrame:
    line = QFrame()
    line.setObjectName("separator")
    return line


def elide_lines(text: str, metrics: QFontMetrics, width: int, lines: int = 2) -> str:
    """Wrap text by words into ``lines`` lines of ``width``, replacing the rest with an ellipsis."""
    words = text.split()
    if width <= 0 or not words:
        return text
    out: list[str] = []
    line = ""
    for index, word in enumerate(words):
        trial = f"{line} {word}".strip()
        if not line or metrics.horizontalAdvance(trial) <= width:
            line = trial
            continue
        if len(out) == lines - 1:  # last line: everything left, cut with an ellipsis
            line = " ".join([line, *words[index:]])
            break
        out.append(line)
        line = word
    out.append(metrics.elidedText(line, Qt.TextElideMode.ElideRight, width))
    return "\n".join(out)


class ElidedLabel(QLabel):
    """Single-line label that elides with an ellipsis instead of stretching the window."""

    def __init__(self, text: str = "", role: str | None = None) -> None:
        super().__init__(text)
        if role:
            self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(1)

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setPen(self.palette().color(self.foregroundRole()))
        painter.setFont(self.font())
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(self.alignment() | Qt.AlignmentFlag.AlignVCenter), text)


class IconLabel(QLabel):
    """Icon from :data:`theme.ICONS` in a theme role color (``muted``, ``accent``, ``ok``...).

    Redraws itself when the window gets a new palette (theme change).
    """

    def __init__(
        self, name: str, tone: str = "muted", size: int = 16, *, stroke: float = 2.0, bg: str = ""
    ) -> None:
        super().__init__()
        self._name, self._tone, self._size, self._stroke, self._bg = name, tone, size, stroke, bg
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._render()

    @property
    def icon_name(self) -> str:
        return self._name

    def set_icon(self, name: str | None = None, tone: str | None = None) -> None:
        self._name = name or self._name
        self._tone = tone or self._tone
        self._render()

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.PaletteChange:
            self._render()
        super().changeEvent(event)

    def _render(self) -> None:
        colors = theme.of(self)
        bg = getattr(colors, self._bg) if self._bg else "none"
        ratio = self.devicePixelRatioF() or 1.0
        self.setPixmap(theme.icon_pixmap(self._name, getattr(colors, self._tone), self._size, ratio,
                                         stroke=self._stroke, bg=bg))  # fmt: skip


class Card(QFrame):
    """White (graphite in the dark theme) card with rounded corners."""

    def __init__(
        self, margins: tuple[int, int, int, int] = (16, 14, 16, 16), spacing: int = 8
    ) -> None:
        super().__init__()
        self.setObjectName("card")
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(*margins)
        self.box.setSpacing(spacing)


class Field(QFrame):
    """Framed input field.

    An icon or prefix on the left, a line edit, a frame highlighted on focus.
    """

    def __init__(self, icon: str | None = None, prefix: str = "", *, large: bool = False) -> None:
        super().__init__()
        self.setObjectName("field")
        if large:
            self.setProperty("variant", "large")
        row = QHBoxLayout(self)
        row.setContentsMargins(14 if large else 12, 0, 8 if large else 12, 0)
        row.setSpacing(10 if large else 6)
        self.icon = IconLabel(icon, size=18 if large else 16) if icon else None
        if self.icon:
            row.addWidget(self.icon)
        self.prefix = label(prefix, "muted")
        self.prefix.setVisible(bool(prefix))
        row.addWidget(self.prefix)
        self.edit = QLineEdit()
        if large:
            self.edit.setProperty("variant", "large")
        self.edit.installEventFilter(self)
        row.addWidget(self.edit, 1)
        self.row = row
        self.setFixedHeight(48 if large else 40)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.edit and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            set_prop(self, "focused", event.type() == QEvent.Type.FocusIn)
        return super().eventFilter(watched, event)


class RichButton(QPushButton):
    """Button with custom inner layout.

    Labels do not grab the mouse, the whole button is clickable.
    """

    def __init__(
        self,
        object_name: str,
        *,
        vertical: bool = False,
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
    ):
        super().__init__()
        self.setObjectName(object_name)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.box: QVBoxLayout | QHBoxLayout = QVBoxLayout(self) if vertical else QHBoxLayout(self)
        self.box.setContentsMargins(*margins)
        self.box.setSpacing(2 if vertical else 5)

    def adopt(self, widget: QWidget) -> QWidget:
        widget.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        return widget

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.LayoutRequest:
            # Inner labels changed (language, bold when selected): the button size depends on them.
            self.updateGeometry()
        return super().event(event)

    def sizeHint(self) -> QSize:
        return self.box.sizeHint().expandedTo(QSize(40, 28))

    def minimumSizeHint(self) -> QSize:
        return self.box.minimumSize()


class FormatTile(RichButton):
    """Format tile: name, beta badge, one-line hint and a check mark when selected."""

    def __init__(self, name: str, hint: str, *, beta: bool = False) -> None:
        super().__init__("tile", vertical=True, margins=(10, 7, 7, 8))
        self.label = f"{name} (beta)" if beta else name
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name_label = self.adopt(label(name, "strong"))
        top.addWidget(self.name_label)
        if beta:
            top.addWidget(self.adopt(label("beta", name="beta")))
        top.addStretch(1)
        self.check = self.adopt(IconLabel("check-dot", "accent", 16, bg="accent_text"))
        self.check.setVisible(False)
        top.addWidget(self.check)
        self.box.addLayout(top)
        self.hint_label = self.adopt(ElidedLabel(hint, "tiny"))
        self.hint_label.setToolTip(hint)
        self.box.addWidget(self.hint_label)
        self.setMinimumHeight(54)
        self.toggled.connect(self.check.setVisible)

    def set_hint(self, hint: str) -> None:
        self.hint_label.setText(hint)
        self.hint_label.setToolTip(hint)


class Chip(RichButton):
    """Compact selection "pill" (format in batch mode)."""

    def __init__(self, name: str, *, beta: bool = False) -> None:
        super().__init__("chip", margins=(10, 0, 10, 0))
        self.label = f"{name} (beta)" if beta else name
        self.box.addStretch(1)
        self.text_label = self.adopt(label(name, "strong", name="chipText"))
        self.box.addWidget(self.text_label)
        if beta:
            badge = self.adopt(label("beta", name="beta"))
            badge.setFixedHeight(16)
            self.box.addWidget(badge)
        self.box.addStretch(1)
        self.setFixedHeight(32)
        self.toggled.connect(
            lambda on: set_prop(self.text_label, "role", "ink" if on else "strong")
        )


class SegmentedControl(QFrame):
    """Single choice among several segment buttons (like a tab switcher)."""

    changed = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("segmented")
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(3, 3, 3, 3)
        self.row.setSpacing(4)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: dict[Hashable, QPushButton] = {}

    def add(
        self, key: Hashable, text: str, tooltip: str = "", button: QPushButton | None = None
    ) -> QPushButton:
        button = button or QPushButton(text)
        button.setObjectName("segment")
        button.setCheckable(True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        if tooltip:
            button.setToolTip(tooltip)
        button.toggled.connect(lambda on, key=key: on and self.changed.emit(key))
        self.group.addButton(button)
        self.buttons[key] = button
        self.row.addWidget(button, 1)
        return button

    def value(self) -> Hashable | None:
        return next((key for key, button in self.buttons.items() if button.isChecked()), None)

    def set_value(self, key: Hashable) -> None:
        self.buttons[key].setChecked(True)


class Stepper(QFrame):
    """Number with "-" and "+" buttons on the sides.

    Clear without hints, but keyboard input works too.

    The buttons auto-repeat while held and are disabled at the range limits.
    """

    def __init__(self, spin: QAbstractSpinBox, accessible_name: str) -> None:
        super().__init__()
        self.setObjectName("stepper")
        self.setFixedHeight(40)
        self.spin = spin
        spin.setObjectName("stepValue")
        spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        spin.setAccessibleName(accessible_name)
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(0)
        self.minus = self._button("−", spin.stepDown)
        self.plus = self._button("+", spin.stepUp)
        row.addWidget(self.minus)
        row.addWidget(spin, 1)
        row.addWidget(self.plus)
        spin.valueChanged.connect(self._sync)
        self._sync()
        self.retranslate(accessible_name)

    def retranslate(self, accessible_name: str) -> None:
        """Update the captions to the current UI language."""
        self.spin.setAccessibleName(accessible_name)
        for button, key in ((self.minus, "stepper.less"), (self.plus, "stepper.more")):
            button.setToolTip(tr(key))
            button.setAccessibleName(tr(key))

    def _button(self, text: str, slot) -> QPushButton:  # type: ignore[no-untyped-def]
        button = QPushButton(text)
        button.setObjectName("stepButton")
        button.setFixedSize(32, 32)
        button.setAutoRepeat(True)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(slot)
        return button

    def _sync(self, *_args: object) -> None:
        self.minus.setEnabled(self.spin.value() > self.spin.minimum())
        self.plus.setEnabled(self.spin.value() < self.spin.maximum())


class ToggleSwitch(QAbstractButton):
    """Mobile-style on/off switch; behaves like a check box."""

    def __init__(self, accessible_name: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(accessible_name)
        self.setFixedSize(self.sizeHint())

    def sizeHint(self) -> QSize:
        return QSize(42, 24)

    def paintEvent(self, event: QPaintEvent) -> None:
        colors = theme.of(self)
        on = self.isChecked()
        track = (colors.accent if on else colors.track) if self.isEnabled() else colors.disabled_bg
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QColor(colors.accent) if self.hasFocus() else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(track))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        knob = rect.height() - 4
        x = rect.right() - knob - 2 if on else rect.left() + 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#ffffff"))
        painter.drawEllipse(QRectF(x, rect.top() + 2, knob, knob))
