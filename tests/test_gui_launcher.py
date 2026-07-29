"""Tests for the ``spar-gui`` desktop entry point (spar.gui.launcher)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from spar.gui import launcher


class TestParseTarget:
    """`spar-gui [PATH] [--pick]` maps onto main_gui's --dir, or asks."""

    def test_bare_invocation_uses_the_current_directory(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target, needs_pick = launcher.parse_target([])
        assert needs_pick is False
        assert target == tmp_path.resolve()

    def test_positional_path_wins(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        target, needs_pick = launcher.parse_target([str(proj)])
        assert needs_pick is False
        assert target == proj.resolve()

    def test_dir_flag_is_accepted_for_parity_with_spar_gui(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        target, needs_pick = launcher.parse_target(["--dir", str(proj)])
        assert target == proj.resolve()

    def test_pick_flag_requests_the_dialog(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        target, needs_pick = launcher.parse_target(["--pick"])
        assert needs_pick is True
        assert target is None

    def test_pick_with_a_path_still_honours_the_path(self, tmp_path):
        """A desktop launcher passes --pick, a file manager appends the
        dropped folder: the concrete folder must win, no dialog."""
        proj = tmp_path / "proj"
        proj.mkdir()
        target, needs_pick = launcher.parse_target(["--pick", str(proj)])
        assert needs_pick is False
        assert target == proj.resolve()

    def test_a_file_argument_resolves_to_its_directory(self, tmp_path):
        """Nautilus "open with" can hand over a file inside the project."""
        proj = tmp_path / "proj"
        proj.mkdir()
        f = proj / "main.py"
        f.write_text("x = 1\n")
        target, needs_pick = launcher.parse_target([str(f)])
        assert target == proj.resolve()

    def test_missing_path_falls_back_to_the_picker(self, tmp_path):
        """Better a dialog than a window on a directory that isn't there."""
        target, needs_pick = launcher.parse_target([str(tmp_path / "gone")])
        assert needs_pick is True
        assert target is None


class TestPickerStart:
    def test_starts_at_the_most_recent_project_when_there_is_one(
        self, tmp_path, monkeypatch
    ):
        from PySide6.QtCore import QSettings

        QSettings("spar", "gui").clear()
        from spar.gui.instances import push_recent_project

        recent = tmp_path / "recent"
        recent.mkdir()
        push_recent_project(recent)
        assert launcher.picker_start_dir() == str(recent.resolve())

    def test_falls_back_to_home_without_history(self, monkeypatch, tmp_path):
        from PySide6.QtCore import QSettings

        QSettings("spar", "gui").clear()
        monkeypatch.setenv("HOME", str(tmp_path))
        assert launcher.picker_start_dir() == str(tmp_path)


class TestMain:
    """The in-process path. `_no_detach` keeps these from forking a real
    window: without it they would spawn detached GUI processes that outlive
    the test session."""

    @pytest.fixture(autouse=True)
    def _no_detach(self, monkeypatch):
        monkeypatch.setenv(launcher._DETACHED_ENV, "1")

    def test_delegates_to_main_gui_with_dir(self, tmp_path, monkeypatch):
        seen = {}

        def fake_main_gui(argv):
            seen["argv"] = argv
            return 0

        monkeypatch.setattr(launcher, "main_gui", fake_main_gui)
        rc = launcher.main([str(tmp_path)])
        assert rc == 0
        assert seen["argv"] == ["--dir", str(tmp_path.resolve())]

    def test_cancelled_picker_exits_quietly(self, monkeypatch):
        monkeypatch.setattr(launcher, "main_gui", lambda argv: pytest.fail("no window"))
        monkeypatch.setattr(launcher, "ask_for_project_dir", lambda: None)
        assert launcher.main(["--pick"]) == 0

    def test_picked_directory_is_passed_through(self, tmp_path, monkeypatch):
        seen = {}

        def fake_main_gui(argv):
            seen["argv"] = argv
            return 0

        monkeypatch.setattr(launcher, "main_gui", fake_main_gui)
        monkeypatch.setattr(launcher, "ask_for_project_dir", lambda: str(tmp_path))
        assert launcher.main(["--pick"]) == 0
        assert seen["argv"] == ["--dir", str(tmp_path)]

    def test_help_prints_usage_without_launching(self, monkeypatch, capsys):
        monkeypatch.setattr(launcher, "main_gui", lambda argv: pytest.fail("no window"))
        for flag in ("-h", "--help"):
            assert launcher.main([flag]) == 0
            assert "usage: spar-gui" in capsys.readouterr().out


class TestDetach:
    """`spar-gui` must return the shell prompt immediately, like `code`."""

    def test_detaches_by_default(self):
        assert launcher.should_detach([], {}) is True

    def test_child_process_does_not_detach_again(self):
        """The re-launched child carries the marker, or we fork forever."""
        assert launcher.should_detach([], {launcher._DETACHED_ENV: "1"}) is False

    def test_foreground_flag_keeps_the_process_attached(self):
        assert launcher.should_detach(["--foreground"], {}) is False

    def test_detached_command_reinvokes_the_module_with_the_same_args(self, tmp_path):
        cmd = launcher.detached_command([str(tmp_path), "--pick"])
        assert cmd[:3] == [launcher.sys.executable, "-m", "spar.gui.launcher"]
        assert cmd[3:] == [str(tmp_path), "--pick"]

    def test_main_spawns_detached_and_returns_zero_without_a_window(
        self, tmp_path, monkeypatch
    ):
        spawned = {}

        def fake_spawn(argv):
            spawned["argv"] = argv
            return True

        monkeypatch.setattr(launcher, "spawn_detached", fake_spawn)
        monkeypatch.setattr(launcher, "main_gui", lambda argv: pytest.fail("no window"))
        monkeypatch.delenv(launcher._DETACHED_ENV, raising=False)
        assert launcher.main([str(tmp_path)]) == 0
        assert spawned["argv"] == [str(tmp_path)]

    def test_foreground_runs_the_window_in_this_process(self, tmp_path, monkeypatch):
        seen = {}

        def fake_main_gui(argv):
            seen["argv"] = argv
            return 0

        monkeypatch.setattr(launcher, "main_gui", fake_main_gui)
        monkeypatch.setattr(
            launcher, "spawn_detached", lambda argv: pytest.fail("must not fork")
        )
        assert launcher.main(["--foreground", str(tmp_path)]) == 0
        # --foreground must NOT swallow the path (it is not the project dir).
        assert seen["argv"] == ["--dir", str(tmp_path.resolve())]

    def test_failed_spawn_falls_back_to_running_in_the_foreground(
        self, tmp_path, monkeypatch
    ):
        """A fork that cannot happen must still give the user a window."""
        seen = {}

        def fake_main_gui(argv):
            seen["argv"] = argv
            return 0

        monkeypatch.setattr(launcher, "spawn_detached", lambda argv: False)
        monkeypatch.setattr(launcher, "main_gui", fake_main_gui)
        monkeypatch.delenv(launcher._DETACHED_ENV, raising=False)
        assert launcher.main([str(tmp_path)]) == 0
        assert seen["argv"] == ["--dir", str(tmp_path.resolve())]


class TestParseTargetWithFlags:
    def test_foreground_flag_does_not_shadow_the_path(self, tmp_path):
        proj = tmp_path / "proj"
        proj.mkdir()
        target, needs_pick = launcher.parse_target(["--foreground", str(proj)])
        assert needs_pick is False
        assert target == proj.resolve()
