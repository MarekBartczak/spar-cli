"""Tests for spar.gui.instances — per-project identity and window plumbing."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from spar.gui import instances


@pytest.fixture(autouse=True)
def _hermetic_qsettings():
    from PySide6.QtCore import QSettings

    QSettings("spar", "gui").clear()
    yield


class TestProjectKey:
    def test_stable_for_the_same_directory(self, tmp_path):
        assert instances.project_key(tmp_path) == instances.project_key(tmp_path)

    def test_differs_between_directories(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert instances.project_key(a) != instances.project_key(b)

    def test_normalizes_relative_and_dotted_paths(self, tmp_path, monkeypatch):
        (tmp_path / "proj").mkdir()
        monkeypatch.chdir(tmp_path / "proj")
        # "." , "../proj" and the absolute path are ONE project.
        assert instances.project_key(".") == instances.project_key(tmp_path / "proj")
        assert instances.project_key("../proj") == instances.project_key(tmp_path / "proj")

    def test_is_short_hex(self, tmp_path):
        key = instances.project_key(tmp_path)
        assert len(key) == 12
        assert all(c in "0123456789abcdef" for c in key)


class TestScoped:
    def test_prefixes_with_project_key(self, tmp_path):
        key = instances.project_key(tmp_path)
        assert instances.scoped(tmp_path, "rails/centre_view") == (
            f"projects/{key}/rails/centre_view"
        )


class TestRecentProjects:
    def test_empty_by_default(self):
        assert instances.recent_projects() == []

    def test_push_puts_newest_first_and_dedups(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        instances.push_recent_project(a)
        instances.push_recent_project(b)
        instances.push_recent_project(a)
        assert instances.recent_projects() == [str(a.resolve()), str(b.resolve())]

    def test_caps_at_ten(self, tmp_path):
        for i in range(12):
            d = tmp_path / f"p{i}"
            d.mkdir()
            instances.push_recent_project(d)
        assert len(instances.recent_projects()) == 10
        assert instances.recent_projects()[0] == str((tmp_path / "p11").resolve())

    def test_drops_vanished_directories(self, tmp_path):
        gone = tmp_path / "gone"
        gone.mkdir()
        instances.push_recent_project(gone)
        gone.rmdir()
        assert instances.recent_projects() == []


class TestWindowTitle:
    def test_includes_name_and_parent(self, tmp_path):
        proj = tmp_path / "ai_fight"
        proj.mkdir()
        title = instances.window_title(proj)
        assert title.startswith("spar — ai_fight (")
        assert str(tmp_path) in title

    def test_collapses_home_to_tilde(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        proj = tmp_path / "P_PROJ" / "ai_fight"
        proj.mkdir(parents=True)
        assert instances.window_title(proj) == "spar — ai_fight (~/P_PROJ)"

    def test_root_level_project_has_no_empty_parens(self):
        assert instances.window_title("/") == "spar — /"


class TestNewWindowCommand:
    def test_normal_install_spawns_module_entry_point(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instances.sys, "frozen", False, raising=False)
        cmd = instances.new_window_command(tmp_path)
        assert cmd[1:] == ["-m", "spar.cli", "gui", "--dir", str(tmp_path.resolve())]

    def test_frozen_macos_uses_open_dash_n(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Spar.app"
        exe = bundle / "Contents" / "MacOS" / "Spar"
        exe.parent.mkdir(parents=True)
        exe.touch()
        monkeypatch.setattr(instances.sys, "frozen", True, raising=False)
        monkeypatch.setattr(instances.sys, "executable", str(exe))
        monkeypatch.setattr(instances.sys, "platform", "darwin")
        proj = tmp_path / "proj"
        proj.mkdir()
        assert instances.new_window_command(proj) == [
            "open", "-n", "-a", str(bundle), "--args", "--dir", str(proj.resolve()),
        ]

    def test_frozen_non_macos_reinvokes_the_executable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instances.sys, "frozen", True, raising=False)
        monkeypatch.setattr(instances.sys, "executable", "/opt/spar/spar")
        monkeypatch.setattr(instances.sys, "platform", "linux")
        cmd = instances.new_window_command(tmp_path)
        assert cmd == ["/opt/spar/spar", "--dir", str(tmp_path.resolve())]


class TestSpawnNewWindow:
    def test_starts_detached_with_the_command(self, tmp_path, monkeypatch):
        from PySide6.QtCore import QProcess

        seen = {}

        def fake_detached(program, arguments, working_dir):
            seen["program"] = program
            seen["arguments"] = arguments
            seen["cwd"] = working_dir
            return True, 4242

        monkeypatch.setattr(QProcess, "startDetached", staticmethod(fake_detached))
        monkeypatch.setattr(instances.sys, "frozen", False, raising=False)
        assert instances.spawn_new_window(tmp_path) is True
        assert seen["arguments"][-2:] == ["--dir", str(tmp_path.resolve())]
        assert seen["cwd"] == str(tmp_path.resolve())
