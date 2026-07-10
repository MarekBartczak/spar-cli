"""spar gui: PySide6 dashboard-pilot for the spar engine (skeleton).

``main_gui`` is the sole entry point (routed from ``spar.cli``). It parses
``--dir`` into a single ``project_dir`` that is the one working directory
for the whole GUI session: every future ``QProcess``/``git`` call this
package makes must be scoped to it (``setWorkingDirectory(project_dir)`` /
``git -C project_dir``) so the GUI never polls one project while driving
another (see task brief, review #1). This task only builds the window
skeleton; process/git wiring lands in later tasks.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QMainWindow,
    QSplitter,
    QStatusBar,
    QToolBar,
    QWidget,
)

from spar.gui import toolbar as toolbar_mod
from spar.gui.runner import RunnerState, SparRunner
from spar.gui.stream import LiveLogTailer, StreamPane
from spar.gui.theme import build_qss
from spar.status import build_status

__all__ = ["MainWindow", "Toolbar", "StreamPane", "SidePane", "main_gui"]

_TOOLBAR_LABELS = ["Nowa debata…", "Start exec", "Wznów", "Stop", "Plan", "Diff"]

# QSplitter sizes expressing the required 1.7 : 1 left:right ratio.
_SPLITTER_SIZES = [1700, 1000]


class SidePane(QWidget):
    """Right pane placeholder: tasks + gate view (built in a later task)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("sidePane")


class Toolbar(QToolBar):
    """Toolbar with disabled placeholder actions; wired up in a later task."""

    def __init__(self, parent=None):
        super().__init__("spar", parent)
        self.setObjectName("toolbar")
        self.setMovable(False)
        self.actions_by_label: dict[str, "QAction"] = {}
        for label in _TOOLBAR_LABELS:
            action = self.addAction(label)
            action.setEnabled(False)
            self.actions_by_label[label] = action


class MainWindow(QMainWindow):
    """Top-level window: toolbar + (stream | side) split + status bar."""

    def __init__(self, project_dir: "str | Path", parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.setWindowTitle(f"spar — {self.project_dir.name}")

        self.toolbar = Toolbar(self)
        self.addToolBar(self.toolbar)

        self.stream_pane = StreamPane(self)
        self.side_pane = SidePane(self)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.addWidget(self.stream_pane)
        self.splitter.addWidget(self.side_pane)
        self.splitter.setSizes(list(_SPLITTER_SIZES))
        self.setCentralWidget(self.splitter)

        self.setStatusBar(QStatusBar(self))

        self._settings = QSettings("spar", "gui")
        self._restore_splitter_state()
        self.splitter.splitterMoved.connect(self._save_splitter_state)

        # Process pilot: owns the spar QProcess for this project_dir.
        self.runner = SparRunner(self.project_dir, self)
        self.runner.started.connect(self._on_started)
        self.runner.finished.connect(self._on_finished)
        self.runner.state_changed.connect(self._on_state_changed)
        self._wire_toolbar()
        self._sync_toolbar()

        # Live stream tailer: path injected (project_dir), never cwd-relative
        # (review #9 -- the gui may run with cwd != project_dir).
        self.tailer = LiveLogTailer(self.project_dir / ".spar" / "live.log", self)
        self.tailer.lines.connect(self.stream_pane.feed_lines)
        self.tailer.start()

    # ------------------------------------------------------------------
    # Runner wiring
    # ------------------------------------------------------------------
    def _wire_toolbar(self) -> None:
        actions = self.toolbar.actions_by_label
        actions[toolbar_mod.NEW_DEBATE].triggered.connect(self._on_new_debate)
        actions[toolbar_mod.START_EXEC].triggered.connect(self.runner.start_exec)
        actions[toolbar_mod.RESUME].triggered.connect(lambda: self.runner.resume(None))
        actions[toolbar_mod.STOP].triggered.connect(self.runner.stop)

    def _current_status(self) -> dict:
        try:
            return build_status(self.project_dir / ".spar")
        except Exception:
            return {"phase": None, "pending_gate": None, "tasks": {}, "artifact": None}

    def _sync_toolbar(self) -> None:
        self._on_state_changed(self.runner.current_state())

    def _on_state_changed(self, state: RunnerState) -> None:
        toolbar_mod.apply_state(self.toolbar, state, self._current_status())

    def _on_started(self, cmd: str) -> None:
        self.statusBar().showMessage(f"uruchomiono: {cmd}")

    def _on_finished(self, exit_code: int) -> None:
        self.statusBar().showMessage(f"zakończono (exit {exit_code})")

    def _on_new_debate(self) -> None:
        dialog = toolbar_mod.NewDebateDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.runner.start_debate(**dialog.values())

    def _restore_splitter_state(self) -> None:
        state = self._settings.value("mainSplitter/state")
        if state is not None:
            self.splitter.restoreState(state)

    def _save_splitter_state(self, *_args) -> None:
        self._settings.setValue("mainSplitter/state", self.splitter.saveState())

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._save_splitter_state()
        self.tailer.stop()
        super().closeEvent(event)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="spar gui",
        description="Launch the spar gui dashboard-pilot",
    )
    parser.add_argument(
        "--dir", dest="project_dir", default=None, metavar="PATH",
        help="Project directory the gui operates on (default: current directory)",
    )
    return parser.parse_args(argv)


def main_gui(argv: list[str]) -> int:
    """Entry point for the ``spar gui`` subcommand."""
    args = _parse_args(argv)
    project_dir = Path(args.project_dir).resolve() if args.project_dir else Path.cwd()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(build_qss())

    window = MainWindow(project_dir)
    window.show()

    return app.exec()
