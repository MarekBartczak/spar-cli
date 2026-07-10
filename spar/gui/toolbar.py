"""Toolbar state machine + new-debate dialog for the spar gui.

``enablement_for`` is the pure mapping from a :class:`RunnerState` (plus the
current status dict) to which of the wired toolbar actions are enabled. It is
kept free of Qt so it can be unit-tested directly; :func:`apply_state` is the
thin Qt adapter that pushes the result onto a live toolbar.

Only the four *wired* actions are driven here — ``Nowa debata…``, ``Start
exec``, ``Wznów`` and ``Stop``. ``Plan`` / ``Diff`` are read-only views built
in a later task and stay disabled.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from spar.config import ConfigError, load_config
from spar.gui.grill_dialog import GrillDialog
from spar.gui.runner import RunnerState

# Sides shown/ordered by default when no project config is readable, or when
# the configured sides don't include these -- keeps the historical
# claude-then-codex ordering (task brief, fix 1).
_DEFAULT_SIDE_ORDER = ("claude", "codex")

__all__ = [
    "NEW_DEBATE",
    "START_EXEC",
    "RESUME",
    "STOP",
    "enablement_for",
    "apply_state",
    "NewDebateDialog",
]

# Toolbar action labels (must match the placeholder labels created in app.py).
NEW_DEBATE = "Nowa debata…"
START_EXEC = "Start exec"
RESUME = "Wznów"
STOP = "Stop"


def enablement_for(state: RunnerState, status: dict | None = None) -> dict[str, bool]:
    """Return ``{label: enabled}`` for the four wired toolbar actions.

    Mirrors the mockup's state machine:

    * ``IDLE``          — only *Nowa debata* (fresh dir, nothing to resume)
    * ``RUNNING``       — only *Stop* (SIGINT the live child)
    * ``GATE_PENDING``  — *Wznów* + *Nowa debata* (resolve gate or restart)
    * ``RESUMABLE``     — *Wznów* + *Nowa debata*
    * ``ABORTED``       — *Wznów* (re-pends the gate) + *Nowa debata*
    * ``DONE``          — *Nowa debata*; *Start exec* too when a finished
      debate (phase debate/none) can still bridge into an execution
    * ``ERROR``         — *Nowa debata* + *Wznów* (retry)
    * ``LOCKED``        — nothing (read-only observation until the lock frees)
    """
    status = status or {}
    enabled = {NEW_DEBATE: False, START_EXEC: False, RESUME: False, STOP: False}

    if state == RunnerState.IDLE:
        enabled[NEW_DEBATE] = True
    elif state == RunnerState.RUNNING:
        enabled[STOP] = True
    elif state in (RunnerState.GATE_PENDING, RunnerState.RESUMABLE, RunnerState.ABORTED):
        enabled[RESUME] = True
        enabled[NEW_DEBATE] = True
        # Bridge survives interruptions/refusals AND gui restarts: an
        # accepted debate left an artifact but no exec run yet — Start exec
        # must stay reachable (live finding: after a dirty-tree refusal the
        # button was dead with no way back).
        if (
            state == RunnerState.RESUMABLE
            and status.get("phase") in (None, "debate")
            and status.get("artifact")
        ):
            enabled[START_EXEC] = True
    elif state == RunnerState.DONE:
        enabled[NEW_DEBATE] = True
        # A finished debate (still on the debate/fresh phase) can launch exec;
        # a finished execution (phase == "done") cannot.
        if status.get("phase") in (None, "debate"):
            enabled[START_EXEC] = True
    elif state == RunnerState.ERROR:
        enabled[NEW_DEBATE] = True
        enabled[RESUME] = True
    elif state == RunnerState.LOCKED:
        pass  # everything stays disabled

    return enabled


def apply_state(toolbar, state: RunnerState, status: dict | None = None) -> None:
    """Push :func:`enablement_for` onto a live toolbar's actions."""
    enabled = enablement_for(state, status)
    for label, is_enabled in enabled.items():
        action = toolbar.actions_by_label.get(label)
        if action is not None:
            action.setEnabled(is_enabled)


def _configured_side_order(project_dir: "str | Path | None") -> list[str]:
    """The project's configured side names, ``claude``/``codex`` first.

    Falls back to the historical ``["claude", "codex"]`` when the project has
    no readable config (fresh dir, malformed config.toml, etc.) -- the dialog
    must never fail to open over a config problem.
    """
    configured: list[str] = list(_DEFAULT_SIDE_ORDER)
    if project_dir is not None:
        try:
            config = load_config(Path(project_dir))
            configured = list(config.sides.keys())
        except Exception:
            configured = list(_DEFAULT_SIDE_ORDER)
    ordered = [s for s in _DEFAULT_SIDE_ORDER if s in configured]
    ordered += [s for s in configured if s not in ordered]
    return ordered


