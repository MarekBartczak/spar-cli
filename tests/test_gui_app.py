"""Tests for the spar gui skeleton: MainWindow shell + theme QSS.

Skipped entirely on interpreters without the optional ``gui`` extra
installed (see pyproject.toml's ``[project.optional-dependencies].gui``).
"""

import re

import pytest

pytest.importorskip("PySide6")

from spar.gui import theme
from spar.gui.app import MainWindow, SidePane, StreamPane, Toolbar, _short_action_label

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

    def test_close_event_stops_runner_and_sidepane_poll_timers(self, qtbot, tmp_path):
        # Final review minor #1: closing the window used to stop only the
        # tailer, leaking the runner's 750ms poll and the side pane's 2s
        # poll -- fatal for an embedded/reused MainWindow.
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        assert window.runner._poll.isActive() is True
        assert window.side_pane._poll.isActive() is True

        window.close()

        assert window.runner._poll.isActive() is False
        assert window.side_pane._poll.isActive() is False

    def test_close_event_sigints_a_still_running_child(self, qtbot, tmp_path):
        # A window closed while a child spar process is alive must not
        # orphan it holding the .spar lock: closeEvent takes the same
        # SIGINT path as Stop.
        from tests.test_gui_runner import _make_fake, _use_fake

        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        record = tmp_path / "rec.jsonl"
        marker = tmp_path / "sigint.marker"
        _use_fake(window.runner, _make_fake(tmp_path, record, sleep=True, sigint_marker=marker))

        window.runner.start_debate("x", "claude,codex", "claude", True)
        qtbot.waitUntil(lambda: record.exists(), timeout=10000)

        window.close()

        assert marker.exists()
        assert marker.read_text() == "sigint"

    def test_state_changed_drives_chat_banner_for_running_and_locked(self, qtbot, tmp_path):
        # Review #28: LOCKED means a CONFIRMED live sibling spar process — the
        # read-only banner must show for it exactly as for our own RUNNING child,
        # and hide again on non-live states.
        from spar.gui.runner import RunnerState

        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        calls = []
        window.chat_panel.set_running = lambda flag: calls.append(flag)
        window._on_state_changed(RunnerState.RUNNING)
        window._on_state_changed(RunnerState.IDLE)
        window._on_state_changed(RunnerState.LOCKED)
        window._on_state_changed(RunnerState.DONE)
        assert calls == [True, False, True, False]

    def test_mainwindow_close_stops_chat_session(self, qtbot, tmp_path):
        # Review #11: drives MainWindow.close() through closeEvent and asserts
        # the chat session is stopped (the earlier retention test never
        # constructed/closed a MainWindow).
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        calls = []
        real_stop = window.chat_panel.stop_session

        def spy():
            calls.append("stop")
            real_stop()  # still perform the real teardown (abandoned-thread safe)

        window.chat_panel.stop_session = spy
        window.close()  # -> closeEvent -> stop_session()
        assert calls == ["stop"]


