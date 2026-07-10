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
from PySide6.QtWidgets import QHBoxLayout

from spar.gui.sidepane import gate_buttons, task_rows, GatePanel, SidePane, TaskBoard, TaskPanel

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

    def test_review_rounds_with_fix_option(self):
        # A per-task-test escalation exposes a "fix" option -> a "fix" button.
        gate = {
            "name": "review_rounds",
            "options": ["accept", "extend", "fix", "abort"],
            "context": {"task_id": "t1", "rounds": 0, "command": "python -m x"},
        }
        specs = gate_buttons(gate)
        options_seen = [s.option for s in specs]
        assert options_seen == ["accept", "extend", "fix", "abort"]
        fix_spec = next(s for s in specs if s.option == "fix")
        assert fix_spec.label

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

    def test_reviewer_resolved_from_configured_side_order_even_with_one_side_seen(self):
        # Fix 6b: with only "codex" tasks seen so far, the reviewer must
        # still resolve from the project's configured side order (the other
        # configured side) instead of falling back to "?" until a "claude"
        # task happens to show up too.
        status = {"tasks": {"t1": {"status": "pending", "side": "codex"}}}
        rows = task_rows(status, side_order=["claude", "codex"])
        assert rows[0]["reviewer"] == "claude"
        assert rows[0]["label"] == "codex → claude"

    def test_reviewer_with_side_order_and_more_than_two_sides_is_unknown(self):
        status = {"tasks": {"t1": {"status": "pending", "side": "codex"}}}
        rows = task_rows(status, side_order=["claude", "codex", "gemini"])
        assert rows[0]["reviewer"] == "?"

    def test_reviewer_without_side_order_falls_back_to_seen_sides(self):
        # Backward-compatible default: no side_order given behaves exactly
        # like before (inferred from tasks seen so far).
        status = {"tasks": {"t1": {"status": "pending", "side": "codex"}}}
        rows = task_rows(status)
        assert rows[0]["reviewer"] == "?"

    def test_label_includes_models_when_present(self):
        # Fix 5: "codex·gpt-5.4 -> claude·opus" using the task's implementer
        # model and the reviewer's review_model.
        status = {
            "tasks": {
                "t1": {
                    "status": "pending",
                    "side": "codex",
                    "model": "gpt-5.4",
                    "review_model": "opus",
                }
            }
        }
        rows = task_rows(status, side_order=["claude", "codex"])
        assert rows[0]["label"] == "codex·gpt-5.4 → claude·opus"
        assert rows[0]["model"] == "gpt-5.4"
        assert rows[0]["review_model"] == "opus"

    def test_label_falls_back_to_plain_sides_without_models(self):
        status = {"tasks": {"t1": {"status": "pending", "side": "codex"}}}
        rows = task_rows(status, side_order=["claude", "codex"])
        assert rows[0]["label"] == "codex → claude"
        assert rows[0]["model"] is None
        assert rows[0]["review_model"] is None

    def test_label_with_only_implementer_model_known(self):
        status = {
            "tasks": {"t1": {"status": "pending", "side": "codex", "model": "gpt-5.4"}}
        }
        rows = task_rows(status, side_order=["claude", "codex"])
        assert rows[0]["label"] == "codex·gpt-5.4 → claude"


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

    def test_set_status_with_side_order_resolves_reviewer_from_config(self, qtbot):
        board = TaskBoard()
        qtbot.addWidget(board)
        status = {"tasks": {"t1": {"status": "pending", "side": "codex"}}}
        board.set_status(status, side_order=["claude", "codex"])
        assert board.rows[0]["reviewer"] == "claude"
        # Side column item (index 2) shows the "side -> reviewer" label.
        assert board.table.item(0, 2).text() == "codex → claude"

    def test_set_status_with_models_shows_models_in_side_column(self, qtbot):
        # Fix 5: "codex·gpt-5.4 -> claude·opus".
        board = TaskBoard()
        qtbot.addWidget(board)
        status = {
            "tasks": {
                "t1": {
                    "status": "pending",
                    "side": "codex",
                    "model": "gpt-5.4",
                    "review_model": "opus",
                }
            }
        }
        board.set_status(status, side_order=["claude", "codex"])
        assert board.table.item(0, 2).text() == "codex·gpt-5.4 → claude·opus"


