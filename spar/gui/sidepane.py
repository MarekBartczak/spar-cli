"""Side pane: task board + dynamic gate panel, driven by ``spar.status``.

``SidePane`` owns the 2s status poll (``QTimer`` -> in-process
``spar.status.build_status(project_dir/'.spar')``): a mid-write state file
must never crash the gui, so a ``StateError``/JSON parse failure is caught
and the *last good* status is kept (the poll simply tries again next tick).

Two pure helpers are unit-tested directly, with no Qt involved:

* :func:`task_rows` — sorts ``status['tasks']`` with the engine's numeric
  task-id order (``t2`` before ``t10``; the tiny key is ported here rather
  than imported from ``spar.exec.loop``, per the task brief) and maps each
  task's status to a pill class plus a ``side -> reviewer`` label (reviewer
  is the other side seen across all tasks; with only one side visible so far
  it falls back to ``"?"``).
* :func:`gate_buttons` — maps a ``pending_gate`` record's ``options`` to a
  list of :class:`ButtonSpec`. Options-driven ONLY: an unknown option is
  logged and skipped rather than guessed at. ``accept`` on a ``consensus``
  gate is special-cased into two buttons (start exec vs. plan-only); every
  other gate's ``accept`` is a single button.

``TaskBoard`` and ``GatePanel`` are the Qt widgets built on top of those pure
functions. ``GatePanel`` disables itself the instant a button is clicked
(double-click safety) and only re-enables on the next ``set_status`` call
that still carries a (new) pending gate.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from spar.gui.theme import TOKENS
from spar.gui.viewers import show_diff_dialog, show_plan_dialog
from spar.state import StateError
from spar.status import build_status

__all__ = [
    "ButtonSpec",
    "gate_buttons",
    "task_rows",
    "TaskBoard",
    "GatePanel",
    "SidePane",
]

log = logging.getLogger(__name__)

_POLL_INTERVAL_MS = 2000

# Ported from spar.exec.loop._task_id_order (kept in sync by hand -- that
# module must not gain a Qt dependency, and the gui must not import exec.loop
# for one tiny sort key -- see task brief).
_TASK_ID_RE = re.compile(r"t(\d+)$")


def _task_id_order(tid: str) -> tuple:
    """Numeric sort key for a task id: ``t2`` before ``t10``."""
    m = _TASK_ID_RE.match(tid)
    return (0, int(m.group(1)), "") if m else (1, 0, tid)


_PILL_OK = {"merged"}
_PILL_WARN = {"review", "implementing", "testing"}
_PILL_MUTED = {"pending", "ready"}


def _pill_for(status: str | None) -> str:
    """Map a task status to a pill class: ``ok`` / ``warn`` / ``muted``."""
    if status in _PILL_OK:
        return "ok"
    if status in _PILL_WARN:
        return "warn"
    return "muted"  # covers pending/ready and any unrecognized status


def _reviewer_for(side: str | None, all_sides: set[str]) -> str:
    """The other side seen across all tasks; ``"?"`` with only one side."""
    if not side:
        return "?"
    others = all_sides - {side}
    if len(others) == 1:
        return next(iter(others))
    return "?"


def task_rows(status: dict) -> list[dict]:
    """Pure projection of ``status['tasks']`` to displayable row dicts.

    Each row: ``task_id``, ``status``, ``pill`` (ok/warn/muted), ``side``,
    ``reviewer``, ``label`` (the ``side -> reviewer`` right-hand string).
    """
    tasks = status.get("tasks") or {}
    all_sides = {t["side"] for t in tasks.values() if t.get("side")}
    rows = []
    for task_id in sorted(tasks, key=_task_id_order):
        task = tasks[task_id]
        side = task.get("side")
        reviewer = _reviewer_for(side, all_sides)
        label = f"{side or '?'} → {reviewer}"
        rows.append(
            {
                "task_id": task_id,
                "status": task.get("status"),
                "pill": _pill_for(task.get("status")),
                "side": side,
                "reviewer": reviewer,
                "label": label,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Gate button mapping (pure)
# ---------------------------------------------------------------------------

_KNOWN_OPTIONS = {"accept", "abort", "extend", "remarks"}


@dataclass(frozen=True)
class ButtonSpec:
    """One gate-decision button.

    ``option`` is the raw gate option this button resolves to ("accept" /
    "abort" / "extend" / "remarks") -- ``GatePanel`` switches on it to build
    the right widget (simple button / spinbox / textarea). ``primary``
    controls emphasis only (e.g. QPushButton default vs. flat). ``auto_exec``
    is only meaningful for ``option == "accept"``: it is threaded straight
    into ``SparRunner.resume(gate_value, auto_exec=...)``.
    """

    label: str
    option: str
    primary: bool = True
    auto_exec: bool = False


def gate_buttons(pending_gate: dict | None) -> list[ButtonSpec]:
    """Map a pending-gate record's ``options`` to ``ButtonSpec``\\ s.

    Options-driven ONLY -- never inferred from the gate ``name`` beyond the
    one sanctioned special case: ``consensus`` + ``accept`` yields TWO
    buttons ("Accept -> start exec" primary / "Accept (tylko plan)"
    secondary) instead of the usual single Accept. An option outside the
    known set (accept/abort/extend/remarks) is logged and skipped rather
    than guessed at.
    """
    if not pending_gate:
        return []
    name = pending_gate.get("name")
    options = pending_gate.get("options") or []

    specs: list[ButtonSpec] = []
    for option in options:
        if option not in _KNOWN_OPTIONS:
            log.warning("gate %r: ignoring unknown option %r", name, option)
            continue
        if option == "accept":
            if name == "consensus":
                specs.append(
                    ButtonSpec(
                        "Accept → start exec", "accept",
                        primary=True, auto_exec=True,
                    )
                )
                specs.append(
                    ButtonSpec("Accept (tylko plan)", "accept", primary=False)
                )
            else:
                specs.append(ButtonSpec("Accept", "accept", primary=True))
        elif option == "abort":
            specs.append(ButtonSpec("Abort", "abort", primary=False))
        elif option == "extend":
            specs.append(ButtonSpec("Dodaj rundy", "extend", primary=False))
        elif option == "remarks":
            specs.append(ButtonSpec("Wyślij remarks", "remarks", primary=False))
    return specs


_SEVERITY_COLORS = {
    "MUST": TOKENS["gate"],
    "USER": TOKENS["warn"],
    "NICE": TOKENS["muted"],
}


def _severity_color(severity: str) -> str:
    return _SEVERITY_COLORS.get(severity, TOKENS["text"])


# ---------------------------------------------------------------------------
# TaskBoard
# ---------------------------------------------------------------------------


class TaskBoard(QWidget):
    """Read-only table of tasks: id, status pill, ``side -> reviewer``."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("taskBoard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 3, self)
        self.table.setObjectName("taskTable")
        self.table.setHorizontalHeaderLabels(["Task", "Status", "Side"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        layout.addWidget(self.table)

        self._rows: list[dict] = []

    def set_status(self, status: dict) -> None:
        """Rebuild rows from a fresh status dict (numeric task-id order)."""
        self._rows = task_rows(status)
        self.table.setRowCount(len(self._rows))
        for i, row in enumerate(self._rows):
            self.table.setItem(i, 0, QTableWidgetItem(row["task_id"]))

            pill_item = QTableWidgetItem(row["status"] or "")
            pill_item.setForeground(Qt.GlobalColor.white)
            pill_item.setData(Qt.ItemDataRole.UserRole, row["pill"])
            color = {
                "ok": TOKENS["ok"],
                "warn": TOKENS["warn"],
                "muted": TOKENS["muted"],
            }[row["pill"]]
            pill_item.setForeground(QColor(color))
            self.table.setItem(i, 1, pill_item)

            side_item = QTableWidgetItem(row["label"])
            side_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(i, 2, side_item)

    @property
    def rows(self) -> list[dict]:
        return list(self._rows)


# ---------------------------------------------------------------------------
# GatePanel
# ---------------------------------------------------------------------------


class GatePanel(QWidget):
    """Renders the currently pending gate (hidden when there is none).

    Buttons come STRICTLY from :func:`gate_buttons`; ``accept``/``abort`` are
    simple push buttons wired straight to ``runner.resume``, ``extend`` gets
    a ``QSpinBox`` (1..99, default 2) + button, ``remarks`` gets a
    ``QPlainTextEdit`` + button (the runner owns the temp file via
    ``resume_with_remarks`` -- this panel never touches files). Any click
    disables the whole panel until the next ``set_pending_gate`` call.
    """

    def __init__(self, runner, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("gatePanel")
        self._runner = runner
        self._pending_gate: dict | None = None

        self._layout = QVBoxLayout(self)
        self._header = QLabel(self)
        self._header.setObjectName("gateHeader")
        self._layout.addWidget(self._header)

        self._context_label = QLabel(self)
        self._context_label.setObjectName("gateContext")
        self._context_label.setWordWrap(True)
        self._context_label.setTextFormat(Qt.TextFormat.RichText)
        self._layout.addWidget(self._context_label)

        self._buttons_row = QWidget(self)
        self._buttons_layout = QHBoxLayout(self._buttons_row)
        self._buttons_layout.setContentsMargins(0, 0, 0, 0)
        self._layout.addWidget(self._buttons_row)

        self._interactive_widgets: list[QWidget] = []
        self.set_pending_gate(None)

    # -- public API --------------------------------------------------
    def set_pending_gate(self, pending_gate: dict | None) -> None:
        """Rebuild the panel for a (possibly new) pending gate.

        Hidden entirely when ``pending_gate`` is ``None``. Re-enables all
        interactive widgets -- this is the only path back to "clickable"
        after a click disabled them.
        """
        self._pending_gate = pending_gate
        self._clear_buttons()

        if not pending_gate:
            self.setVisible(False)
            return

        self.setVisible(True)
        name = pending_gate.get("name")
        context = pending_gate.get("context") or {}
        self._header.setText(f"Gate: {name}")
        self._context_label.setText(self._render_context(name, context))

        for spec in gate_buttons(pending_gate):
            self._add_button(spec)

    # -- context rendering --------------------------------------------------
    def _render_context(self, name: str, context: dict) -> str:
        parts: list[str] = []
        task_id = context.get("task_id")
        rounds = context.get("rounds")
        if task_id is not None:
            parts.append(f"Task <b>{task_id}</b>")
        if rounds is not None:
            parts.append(f"rounds: {rounds}")

        summary = context.get("summary")
        if summary:
            escaped = summary.replace("&", "&amp;").replace("<", "&lt;")
            parts.append(f"<pre>{escaped}</pre>")

        artifact = context.get("artifact")
        if artifact:
            parts.append(f"Plan: <code>{artifact}</code>")

        open_remarks = context.get("open_remarks") or context.get("nice_backlog") or []
        if open_remarks:
            lines = []
            for remark in open_remarks:
                sev = remark.get("severity", "")
                color = _severity_color(sev)
                text = remark.get("text", "")
                author = remark.get("author", "")
                lines.append(
                    f'<span style="color:{color}">[{sev}]</span> '
                    f"({author}) {text}"
                )
            parts.append("<br>".join(lines))

        return "<br>".join(parts)

    # -- button construction --------------------------------------------------
    def _add_button(self, spec: ButtonSpec) -> None:
        if spec.option in ("accept", "abort"):
            button = QPushButton(spec.label, self._buttons_row)
            button.setDefault(spec.primary)
            if spec.option == "accept":
                button.clicked.connect(
                    lambda _=False, s=spec: self._on_accept(s)
                )
            else:
                button.clicked.connect(self._on_abort)
            self._buttons_layout.addWidget(button)
            self._interactive_widgets.append(button)

        elif spec.option == "extend":
            spin = QSpinBox(self._buttons_row)
            spin.setObjectName("extendRounds")
            spin.setRange(1, 99)
            spin.setValue(2)
            self._buttons_layout.addWidget(spin)

            button = QPushButton(spec.label, self._buttons_row)
            button.clicked.connect(lambda _=False, s=spin: self._on_extend(s))
            self._buttons_layout.addWidget(button)
            self._interactive_widgets.extend([spin, button])

        elif spec.option == "remarks":
            container = QWidget(self._buttons_row)
            v = QVBoxLayout(container)
            v.setContentsMargins(0, 0, 0, 0)
            text_edit = QPlainTextEdit(container)
            text_edit.setObjectName("remarksText")
            text_edit.setPlaceholderText("Uwagi, jedna na linijkę…")
            v.addWidget(text_edit)

            button = QPushButton(spec.label, container)
            button.clicked.connect(lambda _=False, t=text_edit: self._on_remarks(t))
            v.addWidget(button)

            self._buttons_layout.addWidget(container)
            self._interactive_widgets.extend([text_edit, button])

    def _clear_buttons(self) -> None:
        while self._buttons_layout.count():
            item = self._buttons_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._interactive_widgets = []

    # -- click handlers --------------------------------------------------
    def _disable_all(self) -> None:
        for widget in self._interactive_widgets:
            widget.setEnabled(False)

    def _on_accept(self, spec: ButtonSpec) -> None:
        self._disable_all()
        self._runner.resume("accept", auto_exec=spec.auto_exec)

    def _on_abort(self) -> None:
        reply = QMessageBox.question(
            self,
            "Abort",
            "Na pewno przerwać bieżący przebieg?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._disable_all()
        self._runner.resume("abort")

    def _on_extend(self, spin: QSpinBox) -> None:
        self._disable_all()
        self._runner.resume(f"extend:{spin.value()}")

    def _on_remarks(self, text_edit: QPlainTextEdit) -> None:
        self._disable_all()
        self._runner.resume_with_remarks(text_edit.toPlainText())


# ---------------------------------------------------------------------------
# SidePane: wires TaskBoard + GatePanel to the 2s status poll
# ---------------------------------------------------------------------------


class SidePane(QWidget):
    """Right pane: status poll -> TaskBoard + GatePanel + Plan/Diff viewers."""

    #: Emitted at the end of every :meth:`refresh` with the (possibly stale,
    #: on a read failure) status dict -- ``MainWindow`` uses it to gate the
    #: Plan/Diff toolbar buttons on ``artifact``/``branches`` presence.
    status_changed = Signal(dict)

    def __init__(self, project_dir: "str | Path", runner, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("sidePane")
        self.project_dir = Path(project_dir)
        self._runner = runner
        self._last_good_status: dict = {
            "phase": None,
            "pending_gate": None,
            "tasks": {},
            "artifact": None,
            "branches": None,
        }

        layout = QVBoxLayout(self)

        self.task_board = TaskBoard(self)
        layout.addWidget(self.task_board)

        self.gate_panel = GatePanel(self._runner, self)
        layout.addWidget(self.gate_panel)

        layout.addStretch(1)

        self._poll = QTimer(self)
        self._poll.setInterval(_POLL_INTERVAL_MS)
        self._poll.timeout.connect(self.refresh)
        self._poll.start()
        self.refresh()

    def refresh(self) -> None:
        """Poll ``spar.status.build_status`` in-process; keep last good status
        on any failure (e.g. a state file mid-write)."""
        try:
            status = build_status(self.project_dir / ".spar")
        except (StateError, ValueError, OSError):
            status = self._last_good_status
        except Exception:  # noqa: BLE001 -- never let a bad status crash the gui
            status = self._last_good_status
        else:
            self._last_good_status = status

        self.task_board.set_status(status)
        self.gate_panel.set_pending_gate(status.get("pending_gate"))
        self.status_changed.emit(status)

    def show_plan(self) -> None:
        artifact = self._last_good_status.get("artifact")
        show_plan_dialog(self, artifact)

    def show_diff(self) -> None:
        branches = self._last_good_status.get("branches")
        show_diff_dialog(self, self.project_dir, branches)
