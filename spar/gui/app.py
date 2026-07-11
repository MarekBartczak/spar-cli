"""spar gui: PySide6 dashboard-pilot for the spar engine.

``main_gui`` is the sole entry point (routed from ``spar.cli``). It parses
``--dir`` into a single ``project_dir`` that is the one working directory
for the whole GUI session: every future ``QProcess``/``git`` call this
package makes must be scoped to it (``setWorkingDirectory(project_dir)`` /
``git -C project_dir``) so the GUI never polls one project while driving
another (see task brief, review #1).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from spar.config import load_config
from spar.gui import repo as repo_mod
from spar.gui import toolbar as toolbar_mod
from spar.gui.orchestrator import OrchestratorChatPanel
from spar.gui.rails import IconRail, RailButtonSpec, right_column_visibility
from spar.gui.runner import RunnerState, SparRunner
from spar.gui.sidepane import SidePane
from spar.gui.stream import LiveLogTailer, StreamPane
from spar.gui.theme import build_qss
from spar.status import build_status

__all__ = ["MainWindow", "Toolbar", "StreamPane", "SidePane", "main_gui"]

_TOOLBAR_LABELS = ["Nowa debata…", "Start exec", "Wznów", "Stop", "Plan", "Diff"]

# QSplitter sizes expressing the required 1.7 : 1 left:right ratio.
_SPLITTER_SIZES = [1700, 1000]

# A run is LIVE (repo being mutated / read-only advisor caveat applies) in
# these states. LOCKED = a CONFIRMED foreign spar process holds the lock
# (review #28). GATE_PENDING is deliberately EXCLUDED: it also covers a
# DEAD headless process that exited leaving a gate pending (exit 10) — no
# live run then, and the gate force-open (Task 2) already dominates the UI.
_CHAT_BANNER_STATES = frozenset({RunnerState.RUNNING, RunnerState.LOCKED})


def _short_action_label(cmd: str) -> str:
    """Pure: map a spawned command line to a short Polish action label for
    the stream's start notice (smoke-feedback round 2, fix 1) -- the
    previous statusbar-only indicator was invisible to the user, so this
    text is what gets echoed straight into the transcript view instead.

    Recognizes the shapes ``SparRunner`` actually spawns: a fresh debate
    (``--task-file``), a fresh exec (``exec`` without ``--continue``), and a
    resume of either (``--continue``, optionally prefixed by ``exec``).
    """
    tokens = cmd.split()
    if "--task-file" in tokens:
        return "nowa debata"
    has_exec = "exec" in tokens
    has_continue = "--continue" in tokens
    if has_continue:
        return "wznów exec" if has_exec else "wznów"
    if has_exec:
        return "start exec"
    return "spar"


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


class RightColumn(QWidget):
    """Right-side tool column: SidePane (Taski + gate) over the chat panel."""

    def __init__(self, side_pane, chat_panel, parent=None):
        super().__init__(parent)
        self.setObjectName("rightColumn")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(side_pane, stretch=3)
        layout.addWidget(chat_panel, stretch=2)
        self._side_pane = side_pane
        self._chat_panel = chat_panel

    def set_tasks_visible(self, visible: bool) -> None:
        self._side_pane.setVisible(visible)

    def set_chat_visible(self, visible: bool) -> None:
        self._chat_panel.setVisible(visible)


class MainWindow(QMainWindow):
    """Top-level window: toolbar + (stream | side) split + status bar."""

    def __init__(self, project_dir: "str | Path", parent=None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.setWindowTitle(f"spar — {self.project_dir.name}")
        # A real default size: without it the window starts at whatever tiny
        # size Qt picks before a show(), which is too small for the splitter
        # to honor the 1.7:1 ratio against the side pane's now-nonzero
        # minimum size hint (task/gate widgets, unlike the old placeholder).
        self.resize(1600, 900)

        self.toolbar = Toolbar(self)
        self.addToolBar(self.toolbar)

        # Side's config model, resolved once per session, feeds StreamPane's
        # human-readable prefix translation for debate rounds (fix 4). Built
        # before the side pane's first refresh(), which fires
        # ``status_changed`` -> ``_on_status_changed`` synchronously.
        # DEBATE rounds actually run on debate_model (the engine resolves
        # ``debate_model or model``) — the display must mirror that
        # resolution, not default_model (live finding: prefixes showed
        # sonnet while the transcript proved opus).
        try:
            config = load_config(self.project_dir)
            self._side_models = {
                name: (side.debate_model or side.model or side.default_model or "")
                for name, side in config.sides.items()
            }
        except Exception:
            self._side_models = {}

        # Process pilot: owns the spar QProcess for this project_dir. Built
        # before the side pane, which needs it to wire the gate buttons.
        self.runner = SparRunner(self.project_dir, self)

        self.stream_pane = StreamPane(self)
        self.side_pane = SidePane(self.project_dir, self.runner, self)
        self.side_pane.status_changed.connect(self._on_status_changed)
        # The consensus "Accept → start exec" auto-chain must run the same
        # dirty-tree pre-flight as the toolbar button (live finding).
        self.side_pane.gate_panel.preflight_auto_exec = self._commit_if_dirty
        # NOTE (review #1): the single explicit initial side_pane.refresh() is
        # deferred to the END of __init__ — because _on_status_changed now
        # touches self.right_rail (gate icon + force-open), the rails, the
        # right column and the central widget must ALL be built and wired
        # first. Do not re-add a refresh() here.

        # Chat panel needs the same claude side-config resolution the grill uses.
        from spar.gui.toolbar import _grill_availability  # reuse the resolver
        chat_side_cfg, chat_timeout, _reason = _grill_availability(self.project_dir)
        self.chat_panel = OrchestratorChatPanel(
            self.project_dir, chat_side_cfg, chat_timeout
        )
        self.right_column = RightColumn(self.side_pane, self.chat_panel, self)

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.setObjectName("mainSplitter")
        self.splitter.addWidget(self.stream_pane)
        self.splitter.addWidget(self.right_column)
        # QSplitter.setSizes() scales its request to the splitter's OWN
        # current size, which without a show()/layout pass is still Qt's
        # tiny default -- too small to honor the ratio once the side pane
        # gained a real minimum size hint (task board/gate widgets, unlike
        # the old empty placeholder). Resize the splitter itself first so
        # the ratio holds pre-show too (tests construct the window without
        # showing it).
        self.splitter.resize(sum(_SPLITTER_SIZES), 900)
        self.splitter.setSizes(list(_SPLITTER_SIZES))

        self.left_rail = IconRail(
            [RailButtonSpec("files", "Pliki", "Pliki (wkrótce)", icon="🗀", enabled=False)],
            self,
        )
        self.right_rail = IconRail(
            [
                RailButtonSpec("tasks", "Taski", "Panel zadań i bramki", icon="☰"),
                RailButtonSpec("chat", "Czat", "Czat z orkiestratorem", icon="💬"),
                RailButtonSpec("gate", "Bramka", "Otwórz oczekującą bramkę",
                               icon="⚠", checkable=False),
            ],
            self,
        )
        central = QWidget(self)
        central_layout = QHBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.left_rail)
        central_layout.addWidget(self.splitter, stretch=1)
        central_layout.addWidget(self.right_rail)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar(self))

        # Startup indicator: shown between "OK"/"Start exec"/"Wznów" and the
        # process's first output line so the gui doesn't look frozen while
        # the child spins up (task brief, fix 3). Hidden on the first tailer
        # batch after a start, or on finish -- whichever comes first.
        self._startup_label = QLabel("uruchamiam spar…", self)
        self._startup_label.setObjectName("startupLabel")
        self._startup_label.hide()
        self._startup_progress = QProgressBar(self)
        self._startup_progress.setObjectName("startupProgress")
        self._startup_progress.setRange(0, 0)  # indeterminate
        self._startup_progress.setMaximumWidth(120)
        self._startup_progress.hide()
        self.statusBar().addPermanentWidget(self._startup_label)
        self.statusBar().addPermanentWidget(self._startup_progress)

        self._settings = QSettings("spar", "gui")
        self._restore_splitter_state()
        self.splitter.splitterMoved.connect(self._save_splitter_state)

        tasks_visible = self._settings.value("rails/tasks_visible", True, type=bool)
        chat_visible = self._settings.value("rails/chat_visible", True, type=bool)
        self.right_rail.set_checked("tasks", tasks_visible)
        self.right_rail.set_checked("chat", chat_visible)
        self.right_rail.set_button_visible("gate", False)
        self.right_rail.toggled.connect(self._on_rail_toggled)
        self.right_rail.clicked.connect(self._on_rail_clicked)
        # Track the LOGICAL right-column visibility ourselves (review #3): before
        # the top-level window is shown, every widget reports isVisible()==False,
        # so _apply_rail_layout() must NOT read effective visibility to decide
        # whether to restore the splitter ratio — seed the tracker from the
        # restored settings so the first _apply_rail_layout() is a no-op that
        # preserves the QSettings-restored splitter sizes.
        self._column_shown = right_column_visibility(tasks_visible, chat_visible)
        # Force-open bookkeeping for a pending gate (review #4): identity of the
        # gate whose panel we last auto-opened, so only a genuinely NEW gate edge
        # re-opens Taski (and only OPENS it — never resolves/hides it).
        self._prev_gate_key = None
        self._apply_rail_layout()

        # The single explicit initial status synchronization (review #1): now
        # safe to run — the rails/right column/central widget are built and
        # wired, so _on_status_changed can drive the gate icon and, when a gate
        # is already pending on startup, force-open Taski.
        self.side_pane.refresh()

        self.runner.started.connect(self._on_started)
        self.runner.finished.connect(self._on_finished)
        self.runner.state_changed.connect(self._on_state_changed)
        # Notices (double-start guard rejections, the auto-exec chain
        # kicking off, ...) surface straight into the stream transcript
        # (fix 1/2) -- not just the statusbar, which is easy to miss.
        self.runner.notice.connect(self.stream_pane.append_notice)
        self._wire_toolbar()
        self._sync_toolbar()

        # Live stream tailer: path injected (project_dir), never cwd-relative
        # (review #9 -- the gui may run with cwd != project_dir).
        self.tailer = LiveLogTailer(self.project_dir / ".spar" / "live.log", self)
        self.tailer.lines.connect(self.stream_pane.feed_lines)
        self.tailer.lines.connect(self._on_first_stream_lines)
        self.tailer.start()

    # ------------------------------------------------------------------
    # Runner wiring
    # ------------------------------------------------------------------
    def _wire_toolbar(self) -> None:
        actions = self.toolbar.actions_by_label
        actions[toolbar_mod.NEW_DEBATE].triggered.connect(self._on_new_debate)
        actions[toolbar_mod.START_EXEC].triggered.connect(self._on_start_exec)
        actions[toolbar_mod.RESUME].triggered.connect(lambda: self.runner.resume(None))
        actions[toolbar_mod.STOP].triggered.connect(self.runner.stop)
        actions["Plan"].triggered.connect(self.side_pane.show_plan)
        actions["Diff"].triggered.connect(self.side_pane.show_diff)

    def _on_status_changed(self, status: dict) -> None:
        actions = self.toolbar.actions_by_label
        actions["Plan"].setEnabled(bool(status.get("artifact")))
        actions["Diff"].setEnabled(bool(status.get("branches")))

        tasks = status.get("tasks") or {}
        task_models = {
            task_id: {"model": task.get("model"), "review_model": task.get("review_model")}
            for task_id, task in tasks.items()
        }
        self.stream_pane.set_models({"sides": self._side_models, "tasks": task_models})

        pending_gate = status.get("pending_gate")
        pending = bool(pending_gate)
        self.right_rail.set_button_visible("gate", pending)
        self.right_rail.set_attention("gate", pending)
        # ADR 0005: a pending gate force-opens its panel. Fire only on the edge to
        # a genuinely new gate (identity = name + task_id + rounds) so a user who
        # deliberately collapsed Taski for the SAME gate isn't fought on every 2s
        # poll; a brand-new gate still reasserts the panel. This only ever OPENS
        # Taski — it never resolves, hides or destroys the gate.
        gate_key = self._gate_identity(pending_gate)
        if pending and gate_key != self._prev_gate_key:
            if not self.right_rail.buttons["tasks"].isChecked():
                self.right_rail.set_checked("tasks", True)
                self._on_rail_toggled("tasks", True)  # persists + re-applies layout
        self._prev_gate_key = gate_key if pending else None

    @staticmethod
    def _gate_identity(pending_gate: "dict | None") -> "tuple | None":
        if not pending_gate:
            return None
        ctx = pending_gate.get("context") or {}
        return (pending_gate.get("name"), ctx.get("task_id"), ctx.get("rounds"))

    def _rail_state(self) -> tuple[bool, bool]:
        return (
            self.right_rail.buttons["tasks"].isChecked(),
            self.right_rail.buttons["chat"].isChecked(),
        )

    def _apply_rail_layout(self) -> None:
        tasks_visible, chat_visible = self._rail_state()
        self.right_column.set_tasks_visible(tasks_visible)
        self.right_column.set_chat_visible(chat_visible)
        show_column = right_column_visibility(tasks_visible, chat_visible)
        # Review #3: compare against the tracked LOGICAL previous state, NOT
        # self.right_column.isVisible(). Pre-show, isVisible() is always False,
        # so the old check treated normal startup as a hidden→shown transition
        # and clobbered the QSettings-restored splitter sizes with
        # _SPLITTER_SIZES. Restore the 1.7:1 ratio only on a real collapsed→
        # shown edge.
        if show_column and not self._column_shown:
            self.right_column.setVisible(True)
            self.splitter.setSizes(list(_SPLITTER_SIZES))  # restore 1.7:1
        else:
            self.right_column.setVisible(show_column)
        self._column_shown = show_column

    def _on_rail_toggled(self, key: str, checked: bool) -> None:
        self._settings.setValue(f"rails/{key}_visible", checked)
        self._apply_rail_layout()

    def _on_rail_clicked(self, key: str) -> None:
        if key == "gate":
            # Force-open Taski (which hosts the GatePanel); never discards it.
            self.right_rail.set_checked("tasks", True)
            self._on_rail_toggled("tasks", True)

    def _current_status(self) -> dict:
        try:
            return build_status(self.project_dir / ".spar")
        except Exception:
            return {"phase": None, "pending_gate": None, "tasks": {}, "artifact": None, "branches": None}

    def _sync_toolbar(self) -> None:
        self._on_state_changed(self.runner.current_state())

    def _on_state_changed(self, state: RunnerState) -> None:
        toolbar_mod.apply_state(self.toolbar, state, self._current_status())
        self.chat_panel.set_running(state in _CHAT_BANNER_STATES)

    def _on_started(self, cmd: str) -> None:
        self.statusBar().showMessage(f"uruchomiono: {cmd}")
        self._startup_label.show()
        self._startup_progress.show()
        # Visible-in-the-stream start feedback (fix 1) -- the statusbar
        # message above is easy to miss; this line lands right in the
        # transcript the user is already watching.
        self.stream_pane.append_notice(f"▶ uruchamiam: {_short_action_label(cmd)}…")

    def _on_finished(self, exit_code: int) -> None:
        self.statusBar().showMessage(f"zakończono (exit {exit_code})")
        self._hide_startup_indicator()

    def _on_first_stream_lines(self, _lines: list) -> None:
        self._hide_startup_indicator()

    def _hide_startup_indicator(self) -> None:
        self._startup_label.hide()
        self._startup_progress.hide()

    def _on_new_debate(self) -> None:
        # Repo check comes FIRST — before the user invests time in typing the
        # task (live feedback: the create-repo question must not appear after
        # the form).
        if not self._ensure_git_repo():
            return
        if repo_mod.ensure_project_config(self.project_dir):
            self.stream_pane.append_notice(
                "▶ utworzono .spar/config.toml — dostosuj modele i test_command do projektu"
            )
        dialog = toolbar_mod.NewDebateDialog(self.project_dir, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.runner.start_debate(**dialog.values())

    def _on_start_exec(self) -> None:
        """Pre-flight for the "Start exec" action (shared by the toolbar
        button in its DONE and RESUMABLE-bridge enablement) -- ``spar exec``
        refuses outright (exit 3, "target worktree not clean") on any
        uncommitted change, but a grill session legitimately leaves behind
        CONTEXT.md/ADR edits with no in-gui way forward (live finding).
        A dirty tree now gets a confirm-and-commit offer instead of a dead
        end; a clean tree proceeds exactly as before with no dialog.
        """
        if not self._commit_if_dirty():
            return
        self.runner.start_exec()

    def _commit_if_dirty(self) -> bool:
        """Offer to commit a dirty tree before an exec starts.

        Returns True when exec may proceed (tree clean, or the user accepted
        the commit); False when the user cancelled. Shared by the toolbar's
        "Start exec" AND the gate panel's "Accept → start exec" auto-chain
        (live finding: the auto-chain bypassed the toolbar pre-flight and
        died on exit 3 again).
        """
        dirty = repo_mod.dirty_paths(self.project_dir)
        if not dirty:
            return True
        shown = dirty[:10]
        listing = "\n".join(shown)
        remaining = len(dirty) - len(shown)
        if remaining > 0:
            listing += f"\n… i {remaining} więcej"
        reply = QMessageBox.question(
            self,
            "Niezacommitowane zmiany",
            "Repozytorium ma niezacommitowane pliki (np. dokumenty z grilla):\n"
            f"{listing}\n\nZacommitować je i wystartować exec?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False
        repo_mod.commit_all(
            self.project_dir,
            "docs: pre-exec snapshot (grill artifacts / manual edits)",
        )
        self.stream_pane.append_notice("▶ zacommitowano zmiany przed exec")
        return True

    def _ensure_git_repo(self) -> bool:
        """Confirm (creating on demand) that ``project_dir`` has a git repo
        with at least one commit before a new debate spawns.

        Returns ``True`` when the debate may proceed, ``False`` when the
        user declined the offer to create one (in which case the caller
        must not spawn).
        """
        state = repo_mod.repo_state(self.project_dir)
        if state == "ok":
            return True

        if state == "none":
            text = (
                "Ten katalog nie jest repozytorium git. spar wymaga "
                "lokalnego repo (gałęzie, merge). Utworzyć je teraz?"
            )
        else:  # "no_head"
            text = (
                "Repozytorium nie ma żadnego commita — utworzyć commit "
                "początkowy?"
            )

        reply = QMessageBox.question(
            self,
            "Brak repozytorium git",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

        if state == "none":
            repo_mod.create_repo(self.project_dir)
        else:
            repo_mod.create_initial_commit(self.project_dir)
        self.stream_pane.append_notice(
            "▶ utworzono repozytorium git (commit początkowy)"
        )
        return True

    def _restore_splitter_state(self) -> None:
        state = self._settings.value("mainSplitter/state")
        if state is not None:
            self.splitter.restoreState(state)

    def _save_splitter_state(self, *_args) -> None:
        self._settings.setValue("mainSplitter/state", self.splitter.saveState())

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._save_splitter_state()
        self.tailer.stop()
        # Stop the runner's process poll and the side pane's status poll so
        # an embedded/reused MainWindow doesn't leak timers, and interrupt a
        # still-running child the same way Stop does (SIGINT) so it isn't
        # orphaned holding the .spar lock (final review, minor #1).
        self.runner._poll.stop()
        self.side_pane._poll.stop()
        self.runner.stop()
        # Tear down the orchestrator chat session too (idempotent; a mid-turn
        # worker thread is abandoned via _ABANDONED_THREADS, never destroyed
        # while running — review #2).
        self.chat_panel.stop_session()
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
