"""Tests for spar.state: persistence, locking, hashing, and recovery."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from spar.state import (
    DebateState,
    LockHeld,
    ResolvedRemark,
    SideState,
    StateError,
    StateRemark,
    StateStore,
    TurnInProgress,
    check_recovery,
    hash_artifact,
)
from spar.verdict import Severity


def _minimal_valid_dict() -> dict:
    """A minimal but fully valid session-state dict, for error-path tests."""
    return {
        "round": 0,
        "last_actor": None,
        "artifact_hash": "",
        "turn_in_progress": None,
        "sides": {},
        "pending_remarks": [],
        "resolved_remarks": [],
        "next_remark_id": 1,
    }


def _full_state() -> DebateState:
    return DebateState(
        round=5,
        last_actor="codex",
        artifact_hash="sha256:" + "a" * 64,
        turn_in_progress=TurnInProgress(side="claude", artifact_hash_before="sha256:" + "b" * 64),
        sides={
            "claude": SideState(
                session_id="sess-1",
                last_verdict_status="AGREE",
                last_verdict_artifact_hash="sha256:" + "c" * 64,
            ),
            "codex": SideState(),
        },
        pending_remarks=[
            StateRemark(
                remark_id=7,
                severity=Severity.MUST,
                author="codex",
                text="No rollback strategy — uses “curly quotes” and emoji ✅",
            ),
            StateRemark(
                remark_id=8,
                severity=Severity.NICE,
                author="claude",
                text="feature flag über alles",
            ),
            StateRemark(
                remark_id=9,
                severity=Severity.USER,
                author="user",
                text="用户备注：请再检查一下",
            ),
        ],
        resolved_remarks=[
            ResolvedRemark(
                remark=StateRemark(remark_id=3, severity=Severity.MUST, author="claude", text="fix"),
                resolution="accepted",
            ),
            ResolvedRemark(
                remark=StateRemark(
                    remark_id=4, severity=Severity.NICE, author="codex", text="consider caching"
                ),
                resolution="rejected",
                justification="adds complexity for marginal gain",
            ),
        ],
        next_remark_id=10,
    )


class TestRoundTrip:
    def test_full_state_round_trip_is_lossless(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        state = _full_state()

        store = StateStore(spar_dir)
        store.save(state)
        loaded = store.load()

        assert loaded == state

    def test_default_empty_state_round_trip(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        state = DebateState()

        store = StateStore(spar_dir)
        store.save(state)
        loaded = store.load()

        assert loaded == state

    def test_exists(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        store = StateStore(spar_dir)
        assert store.exists() is False
        store.save(DebateState())
        assert store.exists() is True


class TestAtomicSave:
    def test_no_tmp_file_left_after_save(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        store = StateStore(spar_dir)
        store.save(DebateState(round=1, artifact_hash="sha256:abc"))

        assert not (spar_dir / "session.json.tmp").exists()
        assert (spar_dir / "session.json").exists()

    def test_save_overwrites_garbage_existing_file_cleanly(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        (spar_dir / "session.json").write_bytes(b"{not: valid json!!! \x00\x01 garbage")

        store = StateStore(spar_dir)
        state = DebateState(round=2, artifact_hash="sha256:def")
        store.save(state)

        assert not (spar_dir / "session.json.tmp").exists()
        loaded = store.load()
        assert loaded == state

    def test_creates_spar_dir_if_missing(self, tmp_path):
        spar_dir = tmp_path / "does" / "not" / "exist"
        store = StateStore(spar_dir)
        store.save(DebateState())
        assert store.exists()


class TestLoadErrors:
    def test_missing_file_raises_state_error(self, tmp_path):
        store = StateStore(tmp_path / "spar")
        with pytest.raises(StateError, match="not found"):
            store.load()

    def test_malformed_json_raises_state_error(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        (spar_dir / "session.json").write_text("{this is not json")
        store = StateStore(spar_dir)
        with pytest.raises(StateError, match="malformed JSON"):
            store.load()

    def test_unknown_severity_raises_state_error(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        data = _minimal_valid_dict()
        data["pending_remarks"] = [
            {"remark_id": 1, "severity": "BOGUS", "author": "x", "text": "y"}
        ]
        (spar_dir / "session.json").write_text(json.dumps(data))
        store = StateStore(spar_dir)
        with pytest.raises(StateError, match="unknown severity"):
            store.load()

    def test_missing_top_level_key_raises_state_error(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        data = _minimal_valid_dict()
        del data["round"]
        (spar_dir / "session.json").write_text(json.dumps(data))
        store = StateStore(spar_dir)
        with pytest.raises(StateError, match="round"):
            store.load()

    def test_missing_nested_key_raises_state_error(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        data = _minimal_valid_dict()
        data["sides"] = {"claude": {"session_id": "x"}}  # missing verdict fields
        (spar_dir / "session.json").write_text(json.dumps(data))
        store = StateStore(spar_dir)
        with pytest.raises(StateError):
            store.load()


class TestLocking:
    def test_second_store_same_process_raises_lock_held(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        store1 = StateStore(spar_dir)
        store2 = StateStore(spar_dir)

        store1.acquire_lock()
        try:
            with pytest.raises(LockHeld):
                store2.acquire_lock()
        finally:
            store1.release_lock()

    def test_lock_file_contains_pid_and_started(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        store = StateStore(spar_dir)
        store.acquire_lock()
        try:
            content = json.loads((spar_dir / "lock").read_text())
            assert content["pid"] == os.getpid()
            assert "started" in content
        finally:
            store.release_lock()

    def test_release_then_reacquire_succeeds(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        store = StateStore(spar_dir)

        store.acquire_lock()
        store.release_lock()
        store.acquire_lock()  # must not raise
        store.release_lock()

    def test_release_lock_is_idempotent(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        store = StateStore(spar_dir)
        store.acquire_lock()
        store.release_lock()
        store.release_lock()  # no error

    def test_locked_context_manager_releases_on_exception(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        store = StateStore(spar_dir)

        with pytest.raises(RuntimeError):
            with store.locked():
                raise RuntimeError("boom")

        # Lock must have been released -> a fresh store can acquire it.
        store2 = StateStore(spar_dir)
        store2.acquire_lock()
        store2.release_lock()

    def test_locked_context_manager_releases_on_normal_exit(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        store = StateStore(spar_dir)

        with store.locked():
            pass

        store2 = StateStore(spar_dir)
        store2.acquire_lock()
        store2.release_lock()

    def test_subprocess_holds_lock_then_releases_on_exit(self, tmp_path):
        spar_dir = tmp_path / "spar"
        spar_dir.mkdir()
        ready_file = tmp_path / "ready"
        lock_path = spar_dir / "lock"

        script = (
            "import fcntl, os, time\n"
            f"fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR, 0o644)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            f"open({str(ready_file)!r}, 'w').write('ready')\n"
            "time.sleep(0.4)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", script])
        try:
            deadline = time.time() + 5
            while not ready_file.exists():
                if time.time() > deadline:
                    pytest.fail("child process never signaled ready")
                time.sleep(0.02)

            store = StateStore(spar_dir)
            with pytest.raises(LockHeld):
                store.acquire_lock()

            proc.wait(timeout=5)

            # Kernel released the child's lock on process exit.
            store.acquire_lock()
            store.release_lock()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


class TestHashArtifact:
    def test_known_content_known_hash(self, tmp_path):
        f = tmp_path / "artifact.md"
        f.write_bytes("hello world".encode("utf-8"))
        expected = "sha256:" + hashlib.sha256(b"hello world").hexdigest()
        assert hash_artifact(f) == expected

    def test_missing_file_raises_state_error(self, tmp_path):
        with pytest.raises(StateError):
            hash_artifact(tmp_path / "does-not-exist.md")


class TestCheckRecovery:
    def test_clean_when_no_turn_in_progress(self, tmp_path):
        state = DebateState()
        assert check_recovery(state, tmp_path / "irrelevant.md") == "clean"

    def test_repeat_turn_when_hash_unchanged(self, tmp_path):
        artifact = tmp_path / "artifact.md"
        artifact.write_text("content")
        h = hash_artifact(artifact)
        state = DebateState(turn_in_progress=TurnInProgress(side="claude", artifact_hash_before=h))
        assert check_recovery(state, artifact) == "repeat_turn"

    def test_artifact_changed_when_hash_differs(self, tmp_path):
        artifact = tmp_path / "artifact.md"
        artifact.write_text("content")
        h = hash_artifact(artifact)
        state = DebateState(turn_in_progress=TurnInProgress(side="claude", artifact_hash_before=h))

        artifact.write_text("different content now")
        assert check_recovery(state, artifact) == "artifact_changed"
