"""Tests for ``spar.gui.sidepane``: task board, dynamic gate panel.

Skipped entirely on interpreters without the optional ``gui`` extra.

Three layers:

* ``gate_buttons`` -- a *pure* function, exercised against all four gate
  shapes the engine produces (consensus / review_rounds / final_merge /
  rounds_exhausted) plus option-subsetting and the unknown-option case;
* ``task_rows`` -- pure projection of a crafted ``status['tasks']`` dict,
  checked for numeric ordering and pill/reviewer mapping;
* ``GatePanel`` -- Qt wiring against a mocked runner (no real SparRunner/
  QProcess involved).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from spar.gui.sidepane import gate_buttons, task_rows, GatePanel, TaskBoard

_LEFT = Qt.MouseButton.LeftButton


# ---------------------------------------------------------------------------
# gate_buttons -- pure
# ---------------------------------------------------------------------------
class TestGateButtons:
    def test_none_pending_gate_yields_no_buttons(self):
        assert gate_buttons(None) == []

    def test_consensus_accept_yields_two_variants(self):
        gate = {
            "name": "consensus",
            "options": ["accept", "remarks", "abort"],
            "context": {},
        }
        specs = gate_buttons(gate)
        accepts = [s for s in specs if s.option == "accept"]
        assert len(accepts) == 2

        primary = next(s for s in accepts if s.auto_exec)
        assert primary.primary is True
        assert "start exec" in primary.label.lower()

        secondary = next(s for s in accepts if not s.auto_exec)
        assert secondary.primary is False
        assert "plan" in secondary.label.lower()

        remarks_spec = next(s for s in specs if s.option == "remarks")
        assert remarks_spec.label

        abort_spec = next(s for s in specs if s.option == "abort")
        assert abort_spec.label

    def test_review_rounds_accept_is_single_button(self):
        gate = {
            "name": "review_rounds",
            "options": ["accept", "extend", "abort"],
            "context": {"task_id": "t1", "rounds": 3, "open_remarks": []},
        }
        specs = gate_buttons(gate)
        accepts = [s for s in specs if s.option == "accept"]
        assert len(accepts) == 1
        assert accepts[0].auto_exec is False
        options_seen = [s.option for s in specs]
        assert options_seen == ["accept", "extend", "abort"]

    def test_final_merge_accept_abort_only(self):
        gate = {
            "name": "final_merge",
            "options": ["accept", "abort"],
            "context": {"summary": "diffstat here"},
        }
        specs = gate_buttons(gate)
        assert [s.option for s in specs] == ["accept", "abort"]
        assert len(specs) == 2

    def test_rounds_exhausted_accept_extend_abort(self):
        gate = {
            "name": "rounds_exhausted",
            "options": ["accept", "extend", "abort"],
            "context": {"artifact": "/tmp/artifact.md", "open_remarks": []},
        }
        specs = gate_buttons(gate)
        assert [s.option for s in specs] == ["accept", "extend", "abort"]
        accepts = [s for s in specs if s.option == "accept"]
        assert len(accepts) == 1
        assert accepts[0].auto_exec is False

    def test_options_subsetting_respected(self):
        # A gate exposing only "accept" gets only an Accept button, even
        # though the gate name is one that normally also has abort/extend.
        gate = {"name": "final_merge", "options": ["accept"], "context": {}}
        specs = gate_buttons(gate)
        assert len(specs) == 1
        assert specs[0].option == "accept"

    def test_unknown_option_is_ignored(self, caplog):
        gate = {"name": "final_merge", "options": ["accept", "mystery"], "context": {}}
        with caplog.at_level("WARNING"):
            specs = gate_buttons(gate)
        assert [s.option for s in specs] == ["accept"]
        assert any("mystery" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# task_rows -- pure
# ---------------------------------------------------------------------------
class TestTaskRows:
    def test_numeric_task_order(self):
        status = {
            "tasks": {
                "t10": {"status": "pending", "side": "claude", "model": "sonnet"},
                "t2": {"status": "pending", "side": "claude", "model": "sonnet"},
                "t1": {"status": "pending", "side": "claude", "model": "sonnet"},
            }
        }
        rows = task_rows(status)
        assert [r["task_id"] for r in rows] == ["t1", "t2", "t10"]

    def test_pill_mapping(self):
        status = {
            "tasks": {
                "t1": {"status": "merged", "side": "claude"},
                "t2": {"status": "review", "side": "claude"},
                "t3": {"status": "implementing", "side": "claude"},
                "t4": {"status": "testing", "side": "claude"},
                "t5": {"status": "pending", "side": "claude"},
                "t6": {"status": "ready", "side": "claude"},
            }
        }
        rows = {r["task_id"]: r for r in task_rows(status)}
        assert rows["t1"]["pill"] == "ok"
        assert rows["t2"]["pill"] == "warn"
        assert rows["t3"]["pill"] == "warn"
        assert rows["t4"]["pill"] == "warn"
        assert rows["t5"]["pill"] == "muted"
        assert rows["t6"]["pill"] == "muted"

    def test_reviewer_is_other_side_seen(self):
        status = {
            "tasks": {
                "t1": {"status": "pending", "side": "claude"},
                "t2": {"status": "pending", "side": "codex"},
            }
        }
        rows = {r["task_id"]: r for r in task_rows(status)}
        assert rows["t1"]["reviewer"] == "codex"
        assert rows["t1"]["label"] == "claude → codex"
        assert rows["t2"]["reviewer"] == "claude"

    def test_reviewer_falls_back_to_unknown_with_one_side(self):
        status = {"tasks": {"t1": {"status": "pending", "side": "claude"}}}
        rows = task_rows(status)
        assert rows[0]["reviewer"] == "?"
        assert rows[0]["label"] == "claude → ?"

    def test_empty_tasks_yields_no_rows(self):
        assert task_rows({"tasks": {}}) == []
        assert task_rows({}) == []


# ---------------------------------------------------------------------------
# TaskBoard -- Qt widget smoke test (ordering/pills flow through)
# ---------------------------------------------------------------------------
class TestTaskBoard:
    def test_set_status_populates_rows_in_order(self, qtbot):
        board = TaskBoard()
        qtbot.addWidget(board)
        status = {
            "tasks": {
                "t10": {"status": "merged", "side": "claude"},
                "t2": {"status": "pending", "side": "codex"},
            }
        }
        board.set_status(status)
        assert board.table.rowCount() == 2
        assert board.table.item(0, 0).text() == "t2"
        assert board.table.item(1, 0).text() == "t10"


# ---------------------------------------------------------------------------
# GatePanel -- Qt wiring against a mocked runner
# ---------------------------------------------------------------------------
class TestGatePanel:
    def test_hidden_when_no_pending_gate(self, qtbot):
        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(None)
        assert panel.isVisible() is False

    def test_visible_with_pending_gate(self, qtbot):
        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {"name": "final_merge", "options": ["accept", "abort"], "context": {"summary": "x"}}
        )
        assert panel.isVisible() is True

    def test_consensus_primary_accept_starts_exec(self, qtbot):
        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {
                "name": "consensus",
                "options": ["accept", "remarks", "abort"],
                "context": {"artifact": "/tmp/a.md", "nice_backlog": []},
            }
        )

        primary_button = next(
            w for w in panel._interactive_widgets
            if hasattr(w, "text") and "start exec" in w.text().lower()
        )
        qtbot.mouseClick(primary_button, _LEFT)

        runner.resume.assert_called_once_with("accept", auto_exec=True)

    def test_consensus_secondary_accept_plan_only(self, qtbot):
        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {
                "name": "consensus",
                "options": ["accept", "remarks", "abort"],
                "context": {"artifact": "/tmp/a.md", "nice_backlog": []},
            }
        )

        secondary_button = next(
            w for w in panel._interactive_widgets
            if hasattr(w, "text") and "tylko plan" in w.text().lower()
        )
        qtbot.mouseClick(secondary_button, _LEFT)

        runner.resume.assert_called_once_with("accept", auto_exec=False)

    def test_remarks_button_calls_resume_with_remarks_raw_text(self, qtbot):
        from PySide6.QtWidgets import QPlainTextEdit, QPushButton

        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {
                "name": "consensus",
                "options": ["accept", "remarks", "abort"],
                "context": {"artifact": "/tmp/a.md", "nice_backlog": []},
            }
        )

        text_edit = next(
            w for w in panel._interactive_widgets if isinstance(w, QPlainTextEdit)
        )
        text_edit.setPlainText("please fix the widget layout")
        button = next(
            w for w in panel._interactive_widgets
            if isinstance(w, QPushButton) and "remarks" in w.text().lower()
        )
        qtbot.mouseClick(button, _LEFT)

        runner.resume_with_remarks.assert_called_once_with("please fix the widget layout")

    def test_extend_button_uses_spinbox_value(self, qtbot):
        from PySide6.QtWidgets import QPushButton, QSpinBox

        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {
                "name": "review_rounds",
                "options": ["accept", "extend", "abort"],
                "context": {"task_id": "t1", "rounds": 2, "open_remarks": []},
            }
        )

        spin = next(w for w in panel._interactive_widgets if isinstance(w, QSpinBox))
        assert spin.minimum() == 1
        assert spin.maximum() == 99
        assert spin.value() == 2
        spin.setValue(5)

        button = next(
            w for w in panel._interactive_widgets
            if isinstance(w, QPushButton) and "rundy" in w.text().lower()
        )
        qtbot.mouseClick(button, _LEFT)

        runner.resume.assert_called_once_with("extend:5")

    def test_abort_button_requires_confirmation(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QMessageBox, QPushButton

        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {"name": "final_merge", "options": ["accept", "abort"], "context": {"summary": "x"}}
        )

        # Decline -> resume must NOT be called.
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
        )
        abort_button = next(
            w for w in panel._interactive_widgets
            if isinstance(w, QPushButton) and w.text().lower() == "abort"
        )
        qtbot.mouseClick(abort_button, _LEFT)
        runner.resume.assert_not_called()

        # Accept the confirm -> resume("abort").
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )
        qtbot.mouseClick(abort_button, _LEFT)
        runner.resume.assert_called_once_with("abort")

    def test_button_disables_after_click_until_next_poll(self, qtbot):
        from PySide6.QtWidgets import QPushButton

        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {"name": "final_merge", "options": ["accept", "abort"], "context": {"summary": "x"}}
        )
        accept_button = next(
            w for w in panel._interactive_widgets
            if isinstance(w, QPushButton) and w.text().lower() == "accept"
        )
        qtbot.mouseClick(accept_button, _LEFT)
        assert accept_button.isEnabled() is False

        # Next poll re-pending the SAME gate rebuilds enabled buttons.
        panel.set_pending_gate(
            {"name": "final_merge", "options": ["accept", "abort"], "context": {"summary": "x"}}
        )
        new_accept_button = next(
            w for w in panel._interactive_widgets
            if isinstance(w, QPushButton) and w.text().lower() == "accept"
        )
        assert new_accept_button.isEnabled() is True