# ---------------------------------------------------------------------------
# TaskBoard -- debate placeholder (fix 3)
# ---------------------------------------------------------------------------
class TestTaskBoardPlaceholder:
    def test_placeholder_visible_by_default_before_any_status(self, qtbot):
        board = TaskBoard()
        qtbot.addWidget(board)
        board.show()

        assert board.placeholder.isVisible() is True
        assert board.table.isVisible() is False
        assert board.table.horizontalHeader().isVisible() is False

    def test_placeholder_visible_during_debate_with_no_tasks(self, qtbot):
        board = TaskBoard()
        qtbot.addWidget(board)
        board.show()

        board.set_status({"tasks": {}, "phase": "debate"})

        assert board.placeholder.isVisible() is True
        assert board.table.isVisible() is False
        assert board.table.horizontalHeader().isVisible() is False

        board.set_status({"tasks": {}, "phase": None})
        assert board.placeholder.isVisible() is True

    def test_placeholder_hidden_once_a_real_task_appears(self, qtbot):
        board = TaskBoard()
        qtbot.addWidget(board)
        board.show()

        board.set_status({"tasks": {}, "phase": "debate"})
        board.set_status(
            {
                "tasks": {"t1": {"status": "pending", "side": "claude"}},
                "phase": "execution",
            }
        )

        assert board.placeholder.isVisible() is False
        assert board.table.isVisible() is True
        assert board.table.horizontalHeader().isVisible() is True


# ---------------------------------------------------------------------------
# TaskPanel -- collapsible "Zadanie" section (fix 5)
# ---------------------------------------------------------------------------
class TestTaskPanel:
    def test_hidden_when_no_task_text(self, qtbot):
        panel = TaskPanel()
        qtbot.addWidget(panel)
        panel.set_text(None)
        assert panel.isVisible() is False

    def test_visible_and_shows_text_when_set(self, qtbot):
        panel = TaskPanel()
        qtbot.addWidget(panel)
        panel.set_text("Build a widget that does X.")
        assert panel.isVisible() is True
        assert panel.label.text() == "Build a widget that does X."

    def test_collapsed_by_default(self, qtbot):
        panel = TaskPanel()
        qtbot.addWidget(panel)
        panel.set_text("some task")
        assert panel.toggle_button.isChecked() is False
        assert panel.scroll.isVisible() is False

    def test_toggle_expands_and_collapses(self, qtbot):
        panel = TaskPanel()
        qtbot.addWidget(panel)
        panel.set_text("some task")

        panel.toggle_button.setChecked(True)
        assert panel.scroll.isVisible() is True

        panel.toggle_button.setChecked(False)
        assert panel.scroll.isVisible() is False

    def test_max_expanded_height_is_capped(self, qtbot):
        panel = TaskPanel()
        qtbot.addWidget(panel)
        assert panel.scroll.maximumHeight() <= 160


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

    def test_fix_button_prompts_and_resumes(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QInputDialog, QPushButton

        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {
                "name": "review_rounds",
                "options": ["accept", "extend", "fix", "abort"],
                "context": {
                    "task_id": "t1",
                    "rounds": 0,
                    "command": "python -m py_compile todo.py",
                },
            }
        )

        fix_button = next(
            w for w in panel._interactive_widgets
            if isinstance(w, QPushButton) and "popraw" in w.text().lower()
        )

        # Cancelled dialog -> resume NOT called, gate stays actionable.
        monkeypatch.setattr(
            QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False))
        )
        qtbot.mouseClick(fix_button, _LEFT)
        runner.resume.assert_not_called()

        # Prefilled with the current command; entering a new one -> fix:<cmd>.
        captured = {}

        def fake_get_text(parent, title, label, echo, text):
            captured["prefill"] = text
            return ("python3 -m py_compile todo.py", True)

        monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_get_text))
        qtbot.mouseClick(fix_button, _LEFT)
        assert captured["prefill"] == "python -m py_compile todo.py"
        runner.resume.assert_called_once_with("fix:python3 -m py_compile todo.py")

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


# ---------------------------------------------------------------------------
# GatePanel layout (fix 6a) -- widget hierarchy, not pixels
# ---------------------------------------------------------------------------
class TestGatePanelLayout:
    def test_remarks_editor_hidden_when_gate_has_no_remarks_option(self, qtbot):
        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {"name": "final_merge", "options": ["accept", "abort"], "context": {}}
        )
        assert panel._remarks_container.isVisible() is False

    def test_remarks_editor_visible_full_width_when_option_present(self, qtbot):
        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {
                "name": "consensus",
                "options": ["accept", "remarks", "abort"],
                "context": {},
            }
        )
        assert panel._remarks_container.isVisible() is True
        # The remarks editor + its send button live in the SAME container,
        # not inside the single button row.
        assert panel._remarks_text.parent() is panel._remarks_container
        assert panel._remarks_send_button.parent() is panel._remarks_container

    def test_single_button_row_holds_accept_extend_and_abort(self, qtbot):
        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {
                "name": "review_rounds",
                "options": ["accept", "extend", "abort"],
                "context": {"task_id": "t1", "rounds": 2},
            }
        )
        # Exactly one QHBoxLayout button row; every accept/extend/abort
        # widget lives inside it (never scattered across a grid).
        assert isinstance(panel._buttons_layout, QHBoxLayout)
        row_widgets = [
            panel._buttons_layout.itemAt(i).widget()
            for i in range(panel._buttons_layout.count())
            if panel._buttons_layout.itemAt(i).widget() is not None
        ]
        assert len(row_widgets) >= 3  # accept, spinbox, extend button, abort

    def test_remarks_send_button_not_duplicated_in_button_row(self, qtbot):
        from PySide6.QtWidgets import QPushButton

        runner = MagicMock()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate(
            {
                "name": "consensus",
                "options": ["accept", "remarks", "abort"],
                "context": {},
            }
        )
        row_widgets = [
            panel._buttons_layout.itemAt(i).widget()
            for i in range(panel._buttons_layout.count())
            if panel._buttons_layout.itemAt(i).widget() is not None
        ]
        remarks_buttons_in_row = [
            w for w in row_widgets
            if isinstance(w, QPushButton) and "remarks" in w.text().lower()
        ]
        assert remarks_buttons_in_row == []


