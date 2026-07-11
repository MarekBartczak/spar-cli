"""Docked orchestrator chat panel (ADR 0005). Shell — filled out in a later task."""
from __future__ import annotations

try:  # pragma: no cover
    from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False


if _HAS_QT:
    from spar.gui.theme import TOKENS

    class OrchestratorChatPanel(QWidget):
        """Read-only advisor chat, docked at the bottom of the right column."""

        def __init__(self, project_dir, side_cfg, timeout_sec, parent=None, session=None):
            super().__init__(parent)
            self.setObjectName("orchestratorPanel")
            self._project_dir = project_dir
            self._side_cfg = side_cfg
            self._timeout_sec = timeout_sec
            layout = QVBoxLayout(self)
            self.header = QLabel("claude · orkiestrator", self)
            self.header.setObjectName("orchestratorHeader")
            layout.addWidget(self.header)
            self.placeholder = QLabel("czat pojawi się tutaj", self)
            self.placeholder.setStyleSheet(f"color: {TOKENS['muted']};")
            layout.addWidget(self.placeholder)
            layout.addStretch(1)
