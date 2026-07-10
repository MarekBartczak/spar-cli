"""Tests for ``spar.gui.toolbar``'s ``NewDebateDialog`` (task brief, fix 1/2).

Skipped entirely on interpreters without the optional ``gui`` extra.

The dialog builds its "Strony" checkboxes from the project's configured
sides (``spar.config.load_config(project_dir).sides``) rather than a free
``QLineEdit``, and its "Pierwszy" combo tracks whichever sides are
currently CHECKED. A real ``.spar/config.toml`` is written per test
(rather than mocking ``load_config``) so the dialog is exercised against
the real config-loading path.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QCheckBox, QComboBox

from spar.gui.toolbar import NewDebateDialog


def _write_config(project_dir, body: str) -> None:
    config_path = project_dir / ".spar" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(body, encoding="utf-8")


class TestNewDebateDialogSides:
    def test_default_project_offers_claude_and_codex_both_checked(self, qtbot, tmp_path):
        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        assert list(dialog.side_checks.keys()) == ["claude", "codex"]
        assert all(check.isChecked() for check in dialog.side_checks.values())
        assert isinstance(dialog.side_checks["claude"], QCheckBox)

    def test_first_combo_defaults_to_first_configured_side(self, qtbot, tmp_path):
        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        assert isinstance(dialog.first_combo, QComboBox)
        assert dialog.first_combo.currentText() == "claude"
        assert [dialog.first_combo.itemText(i) for i in range(dialog.first_combo.count())] == [
            "claude",
            "codex",
        ]

    def test_unchecking_a_side_removes_it_from_first_combo(self, qtbot, tmp_path):
        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        dialog.side_checks["claude"].setChecked(False)

        assert [dialog.first_combo.itemText(i) for i in range(dialog.first_combo.count())] == [
            "codex"
        ]
        assert dialog.first_combo.currentText() == "codex"

    def test_rechecking_restores_the_side_in_first_combo(self, qtbot, tmp_path):
        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        dialog.side_checks["claude"].setChecked(False)
        dialog.side_checks["claude"].setChecked(True)

        assert [dialog.first_combo.itemText(i) for i in range(dialog.first_combo.count())] == [
            "claude",
            "codex",
        ]

    def test_values_builds_csv_from_checked_sides_only(self, qtbot, tmp_path):
        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        dialog.task_edit.setPlainText("do the thing")
        dialog.side_checks["codex"].setChecked(False)

        values = dialog.values()
        assert values == {
            "task_text": "do the thing",
            "sides": "claude",
            "first": "claude",
            "tasks": True,
        }

    def test_configured_third_side_appears_after_claude_codex(self, qtbot, tmp_path):
        _write_config(
            tmp_path,
            """
[sides.claude]
adapter = "claude"
command = "claude"

[sides.codex]
adapter = "codex"
command = "codex"

[sides.thirdside]
adapter = "claude"
command = "claude-alt"
""",
        )
        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        assert list(dialog.side_checks.keys()) == ["claude", "codex", "thirdside"]

    def test_malformed_config_falls_back_to_default_sides(self, qtbot, tmp_path):
        _write_config(tmp_path, "not valid toml [[[")
        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        assert list(dialog.side_checks.keys()) == ["claude", "codex"]

    def test_no_project_dir_falls_back_to_default_sides(self, qtbot):
        dialog = NewDebateDialog(None)
        qtbot.addWidget(dialog)

        assert list(dialog.side_checks.keys()) == ["claude", "codex"]


class TestNewDebateDialogSize:
    def test_resizes_to_760x560(self, qtbot, tmp_path):
        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        assert dialog.size().width() == 760
        assert dialog.size().height() == 560


class TestNewDebateDialogGrillButton:
    def test_grill_accept_replaces_task_text(self, qtbot, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QDialog

        import spar.gui.toolbar as toolbar_mod

        captured = {}

        class FakeGrillDialog:
            def __init__(self, project_dir, side_cfg, timeout_sec, draft, parent=None):
                captured["draft"] = draft
                self.result_requirements = "# Wygrillowane wymagania\n\n## Tasks\n- a\n"

            def exec(self):
                return QDialog.DialogCode.Accepted

        monkeypatch.setattr(toolbar_mod, "GrillDialog", FakeGrillDialog)

        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)
        dialog.task_edit.setPlainText("mój szkic")

        dialog.grill_button.click()

        assert captured["draft"] == "mój szkic"
        assert dialog.task_edit.toPlainText() == "# Wygrillowane wymagania\n\n## Tasks\n- a\n"

    def test_grill_rejected_leaves_task_text_untouched(self, qtbot, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QDialog

        import spar.gui.toolbar as toolbar_mod

        class FakeGrillDialog:
            def __init__(self, project_dir, side_cfg, timeout_sec, draft, parent=None):
                self.result_requirements = None

            def exec(self):
                return QDialog.DialogCode.Rejected

        monkeypatch.setattr(toolbar_mod, "GrillDialog", FakeGrillDialog)

        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)
        dialog.task_edit.setPlainText("nietknięty szkic")

        dialog.grill_button.click()

        assert dialog.task_edit.toPlainText() == "nietknięty szkic"

    def test_grill_button_disabled_when_config_load_fails(self, qtbot, tmp_path, monkeypatch):
        import spar.gui.toolbar as toolbar_mod

        def _boom(project_dir):
            raise toolbar_mod.ConfigError("bad toml")

        monkeypatch.setattr(toolbar_mod, "load_config", _boom)

        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        assert not dialog.grill_button.isEnabled()
        assert dialog.grill_button.toolTip()

    def test_grill_button_disabled_when_claude_side_not_claude_adapter(
        self, qtbot, tmp_path, monkeypatch
    ):
        import spar.gui.toolbar as toolbar_mod
        from spar.config import Config, DebateConfig, ExecutionConfig, SideConfig

        def _fake_load_config(project_dir):
            return Config(
                sides={"claude": SideConfig(adapter="codex", command="codex")},
                debate=DebateConfig(),
                execution=ExecutionConfig(),
            )

        monkeypatch.setattr(toolbar_mod, "load_config", _fake_load_config)

        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        assert not dialog.grill_button.isEnabled()
        assert dialog.grill_button.toolTip()

    def test_grill_button_enabled_with_valid_claude_side(self, qtbot, tmp_path, monkeypatch):
        import spar.gui.toolbar as toolbar_mod
        from spar.config import Config, DebateConfig, ExecutionConfig, SideConfig

        def _fake_load_config(project_dir):
            return Config(
                sides={
                    "claude": SideConfig(adapter="claude", command="claude"),
                    "codex": SideConfig(adapter="codex", command="codex"),
                },
                debate=DebateConfig(turn_timeout_sec=123),
                execution=ExecutionConfig(),
            )

        monkeypatch.setattr(toolbar_mod, "load_config", _fake_load_config)

        dialog = NewDebateDialog(tmp_path)
        qtbot.addWidget(dialog)

        assert dialog.grill_button.isEnabled()
        assert dialog._grill_timeout_sec == 123
