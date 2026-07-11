"""Vertical icon rails for the spar gui main window (ADR 0005)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RailButtonSpec:
    key: str
    label: str
    tooltip: str
    icon: str = ""          # unicode glyph rendered as the button face (ADR 0005)
    checkable: bool = True
    enabled: bool = True


def right_column_visibility(tasks_visible: bool, chat_visible: bool) -> bool:
    """Pure: the right column is shown iff at least one right panel is visible."""
    return bool(tasks_visible or chat_visible)


try:  # pragma: no cover
    from PySide6.QtCore import QSize, Qt, Signal
    from PySide6.QtGui import QColor, QPainter
    from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget

    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False


if _HAS_QT:

    _RAIL_BUTTON_SIZE = 34   # fixed square face -> a real icon rail, not a text column
    _ATTENTION_DOT = "#e6b800"  # saturated yellow, deliberately hotter than TOKENS['warn'] (#e0b154)

    class _RailButton(QToolButton):
        """Square glyph button that paints a yellow attention dot top-right.

        The dot is an OVERLAY drawn in ``paintEvent`` (QSS cannot draw a filled
        circle); it appears iff the dynamic property ``attention`` is truthy, so
        ``set_attention`` only has to flip the property and call ``update()``.
        """

        def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
            super().paintEvent(event)
            if not self.property("attention"):
                return
            d = 8  # dot diameter
            m = 3  # margin from the top-right corner
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_ATTENTION_DOT))
            painter.drawEllipse(self.width() - d - m, m, d, d)
            painter.end()

    class IconRail(QWidget):
        """A vertical strip of toggle/action buttons on a window edge."""

        toggled = Signal(str, bool)  # key, checked (checkable buttons)
        clicked = Signal(str)        # key (non-checkable buttons)

        def __init__(self, specs: "list[RailButtonSpec]", parent: QWidget | None = None):
            super().__init__(parent)
            self.setObjectName("iconRail")
            layout = QVBoxLayout(self)
            layout.setContentsMargins(2, 6, 2, 6)
            layout.setSpacing(6)
            self.buttons: dict[str, _RailButton] = {}
            for spec in specs:
                btn = _RailButton(self)
                btn.setObjectName(f"rail_{spec.key}")
                # Glyph is the icon face; the human name lives in the tooltip.
                btn.setText(spec.icon or spec.label)
                btn.setToolTip(f"{spec.label} — {spec.tooltip}")
                btn.setCheckable(spec.checkable)
                btn.setEnabled(spec.enabled)
                btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
                btn.setFixedSize(QSize(_RAIL_BUTTON_SIZE, _RAIL_BUTTON_SIZE))
                if spec.checkable:
                    btn.toggled.connect(
                        lambda checked, k=spec.key: self.toggled.emit(k, checked)
                    )
                else:
                    btn.clicked.connect(lambda _=False, k=spec.key: self.clicked.emit(k))
                self.buttons[spec.key] = btn
                layout.addWidget(btn)
            layout.addStretch(1)

        def set_checked(self, key: str, checked: bool) -> None:
            btn = self.buttons.get(key)
            if btn is None:
                return
            btn.blockSignals(True)
            btn.setChecked(checked)
            btn.blockSignals(False)

        def set_attention(self, key: str, on: bool) -> None:
            btn = self.buttons.get(key)
            if btn is None:
                return
            btn.setProperty("attention", bool(on))
            # Re-polish so the QSS border/text (below) tracks the flag, then
            # repaint so the overlay dot in paintEvent is drawn/cleared.
            btn.style().unpolish(btn)
            btn.style().polish(btn)
            btn.update()

        def set_button_visible(self, key: str, visible: bool) -> None:
            btn = self.buttons.get(key)
            if btn is not None:
                btn.setVisible(visible)