def _grill_availability(
    project_dir: "str | Path | None",
) -> tuple["object | None", int, "str | None"]:
    """Resolve the claude ``SideConfig``/timeout for grilling, or a disable reason.

    Returns ``(side_cfg, timeout_sec, disabled_reason)`` -- ``disabled_reason``
    is ``None`` when grilling is available. The button is disabled (with a
    tooltip explaining why) when the project's config fails to load or the
    ``claude`` side is missing/not backed by the ``claude`` adapter (review
    #7 -- a missing side is unreachable with the current defaults, which
    always seed claude/codex).
    """
    if project_dir is None:
        return None, 900, "Brak katalogu projektu"
    try:
        config = load_config(Path(project_dir))
    except ConfigError as exc:
        return None, 900, f"Nie można wczytać konfiguracji: {exc}"
    side = config.sides.get("claude")
    if side is None or side.adapter != "claude":
        return None, 900, "Strona „claude” nie jest skonfigurowana z adapterem claude"
    return side, config.debate.turn_timeout_sec, None


class NewDebateDialog(QDialog):
    """Collect the parameters for a fresh debate.

    Fields: multiline task text, one checkbox per side configured in the
    project (``spar.config.load_config(project_dir).sides``, default ALL
    checked, ``claude``/``codex`` ordered first -- fix 1), a ``first`` combo
    populated from the currently-CHECKED sides (updates live as checkboxes
    toggle) and a ``tasks`` checkbox (default ON — require a machine-parsable
    ``## Tasks`` section so the plan can bridge into ``spar exec``).
    """

    def __init__(self, project_dir: "str | Path | None" = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nowa debata")
        # The live session's dialog was resized to roughly this by hand --
        # the previous Qt-picked default was cramped (task brief, fix 2).
        self.resize(760, 560)

        self._side_order = _configured_side_order(project_dir)
        self._project_dir = Path(project_dir) if project_dir is not None else None
        self._grill_side_cfg, self._grill_timeout_sec, grill_disabled_reason = (
            _grill_availability(project_dir)
        )

        layout = QVBoxLayout(self)
        form = QFormLayout()

        task_widget = QWidget(self)
        task_layout = QVBoxLayout(task_widget)
        task_layout.setContentsMargins(0, 0, 0, 0)

        self.task_edit = QPlainTextEdit(self)
        self.task_edit.setObjectName("taskText")
        self.task_edit.setPlaceholderText("Opis zadania dla debaty…")
        task_layout.addWidget(self.task_edit)

        self.grill_button = QPushButton("Grilluj z modelem…", task_widget)
        self.grill_button.setObjectName("grillButton")
        self.grill_button.clicked.connect(self._on_grill)
        if grill_disabled_reason is not None:
            self.grill_button.setEnabled(False)
            self.grill_button.setToolTip(grill_disabled_reason)
        task_layout.addWidget(self.grill_button)

        form.addRow("Zadanie", task_widget)

        sides_widget = QWidget(self)
        sides_layout = QHBoxLayout(sides_widget)
        sides_layout.setContentsMargins(0, 0, 0, 0)
        self.side_checks: dict[str, QCheckBox] = {}
        for side in self._side_order:
            check = QCheckBox(side, sides_widget)
            check.setObjectName(f"side_{side}")
            check.setChecked(True)
            check.toggled.connect(self._refresh_first_combo)
            sides_layout.addWidget(check)
            self.side_checks[side] = check
        sides_layout.addStretch(1)
        form.addRow("Strony", sides_widget)

        self.first_combo = QComboBox(self)
        self.first_combo.setObjectName("first")
        form.addRow("Pierwszy", self.first_combo)
        self._refresh_first_combo()

        self.tasks_check = QCheckBox("Wymagaj sekcji ## Tasks", self)
        self.tasks_check.setObjectName("tasks")
        self.tasks_check.setChecked(True)
        form.addRow("", self.tasks_check)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_grill(self) -> None:
        """Open ``GrillDialog`` seeded with the current task draft.

        On accept, the requirements content REPLACES the task field.
        """
        dialog = GrillDialog(
            self._project_dir,
            self._grill_side_cfg,
            self._grill_timeout_sec,
            draft=self.task_edit.toPlainText(),
            parent=self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.result_requirements:
            self.task_edit.setPlainText(dialog.result_requirements)

    def _checked_sides(self) -> list[str]:
        return [side for side in self._side_order if self.side_checks[side].isChecked()]

    def _refresh_first_combo(self, *_args) -> None:
        """Repopulate ``first_combo`` from the checked sides, preserving the
        current pick when it's still checked."""
        current = self.first_combo.currentText()
        checked = self._checked_sides()
        self.first_combo.blockSignals(True)
        self.first_combo.clear()
        self.first_combo.addItems(checked)
        if current in checked:
            self.first_combo.setCurrentText(current)
        self.first_combo.blockSignals(False)

    def values(self) -> dict:
        """Return the dialog's fields as kwargs for ``SparRunner.start_debate``."""
        return {
            "task_text": self.task_edit.toPlainText(),
            "sides": ",".join(self._checked_sides()),
            "first": self.first_combo.currentText(),
            "tasks": self.tasks_check.isChecked(),
        }
