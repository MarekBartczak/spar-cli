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


class _FakeSignal:
    """Stand-in for a Qt signal on a fake object: records connections."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)


class TestLocalServerName:
    def test_derives_from_project_key(self, tmp_path):
        assert instances.local_server_name(tmp_path) == (
            f"spar-gui-{instances.project_key(tmp_path)}"
        )

    def test_short_enough_for_af_unix(self, tmp_path):
        assert len(instances.local_server_name(tmp_path)) <= 40


class TestSingleInstanceGuard:
    def test_first_claim_wins(self, qtbot, tmp_path):
        guard = instances.SingleInstanceGuard(tmp_path)
        try:
            assert guard.try_claim() is True
        finally:
            guard.release()

    def test_second_claim_fails_and_delivers_exactly_one_raise(self, qtbot, tmp_path):
        """Review #7: try_claim() itself delivers the raise — the caller must
        NOT send a second one, or the owner's window bounces twice."""
        owner = instances.SingleInstanceGuard(tmp_path)
        second = instances.SingleInstanceGuard(tmp_path)
        raised = []
        owner.attach(lambda: raised.append(True))
        try:
            assert owner.try_claim() is True
            assert second.try_claim() is False
            qtbot.waitUntil(lambda: raised == [True], timeout=2000)
            # Give a stray second delivery a chance to show up, then prove
            # there was none.
            qtbot.wait(100)
            assert raised == [True]
        finally:
            second.release()
            owner.release()

    def test_raise_arriving_before_attach_is_replayed_once(self, qtbot, tmp_path):
        """Review #8: the guard listens before MainWindow exists, and window
        construction pumps the event loop (FilesView._wait_dir_loaded), so a
        raise can land with no receiver attached. It must be buffered and
        replayed by attach(), exactly once."""
        owner = instances.SingleInstanceGuard(tmp_path)
        second = instances.SingleInstanceGuard(tmp_path)
        try:
            assert owner.try_claim() is True
            assert second.try_claim() is False  # delivers the raise now
            # Simulate "still constructing the window": pump events with NO
            # receiver attached, exactly what FilesView does.
            qtbot.wait(200)
            raised = []
            owner.attach(lambda: raised.append(True))
            assert raised == [True]  # replayed, not lost
            qtbot.wait(100)
            assert raised == [True]  # and not replayed twice
        finally:
            second.release()
            owner.release()

    def test_different_projects_both_claim(self, qtbot, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        ga, gb = instances.SingleInstanceGuard(a), instances.SingleInstanceGuard(b)
        try:
            assert ga.try_claim() is True
            assert gb.try_claim() is True
        finally:
            ga.release()
            gb.release()

    def test_release_frees_the_name_for_a_later_claim(self, qtbot, tmp_path):
        first = instances.SingleInstanceGuard(tmp_path)
        assert first.try_claim() is True
        first.release()
        second = instances.SingleInstanceGuard(tmp_path)
        try:
            assert second.try_claim() is True
        finally:
            second.release()

    def test_release_by_a_non_owner_does_not_free_the_owner(self, qtbot, tmp_path):
        """Review #4: release() must be a no-op for a guard that never
        claimed — otherwise the loser of the race removes the WINNER's
        socket name and the next launch opens a duplicate window."""
        owner = instances.SingleInstanceGuard(tmp_path)
        loser = instances.SingleInstanceGuard(tmp_path)
        try:
            assert owner.try_claim() is True
            assert loser.try_claim() is False
            loser.release()  # must not touch the owner's name
            third = instances.SingleInstanceGuard(tmp_path)
            assert third.try_claim() is False  # owner still owns it
        finally:
            loser.release()
            owner.release()

    def test_request_raise_without_owner_is_false(self, qtbot, tmp_path):
        guard = instances.SingleInstanceGuard(tmp_path)
        assert guard.request_raise(timeout_ms=200) is False


class TestSingleInstanceGuardBranches:
    """Deterministic coverage of try_claim()'s error branches via injected
    factories — review #2/#3: the stale-socket path CANNOT be simulated with
    a real QLocalServer, because `removeServer()` succeeds against a LIVE
    server on Linux and both processes then listen on one name (verified
    empirically). Only injection proves the branch order."""

    class FakeServer:
        """Minimal QLocalServer stand-in: `listen` outcomes are scripted."""

        def __init__(self, outcomes, error, log):
            self._outcomes = list(outcomes)
            self._error = error
            self._log = log
            self.newConnection = _FakeSignal()
            self.closed = False

        def listen(self, name):
            self._log.append(("listen", name))
            return self._outcomes.pop(0)

        def serverError(self):
            return self._error

        def close(self):
            self.closed = True

    def _guard(self, tmp_path, outcomes, error, log, raise_delivered):
        server = TestSingleInstanceGuardBranches.FakeServer(outcomes, error, log)
        guard = instances.SingleInstanceGuard(
            tmp_path, server_factory=lambda _parent: server
        )
        guard.request_raise = lambda timeout_ms=1000: (
            log.append(("raise", timeout_ms)) or raise_delivered
        )
        return guard, server

    def test_first_listen_success_claims_without_probing(self, tmp_path):
        from PySide6.QtNetwork import QAbstractSocket

        log = []
        guard, _ = self._guard(
            tmp_path, [True], QAbstractSocket.SocketError.UnknownSocketError,
            log, raise_delivered=False,
        )
        assert guard.try_claim() is True
        assert [entry[0] for entry in log] == ["listen"]

    def test_address_in_use_with_live_owner_yields_the_slot(self, tmp_path):
        from PySide6.QtNetwork import QAbstractSocket

        log = []
        guard, _ = self._guard(
            tmp_path, [False], QAbstractSocket.SocketError.AddressInUseError,
            log, raise_delivered=True,
        )
        assert guard.try_claim() is False
        # Probed the owner, did NOT remove the name, did NOT listen again.
        assert [entry[0] for entry in log] == ["listen", "raise"]

    def test_address_in_use_with_dead_owner_removes_and_relistens(
        self, tmp_path, monkeypatch
    ):
        from PySide6.QtNetwork import QAbstractSocket, QLocalServer

        log = []
        removed = []
        monkeypatch.setattr(
            QLocalServer, "removeServer",
            staticmethod(lambda name: removed.append(name) or True),
        )
        guard, _ = self._guard(
            tmp_path, [False, True], QAbstractSocket.SocketError.AddressInUseError,
            log, raise_delivered=False,
        )
        assert guard.try_claim() is True
        assert removed == [instances.local_server_name(tmp_path)]
        assert [entry[0] for entry in log] == ["listen", "raise", "listen"]

    def test_unknown_listen_error_still_opens_the_window(self, tmp_path):
        """A broken socket layer must never make the gui unlaunchable."""
        from PySide6.QtNetwork import QAbstractSocket

        log = []
        guard, _ = self._guard(
            tmp_path, [False], QAbstractSocket.SocketError.SocketAccessError,
            log, raise_delivered=False,
        )
        assert guard.try_claim() is True
        assert [entry[0] for entry in log] == ["listen"]  # no probe, no remove