class TestRailsLayout:
    """ADR 0005: JetBrains-style icon rails on both edges toggling the
    collapsible right column, with a pending-gate attention icon."""

    def test_has_left_and_right_rails(self, qtbot, tmp_path):
        from spar.gui.rails import IconRail
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        rails = window.findChildren(IconRail)
        assert len(rails) == 2
        assert "files" in window.left_rail.buttons
        assert set(window.right_rail.buttons) >= {"tasks", "chat", "gate"}
        assert window.left_rail.buttons["files"].isEnabled() is False

    def test_collapsing_both_right_panels_hides_column(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        window.right_rail.buttons["tasks"].setChecked(False)
        window.right_rail.buttons["chat"].setChecked(False)
        # isHidden() reflects the explicit setVisible(False) regardless of whether
        # the (never-shown) window has been shown — unlike isVisible(), which is
        # vacuously False pre-show and would pass even if the code did nothing
        # (review #8).
        assert window.right_column.isHidden() is True
        # Re-open one panel -> column reappears (explicitly not hidden).
        window.right_rail.buttons["tasks"].setChecked(True)
        assert window.right_column.isHidden() is False

    def test_rail_state_persists_via_qsettings(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        window.right_rail.buttons["chat"].setChecked(False)
        assert window._settings.value("rails/chat_visible") in (False, "false", 0, "0")

    def test_gate_icon_hidden_without_pending_gate(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        # Fresh dir: no pending gate -> Bramka icon explicitly hidden (isHidden(),
        # not the vacuous pre-show isVisible(); review #8).
        assert window.right_rail.buttons["gate"].isHidden() is True

    def test_new_pending_gate_force_opens_taski(self, qtbot, tmp_path):
        # Review #4: a new pending gate auto-opens Taski without resolving it.
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        window.right_rail.set_checked("tasks", False)
        window._on_rail_toggled("tasks", False)
        assert window.right_rail.buttons["tasks"].isChecked() is False
        window._on_status_changed(
            {"pending_gate": {"name": "consensus", "context": {"task_id": "t1"}}}
        )
        assert window.right_rail.buttons["tasks"].isChecked() is True
        assert window.right_rail.buttons["gate"].isHidden() is False


class TestStartupIndicator:
    """Task brief, fix 3: an indeterminate progress bar + label shown between
    a start and the process's first output line (or its finish)."""

    def test_hidden_initially(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        assert window._startup_progress.isVisible() is False
        assert window._startup_label.isVisible() is False

    def test_shown_on_started(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        window.show()

        window._on_started("spar --continue")

        assert window._startup_progress.isVisible() is True
        assert window._startup_label.isVisible() is True
        assert window._startup_progress.minimum() == 0
        assert window._startup_progress.maximum() == 0  # indeterminate

    def test_hidden_on_first_stream_lines(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        window.show()

        window._on_started("spar --continue")
        window._on_first_stream_lines(["[claude r0] hello"])

        assert window._startup_progress.isVisible() is False
        assert window._startup_label.isVisible() is False

    def test_hidden_on_finished(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        window.show()

        window._on_started("spar --continue")
        window._on_finished(0)

        assert window._startup_progress.isVisible() is False
        assert window._startup_label.isVisible() is False


class TestShortActionLabel:
    """Pure helper behind the stream's start notice (fix 1)."""

    def test_new_debate(self):
        cmd = "python -m spar.cli --task-file /tmp/x.md --sides claude,codex --first claude --headless --quiet --tasks"
        assert _short_action_label(cmd) == "nowa debata"

    def test_start_exec(self):
        assert _short_action_label("python -m spar.cli exec --headless --quiet") == "start exec"

    def test_resume_debate(self):
        assert _short_action_label("python -m spar.cli --continue --headless --quiet") == "wznów"

    def test_resume_exec(self):
        cmd = "python -m spar.cli exec --continue --headless --quiet --gate accept"
        assert _short_action_label(cmd) == "wznów exec"


class TestStreamNotices:
    """Smoke-feedback round 2, fix 1/2: visible-in-stream start/guard/chain
    feedback, wired from SparRunner's started/notice signals."""

    def test_on_started_appends_notice_to_stream(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        window._on_started(
            "python -m spar.cli --task-file /tmp/x.md --sides claude,codex --first claude --headless --quiet"
        )

        text = window.stream_pane.text.toPlainText()
        assert "▶ uruchamiam: nowa debata…" in text

    def test_runner_notice_appends_to_stream(self, qtbot, tmp_path):
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        window.runner.notice.emit("▶ konsensus przyjęty — startuję exec…")

        text = window.stream_pane.text.toPlainText()
        assert "konsensus przyjęty" in text


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


def test_side_models_prefer_debate_model(tmp_path, monkeypatch):
    # The debate actually runs on debate_model (engine: debate_model or
    # model); the humanized prefix must mirror that, not default_model
    # (live finding: display said sonnet while the transcript proved opus).
    from spar.config import SideConfig
    import spar.gui.app as app_mod

    class _Cfg:
        sides = {
            "claude": SideConfig(
                adapter="claude", command="claude",
                models=("opus", "sonnet"), default_model="sonnet",
                debate_model="opus",
            ),
            "codex": SideConfig(
                adapter="codex", command="codex",
                models=("gpt-5.5",), default_model="gpt-5.5",
            ),
        }

    monkeypatch.setattr(app_mod, "load_config", lambda _dir: _Cfg())
    win = app_mod.MainWindow(tmp_path)
    try:
        assert win._side_models["claude"] == "opus"      # debate_model wins
        assert win._side_models["codex"] == "gpt-5.5"    # fallback chain
    finally:
        win.close()


class TestNewDebateGitGuard:
    """A new debate must not start outside a local git repo (task brief):
    the toolbar's new-debate flow offers to create one on demand."""

    @staticmethod
    def _accept_dialog(monkeypatch, values):
        import spar.gui.app as app_mod

        class _FakeDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                from PySide6.QtWidgets import QDialog

                return QDialog.DialogCode.Accepted

            def values(self):
                return values

        monkeypatch.setattr(app_mod.toolbar_mod, "NewDebateDialog", _FakeDialog)

    def test_no_repo_accept_creates_repo_and_starts_debate(self, qtbot, tmp_path, monkeypatch):
        import spar.gui.app as app_mod
        from PySide6.QtWidgets import QMessageBox

        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        self._accept_dialog(monkeypatch, {"task_text": "x", "sides": "claude,codex", "first": "claude", "tasks": True})
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )
        started = []
        monkeypatch.setattr(window.runner, "start_debate", lambda **kw: started.append(kw))

        window._on_new_debate()

        assert (tmp_path / ".git").is_dir()
        log = __import__("subprocess").run(
            ["git", "-C", str(tmp_path), "log", "--oneline"],
            check=True, capture_output=True, text=True,
        )
        assert len(log.stdout.strip().splitlines()) == 1
        assert started == [{"task_text": "x", "sides": "claude,codex", "first": "claude", "tasks": True}]

    def test_no_repo_cancel_does_not_spawn_or_create_repo(self, qtbot, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        self._accept_dialog(monkeypatch, {"task_text": "x", "sides": "claude,codex", "first": "claude", "tasks": True})
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel)
        )
        started = []
        monkeypatch.setattr(window.runner, "start_debate", lambda **kw: started.append(kw))

        window._on_new_debate()

        assert not (tmp_path / ".git").exists()
        assert started == []

    def test_existing_repo_with_commit_proceeds_without_dialog(self, qtbot, tmp_path, monkeypatch):
        import subprocess

        subprocess.run(["git", "init", "-b", "master"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"],
            cwd=tmp_path, check=True, capture_output=True,
        )

        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        self._accept_dialog(monkeypatch, {"task_text": "x", "sides": "claude,codex", "first": "claude", "tasks": True})

        from PySide6.QtWidgets import QMessageBox

        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: asked.append(1) or QMessageBox.StandardButton.Cancel),
        )
        started = []
        monkeypatch.setattr(window.runner, "start_debate", lambda **kw: started.append(kw))

        window._on_new_debate()

        assert asked == []
        assert started == [{"task_text": "x", "sides": "claude,codex", "first": "claude", "tasks": True}]


def _init_repo(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-b", "master"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
    )


class TestStartExecDirtyTreeGuard:
    """A grill session legitimately leaves behind uncommitted docs (live
    finding): ``spar exec`` refuses outright on a dirty tree, so the Start
    exec action offers to commit them instead of dead-ending the user."""

    def test_dirty_tree_accept_commits_and_starts_exec(self, qtbot, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        _init_repo(tmp_path)
        (tmp_path / "CONTEXT.md").write_text("grill notes\n", encoding="utf-8")

        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )
        started = []
        monkeypatch.setattr(window.runner, "start_exec", lambda: started.append(1))

        window._on_start_exec()

        from spar.gui import repo as repo_mod

        assert repo_mod.dirty_paths(tmp_path) == []
        import subprocess

        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline"],
            check=True, capture_output=True, text=True,
        )
        lines = log.stdout.strip().splitlines()
        assert len(lines) == 2
        assert "docs: pre-exec snapshot" in lines[0]
        assert started == [1]
        assert "▶ zacommitowano zmiany przed exec" in window.stream_pane.text.toPlainText()

    def test_dirty_tree_cancel_does_not_commit_or_start(self, qtbot, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        _init_repo(tmp_path)
        (tmp_path / "CONTEXT.md").write_text("grill notes\n", encoding="utf-8")

        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel)
        )
        started = []
        monkeypatch.setattr(window.runner, "start_exec", lambda: started.append(1))

        window._on_start_exec()

        from spar.gui import repo as repo_mod

        assert repo_mod.dirty_paths(tmp_path) == ["CONTEXT.md"]
        import subprocess

        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline"],
            check=True, capture_output=True, text=True,
        )
        assert len(log.stdout.strip().splitlines()) == 1
        assert started == []

    def test_clean_tree_proceeds_without_dialog(self, qtbot, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        _init_repo(tmp_path)

        window = MainWindow(tmp_path)
        qtbot.addWidget(window)

        def _fail_if_asked(*_a, **_k):
            raise AssertionError("QMessageBox.question must not be called on a clean tree")

        monkeypatch.setattr(QMessageBox, "question", staticmethod(_fail_if_asked))
        started = []
        monkeypatch.setattr(window.runner, "start_exec", lambda: started.append(1))

        window._on_start_exec()

        assert started == [1]


def test_new_debate_creates_starter_config_and_notifies(qtbot, tmp_path, monkeypatch):
    # Fresh project: no .git and no .spar/config.toml. Accepting the
    # create-repo offer must be followed by a starter config being written
    # (before the dialog opens) and a stream notice about it.
    import spar.gui.app as app_mod
    from PySide6.QtWidgets import QDialog, QMessageBox

    window = app_mod.MainWindow(tmp_path)
    qtbot.addWidget(window)
    try:
        class _FakeDialog:
            def __init__(self, *_args, **_kwargs):
                pass

            def exec(self):
                return QDialog.DialogCode.Accepted

            def values(self):
                return {"task_text": "x", "sides": "claude,codex", "first": "claude", "tasks": True}

        monkeypatch.setattr(app_mod.toolbar_mod, "NewDebateDialog", _FakeDialog)
        monkeypatch.setattr(
            QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        )
        started = []
        monkeypatch.setattr(window.runner, "start_debate", lambda **kw: started.append(kw))

        window._on_new_debate()

        config_path = tmp_path / ".spar" / "config.toml"
        assert config_path.is_file()
        from spar.gui.repo import _CONFIG_TEMPLATE

        assert config_path.read_text(encoding="utf-8") == _CONFIG_TEMPLATE
        assert started  # the debate still proceeded

        notice = "▶ utworzono .spar/config.toml — dostosuj modele i test_command do projektu"
        assert notice in window.stream_pane.text.toPlainText()
    finally:
        window.close()


def test_repo_check_precedes_the_new_debate_dialog(tmp_path, monkeypatch):
    # Declining the create-repo question must prevent the form from even
    # opening (the user must not type a task first).
    import spar.gui.app as app_mod
    from PySide6.QtWidgets import QMessageBox

    win = app_mod.MainWindow(tmp_path)  # tmp dir: no git repo
    try:
        opened = []
        monkeypatch.setattr(
            app_mod.toolbar_mod, "NewDebateDialog",
            lambda *a, **k: opened.append(1) or (_ for _ in ()).throw(AssertionError("dialog opened")),
        )
        monkeypatch.setattr(
            QMessageBox, "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
        )
        win._on_new_debate()
        assert opened == []  # form never constructed
    finally:
        win.close()


def test_create_repo_gitignores_spar_dir(tmp_path):
    # Fresh-project E2E: .spar/ runtime state must be ignored from the very
    # first commit, or exec later refuses on a "dirty" target.
    from spar.gui import repo as repo_mod

    (tmp_path / ".spar").mkdir()
    (tmp_path / ".spar" / "config.toml").write_text("# cfg\n", encoding="utf-8")
    repo_mod.create_repo(tmp_path)

    gi = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".spar/" in gi
    import subprocess
    tracked = subprocess.run(
        ["git", "-C", str(tmp_path), "ls-files"], capture_output=True, text=True
    ).stdout
    assert ".spar/config.toml" not in tracked
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert status == ""  # clean tree despite .spar content


class TestEnsureProjectConfig:
    """A fresh project has no ``.spar/config.toml`` -- and without one every
    ``--tasks`` plan is rejected (empty model catalogs). The GUI must create
    a starter config once, and never touch an existing one."""

    def test_creates_starter_config_when_missing(self, tmp_path):
        from spar.gui import repo as repo_mod

        created = repo_mod.ensure_project_config(tmp_path)

        assert created is True
        config_path = tmp_path / ".spar" / "config.toml"
        assert config_path.is_file()
        assert config_path.read_text(encoding="utf-8") == repo_mod._CONFIG_TEMPLATE

    def test_no_ops_when_config_already_exists(self, tmp_path):
        from spar.gui import repo as repo_mod

        config_path = tmp_path / ".spar" / "config.toml"
        config_path.parent.mkdir(parents=True)
        sentinel = "# sentinel — do not touch\n"
        config_path.write_text(sentinel, encoding="utf-8")

        created = repo_mod.ensure_project_config(tmp_path)

        assert created is False
        assert config_path.read_text(encoding="utf-8") == sentinel

    def test_second_call_is_a_no_op(self, tmp_path):
        from spar.gui import repo as repo_mod

        first = repo_mod.ensure_project_config(tmp_path)
        config_path = tmp_path / ".spar" / "config.toml"
        config_path.write_text("# mutated after first call\n", encoding="utf-8")

        second = repo_mod.ensure_project_config(tmp_path)

        assert first is True
        assert second is False
        assert config_path.read_text(encoding="utf-8") == "# mutated after first call\n"
