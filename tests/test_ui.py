"""Tests for spar.ui: pick_spawn_argv detection cascade + main_ui + routing."""

import pytest

import spar.ui as ui_mod
from spar.ui import main_ui, pick_spawn_argv


class TestPickSpawnArgv:
    def test_tmux_env_wins_first(self):
        argv = pick_spawn_argv({"TMUX": "/tmp/tmux-1000/default,123,0"}, which=lambda name: None)
        assert argv == ["tmux", "split-window", "-h", "spar watch"]

    def test_tmux_takes_precedence_over_terminal_emulators(self):
        def which(name):
            return f"/usr/bin/{name}" if name == "gnome-terminal" else None

        argv = pick_spawn_argv({"TMUX": "x"}, which=which)
        assert argv == ["tmux", "split-window", "-h", "spar watch"]

    def test_no_tmux_gnome_terminal_available(self):
        def which(name):
            return "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None

        argv = pick_spawn_argv({}, which=which)
        assert argv == ["gnome-terminal", "--", "spar", "watch"]

    def test_no_tmux_xterm_available_uses_dash_e_separate_args(self):
        def which(name):
            return "/usr/bin/xterm" if name == "xterm" else None

        argv = pick_spawn_argv({}, which=which)
        assert argv == ["xterm", "-e", "spar", "watch"]
        # "-e" and "spar watch" must NOT be collapsed into one string: several
        # terminal emulators treat a single "spar watch" arg as a literal
        # executable name to look up, not a shell command line.
        assert "spar" in argv and "watch" in argv
        assert "spar watch" not in argv

    def test_no_tmux_konsole_available(self):
        def which(name):
            return "/usr/bin/konsole" if name == "konsole" else None

        argv = pick_spawn_argv({}, which=which)
        assert argv == ["konsole", "-e", "spar", "watch"]

    def test_x_terminal_emulator_preferred_over_others(self):
        def which(name):
            return f"/usr/bin/{name}"  # everything "available"

        argv = pick_spawn_argv({}, which=which)
        assert argv == ["x-terminal-emulator", "-e", "spar", "watch"]

    def test_nothing_available_returns_none(self):
        argv = pick_spawn_argv({}, which=lambda name: None)
        assert argv is None

    def test_warp_terminal_present_alone_still_falls_through_to_none(self):
        # The warp branch is deliberately kept minimal/best-effort (see
        # spar/ui.py): presence of warp-terminal alone, with no other
        # terminal emulator available, must not crash and degrades to the
        # manual instruction rather than guessing at an unverified
        # integration.
        def which(name):
            return "/usr/bin/warp-terminal" if name == "warp-terminal" else None

        argv = pick_spawn_argv({}, which=which)
        assert argv is None


class TestMainUi:
    def test_prints_instruction_and_exits_zero_when_nothing_available(
        self, monkeypatch, capsys
    ):
        monkeypatch.setattr(ui_mod, "pick_spawn_argv", lambda env, which: None)
        result = main_ui([])
        assert result == 0
        out = capsys.readouterr().out
        assert "spar watch" in out

    def test_spawns_detached_when_argv_available(self, monkeypatch):
        calls = []

        class _FakePopen:
            def __init__(self, argv, **kwargs):
                calls.append((argv, kwargs))

        monkeypatch.setattr(
            ui_mod, "pick_spawn_argv", lambda env, which: ["xterm", "-e", "spar", "watch"]
        )
        monkeypatch.setattr(ui_mod.subprocess, "Popen", _FakePopen)

        result = main_ui([])

        assert result == 0
        assert len(calls) == 1
        assert calls[0][0] == ["xterm", "-e", "spar", "watch"]

    def test_never_fails_even_if_spawn_raises(self, monkeypatch, capsys):
        def _raise(*a, **k):
            raise OSError("no such file")

        monkeypatch.setattr(
            ui_mod, "pick_spawn_argv", lambda env, which: ["xterm", "-e", "spar", "watch"]
        )
        monkeypatch.setattr(ui_mod.subprocess, "Popen", _raise)

        result = main_ui([])

        assert result == 0
        assert "spar watch" in capsys.readouterr().out

    def test_no_env_or_terminal_real_cascade_prints_instruction(
        self, monkeypatch, capsys
    ):
        """End-to-end (no monkeypatching pick_spawn_argv itself): a bare
        environment with no TMUX and no terminal emulators on PATH must
        print the manual instruction and exit 0."""
        monkeypatch.delenv("TMUX", raising=False)
        monkeypatch.setattr(ui_mod.shutil, "which", lambda name: None)

        result = main_ui([])

        assert result == 0
        assert "spar watch" in capsys.readouterr().out


class TestUiCliRouting:
    def test_ui_help_exits_zero(self, capsys):
        from spar.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["ui", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "usage" in captured.out
