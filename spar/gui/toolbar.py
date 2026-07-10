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

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from spar.gui.runner import RunnerState

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


class NewDebateDialog(QDialog):
    """Collect the parameters for a fresh debate.

    Fields: multiline task text, comma-separated ``sides`` (default
    ``claude,codex``), ``first`` side (default ``claude``) and a ``tasks``
    checkbox (default ON — require a machine-parsable ``## Tasks`` section so
    the plan can bridge into ``spar exec``).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nowa debata")

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.task_edit = QPlainTextEdit(self)
        self.task_edit.setObjectName("taskText")
        self.task_edit.setPlaceholderText("Opis zadania dla debaty…")
        form.addRow("Zadanie", self.task_edit)

        self.sides_edit = QLineEdit("claude,codex", self)
        self.sides_edit.setObjectName("sides")
        form.addRow("Strony", self.sides_edit)

        self.first_edit = QLineEdit("claude", self)
        self.first_edit.setObjectName("first")
        form.addRow("Pierwszy", self.first_edit)

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

    def values(self) -> dict:
        """Return the dialog's fields as kwargs for ``SparRunner.start_debate``."""
        return {
            "task_text": self.task_edit.toPlainText(),
            "sides": self.sides_edit.text().strip(),
            "first": self.first_edit.text().strip(),
            "tasks": self.tasks_check.isChecked(),
        }
