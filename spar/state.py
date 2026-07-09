"""Debate state, atomic persistence, single-instance lock, and crash recovery.

``session.json`` under ``<spar_dir>`` is the orchestrator's sole source of
truth about a debate's progress. This module provides:

- ``DebateState`` and its component dataclasses (the in-memory model).
- ``StateStore`` — atomic save/load of ``session.json``, plus a
  ``fcntl.flock``-based single-instance lock on ``<spar_dir>/lock``.
- ``hash_artifact`` — content hash helper used to detect artifact changes.
- ``check_recovery`` — classifies state left behind by a crashed/killed
  process into "clean", "repeat_turn", or "artifact_changed".

Locking notes: the lock is held for the lifetime of the process via
``fcntl.flock(LOCK_EX | LOCK_NB)``. The kernel releases the lock
automatically when the holding process dies (including SIGKILL), so there
is no stale-lock problem and no lock-takeover protocol is needed. The pid
and start time written into the lock file are purely informational (used
in the ``LockHeld`` error message); they are never used for lock logic.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from spar.verdict import Severity


class StateError(Exception):
    """Raised when session state cannot be loaded/parsed, or hashing fails."""


class LockHeld(Exception):
    """Raised when the single-instance lock on ``<spar_dir>/lock`` is held."""


@dataclass(frozen=True)
class StateRemark:
    """A single remark tracked in persisted state."""

    remark_id: int
    severity: Severity
    author: str  # side name or "user"
    text: str


@dataclass(frozen=True)
class ResolvedRemark:
    """A remark that has been accepted or rejected."""

    remark: StateRemark
    resolution: str  # "accepted" | "rejected"
    justification: str = ""  # non-empty iff rejected


@dataclass(frozen=True)
class SideState:
    """Per-side (per-agent) persisted state."""

    session_id: str | None = None
    last_verdict_status: str | None = None  # "AGREE" | "CONTINUE" | None
    last_verdict_artifact_hash: str | None = None


@dataclass(frozen=True)
class TurnInProgress:
    """Marker written before a turn starts, cleared when it completes."""

    side: str
    artifact_hash_before: str


@dataclass
class DebateState:
    """The full debate state — the one object the orchestrator mutates."""

    round: int = 0
    last_actor: str | None = None
    artifact_hash: str = ""
    turn_in_progress: TurnInProgress | None = None
    sides: dict[str, SideState] = field(default_factory=dict)
    pending_remarks: list[StateRemark] = field(default_factory=list)
    resolved_remarks: list[ResolvedRemark] = field(default_factory=list)
    next_remark_id: int = 1
    pending_gate: dict | None = None


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------


def _require_keys(obj: Any, keys: list[str], context: str) -> None:
    if not isinstance(obj, dict):
        raise StateError(f"expected an object for {context}, got {type(obj).__name__}")
    missing = [k for k in keys if k not in obj]
    if missing:
        raise StateError(f"missing required key(s) {missing} in {context}")


def _severity_from_name(name: Any) -> Severity:
    if not isinstance(name, str):
        raise StateError(f"unknown severity: {name!r}")
    try:
        return Severity[name]
    except KeyError:
        raise StateError(f"unknown severity: {name!r}") from None


def _remark_to_dict(remark: StateRemark) -> dict:
    return {
        "remark_id": remark.remark_id,
        "severity": remark.severity.name,
        "author": remark.author,
        "text": remark.text,
    }


def _remark_from_dict(data: Any) -> StateRemark:
    _require_keys(data, ["remark_id", "severity", "author", "text"], "remark")
    return StateRemark(
        remark_id=data["remark_id"],
        severity=_severity_from_name(data["severity"]),
        author=data["author"],
        text=data["text"],
    )


def _resolved_to_dict(resolved: ResolvedRemark) -> dict:
    return {
        "remark": _remark_to_dict(resolved.remark),
        "resolution": resolved.resolution,
        "justification": resolved.justification,
    }


def _resolved_from_dict(data: Any) -> ResolvedRemark:
    _require_keys(data, ["remark", "resolution"], "resolved_remarks entry")
    return ResolvedRemark(
        remark=_remark_from_dict(data["remark"]),
        resolution=data["resolution"],
        justification=data.get("justification", ""),
    )


def _side_to_dict(side: SideState) -> dict:
    return {
        "session_id": side.session_id,
        "last_verdict_status": side.last_verdict_status,
        "last_verdict_artifact_hash": side.last_verdict_artifact_hash,
    }


def _side_from_dict(data: Any) -> SideState:
    _require_keys(
        data,
        ["session_id", "last_verdict_status", "last_verdict_artifact_hash"],
        "sides entry",
    )
    return SideState(
        session_id=data["session_id"],
        last_verdict_status=data["last_verdict_status"],
        last_verdict_artifact_hash=data["last_verdict_artifact_hash"],
    )


def _turn_to_dict(turn: TurnInProgress | None) -> dict | None:
    if turn is None:
        return None
    return {"side": turn.side, "artifact_hash_before": turn.artifact_hash_before}


def _turn_from_dict(data: Any) -> TurnInProgress | None:
    if data is None:
        return None
    _require_keys(data, ["side", "artifact_hash_before"], "turn_in_progress")
    return TurnInProgress(side=data["side"], artifact_hash_before=data["artifact_hash_before"])


_STATE_KEYS = [
    "round",
    "last_actor",
    "artifact_hash",
    "turn_in_progress",
    "sides",
    "pending_remarks",
    "resolved_remarks",
    "next_remark_id",
]


def _state_to_dict(state: DebateState) -> dict:
    return {
        "round": state.round,
        "last_actor": state.last_actor,
        "artifact_hash": state.artifact_hash,
        "turn_in_progress": _turn_to_dict(state.turn_in_progress),
        "sides": {name: _side_to_dict(side) for name, side in state.sides.items()},
        "pending_remarks": [_remark_to_dict(r) for r in state.pending_remarks],
        "resolved_remarks": [_resolved_to_dict(r) for r in state.resolved_remarks],
        "next_remark_id": state.next_remark_id,
        "pending_gate": state.pending_gate,
    }


def _state_from_dict(data: Any) -> DebateState:
    _require_keys(data, _STATE_KEYS, "session state")

    sides_raw = data["sides"]
    if not isinstance(sides_raw, dict):
        raise StateError(f"'sides' must be an object, got {type(sides_raw).__name__}")

    pending_raw = data["pending_remarks"]
    if not isinstance(pending_raw, list):
        raise StateError(f"'pending_remarks' must be a list, got {type(pending_raw).__name__}")

    resolved_raw = data["resolved_remarks"]
    if not isinstance(resolved_raw, list):
        raise StateError(f"'resolved_remarks' must be a list, got {type(resolved_raw).__name__}")

    return DebateState(
        round=data["round"],
        last_actor=data["last_actor"],
        artifact_hash=data["artifact_hash"],
        turn_in_progress=_turn_from_dict(data["turn_in_progress"]),
        sides={name: _side_from_dict(side) for name, side in sides_raw.items()},
        pending_remarks=[_remark_from_dict(r) for r in pending_raw],
        resolved_remarks=[_resolved_from_dict(r) for r in resolved_raw],
        next_remark_id=data["next_remark_id"],
        # Tolerant default (not in _STATE_KEYS): a pre-upgrade session.json
        # without the key must still load.
        pending_gate=data.get("pending_gate"),
    )


# --------------------------------------------------------------------------
# StateStore
# --------------------------------------------------------------------------


class StateStore:
    """Manages ``<spar_dir>/session.json`` and ``<spar_dir>/lock``."""

    def __init__(self, spar_dir: Path):
        self.spar_dir = Path(spar_dir)
        self.session_path = self.spar_dir / "session.json"
        self.lock_path = self.spar_dir / "lock"
        self._lock_fd: int | None = None

    # -- persistence --------------------------------------------------

    def save(self, state: DebateState) -> None:
        """Atomically write ``state`` to ``session.json``.

        Serializes to JSON, writes to ``session.json.tmp``, fsyncs it, then
        ``os.replace``s it onto ``session.json``. A prior corrupt/garbage
        ``session.json`` (e.g. left by a crash) is simply overwritten — the
        replace is unconditional.
        """
        self.spar_dir.mkdir(parents=True, exist_ok=True)
        data = _state_to_dict(state)
        tmp_path = self.spar_dir / "session.json.tmp"

        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        os.replace(tmp_path, self.session_path)

    def load(self) -> DebateState:
        """Read and deserialize ``session.json``.

        Raises:
            StateError: if the file is missing, contains malformed JSON,
                references an unknown severity name, or is missing a
                required key.
        """
        if not self.session_path.exists():
            raise StateError(f"session file not found: {self.session_path}")

        try:
            raw = self.session_path.read_text(encoding="utf-8")
        except OSError as e:
            raise StateError(f"failed to read session file {self.session_path}: {e}") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise StateError(f"malformed JSON in session file {self.session_path}: {e}") from e

        return _state_from_dict(data)

    def exists(self) -> bool:
        return self.session_path.exists()

    # -- locking --------------------------------------------------------

    def acquire_lock(self) -> None:
        """Acquire the single-instance lock, raising ``LockHeld`` if busy."""
        self.spar_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as e:
            info = self._read_lock_info(fd)
            os.close(fd)
            raise LockHeld(f"lock is already held ({info}): {self.lock_path}") from e

        self._lock_fd = fd
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        payload = json.dumps(
            {"pid": os.getpid(), "started": datetime.now(timezone.utc).isoformat()}
        ).encode("utf-8")
        os.write(fd, payload)

    def release_lock(self) -> None:
        """Release the lock (idempotent)."""
        if self._lock_fd is None:
            return
        fd = self._lock_fd
        self._lock_fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    @contextmanager
    def locked(self) -> Iterator["StateStore"]:
        """Context manager: acquire on enter, release on exit (even on error)."""
        self.acquire_lock()
        try:
            yield self
        finally:
            self.release_lock()

    @staticmethod
    def _read_lock_info(fd: int) -> str:
        """Best-effort, informational read of the lock file's pid/started."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
        except OSError:
            return "unknown holder"
        if not raw:
            return "unknown holder"
        try:
            info = json.loads(raw)
            pid = info.get("pid", "?")
            started = info.get("started", "?")
            return f"pid={pid}, started={started}"
        except json.JSONDecodeError:
            return "unknown holder"


# --------------------------------------------------------------------------
# Artifact hashing
# --------------------------------------------------------------------------


def hash_artifact(path: Path) -> str:
    """Return ``"sha256:" + hexdigest`` of the file's bytes.

    Raises:
        StateError: if the file is missing or unreadable.
    """
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        raise StateError(f"cannot hash artifact, unreadable file: {path}: {e}") from e
    return "sha256:" + hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------
# Crash recovery
# --------------------------------------------------------------------------


def check_recovery(state: DebateState, artifact_path: Path) -> str:
    """Classify recovery status against the current artifact on disk.

    Returns one of:
        "clean" — no turn was in progress.
        "repeat_turn" — a turn was in progress but the artifact is
            unchanged from before it started (it died before modifying
            anything); the orchestrator can safely repeat the turn.
        "artifact_changed" — a turn was in progress and the artifact hash
            differs; the orchestrator must ask the user how to proceed.
    """
    if state.turn_in_progress is None:
        return "clean"

    current_hash = hash_artifact(artifact_path)
    if current_hash == state.turn_in_progress.artifact_hash_before:
        return "repeat_turn"
    return "artifact_changed"