# ---------------------------------------------------------------------------
# SidePane -- task panel wiring (fix 5) + reviewer resolution (fix 6b)
# ---------------------------------------------------------------------------
class TestSidePaneLayout:
    def test_has_minimum_width_of_320(self, qtbot, tmp_path):
        (tmp_path / ".spar").mkdir()
        pane = SidePane(tmp_path, MagicMock())
        qtbot.addWidget(pane)

        assert pane.minimumWidth() >= 320


class TestSidePaneTaskPanel:
    def test_task_panel_hidden_when_no_task_md(self, qtbot, tmp_path):
        (tmp_path / ".spar").mkdir()
        pane = SidePane(tmp_path, MagicMock())
        qtbot.addWidget(pane)

        assert pane.task_panel.isVisible() is False

    def test_task_panel_shows_task_md_content(self, qtbot, tmp_path):
        spar_dir = tmp_path / ".spar"
        spar_dir.mkdir()
        (spar_dir / "task.md").write_text("Build the widget.", encoding="utf-8")

        pane = SidePane(tmp_path, MagicMock())
        qtbot.addWidget(pane)
        pane.show()

        assert pane.task_panel.isVisible() is True
        assert pane.task_panel.label.text() == "Build the widget."

    def test_task_panel_updates_on_refresh(self, qtbot, tmp_path):
        spar_dir = tmp_path / ".spar"
        spar_dir.mkdir()
        pane = SidePane(tmp_path, MagicMock())
        qtbot.addWidget(pane)
        pane.show()
        assert pane.task_panel.isVisible() is False

        (spar_dir / "task.md").write_text("Now it exists.", encoding="utf-8")
        pane.refresh()

        assert pane.task_panel.isVisible() is True
        assert pane.task_panel.label.text() == "Now it exists."

    def test_task_board_reviewer_uses_configured_side_order(self, qtbot, tmp_path):
        # End-to-end: only a "codex" task is visible, but the project's
        # config declares both claude and codex -- the Side column must
        # show "codex -> claude", not "codex -> ?" (fix 6b).
        from spar.exec.state import ExecState, ExecStateStore, TaskState
        from spar.exec.tasklist import Task

        spar_dir = tmp_path / ".spar"
        spar_dir.mkdir()
        task = Task(
            id="t1", description="x", side="codex", model="gpt-5.5",
            review_model="sonnet", deps=(), files=(), test=None,
        )
        exec_state = ExecState(
            phase="execution",
            tasks={"t1": TaskState(task=task, status="pending")},
            pending_gate=None,
        )
        ExecStateStore(spar_dir).save(exec_state)

        pane = SidePane(tmp_path, MagicMock())
        qtbot.addWidget(pane)
        pane.refresh()

        rows = {r["task_id"]: r for r in pane.task_board.rows}
        assert rows["t1"]["reviewer"] == "claude"
        # Fix 5: the Side column includes each side's model when the task
        # carries one -- "codex·gpt-5.5 -> claude·sonnet".
        assert rows["t1"]["label"] == "codex·gpt-5.5 → claude·sonnet"


class TestAutoExecPreflight:
    def test_cancelled_preflight_blocks_resume_and_keeps_gate_actionable(self, qtbot, tmp_path):
        # The consensus "Accept → start exec" auto-chain must run the shared
        # dirty-tree pre-flight; cancelling it must NOT consume the gate.
        from spar.gui.sidepane import GatePanel

        class FakeRunner:
            def __init__(self):
                self.calls = []
            def resume(self, value, auto_exec=False):
                self.calls.append((value, auto_exec))
            def resume_with_remarks(self, text):
                self.calls.append(("remarks", text))

        runner = FakeRunner()
        panel = GatePanel(runner)
        qtbot.addWidget(panel)
        panel.set_pending_gate({"name": "consensus", "options": ["accept", "abort"], "context": {}})

        panel.preflight_auto_exec = lambda: False
        from PySide6.QtWidgets import QPushButton
        primary = next(
            b for b in panel.findChildren(QPushButton) if "start exec" in b.text().lower()
        )
        primary.click()
        assert runner.calls == []
        assert primary.isEnabled()  # gate still actionable

        panel.preflight_auto_exec = lambda: True
        primary.click()
        assert runner.calls == [("accept", True)]
