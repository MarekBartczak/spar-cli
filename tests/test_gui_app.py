"""Tests for the spar gui skeleton: MainWindow shell + theme QSS.

Skipped entirely on interpreters without the optional ``gui`` extra
installed (see pyproject.toml's ``[project.optional-dependencies].gui``).
"""

import re

import pytest

pytest.importorskip("PySide6")

from spar.gui import theme
from spar.gui.app import MainWindow, SidePane, StreamPane, Toolbar

_TOOLBAR_LABELS = ["Nowa debata…", "Start exec", "Wznów", "Stop", "Plan", "Diff"]


class TestMainWindow:
    def test_constructs_with_three_panes(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        assert window.findChild(StreamPane, "streamPane") is not None
        assert window.findChild(SidePane, "sidePane") is not None
        assert window.findChild(Toolbar) is not None
        assert window.statusBar() is not None

    def test_window_title_contains_project_dir_name(self, qtbot, tmp_path):
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()
        window = MainWindow(project_dir)
        qtbot.addWidget(window)

        assert "my-project" in window.windowTitle()

    def test_toolbar_wired_for_idle_dir(self, qtbot, tmp_path):
        # A fresh dir derives IDLE: only "Nowa debata…" is enabled; the
        # unwired read-only views ("Plan"/"Diff") stay disabled.
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        actions = window.toolbar.actions_by_label
        assert actions["Nowa debata…"].isEnabled() is True
        for label in ["Start exec", "Wznów", "Stop", "Plan", "Diff"]:
            assert actions[label].isEnabled() is False

    def test_splitter_ratio_is_wider_left_than_right(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        sizes = window.splitter.sizes()
        assert len(sizes) == 2
        assert sizes[0] > sizes[1]

    def test_splitter_state_persists_via_qsettings(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        window.splitter.setSizes([500, 500])
        window._save_splitter_state()

        window2 = MainWindow(tmp_path)
        qtbot.addWidget(window2)

        assert window2._settings.value("mainSplitter/state") is not None


class TestTheme:
    def test_tokens_dict_has_required_keys(self):
        required = {
            "ground", "panel", "line", "text", "muted",
            "claude", "codex", "spar-log", "ok", "warn", "gate",
        }
        assert required.issubset(theme.TOKENS.keys())

    def test_build_qss_uses_only_token_colors(self):
        # The base chrome QSS built here only styles window/toolbar/status
        # bar/splitter/panes -- the role colors (claude/codex/spar-log/ok/
        # warn/gate) are consumed directly by stream/side content in later
        # tasks, not by this skeleton's QSS. Every hex literal that *does*
        # appear in the QSS must come from TOKENS (no ad-hoc colors).
        qss = theme.build_qss()
        assert isinstance(qss, str) and qss.strip()

        chrome_tokens = {
            "ground", "panel", "panel-alt", "line", "text", "muted",
        }
        for name in chrome_tokens:
            assert theme.TOKENS[name] in qss

        hex_literals = set(re.findall(r"#[0-9a-fA-F]{6}", qss))
        assert hex_literals.issubset(set(theme.TOKENS.values()))
