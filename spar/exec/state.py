"""Execution state, per-Task remark ledger, and atomic persistence.

``exec.json`` under ``<spar_dir>`` is the FSM executor's (Task 7) and
cross-review loop's (Task 6) sole source of truth about a multi-task
execution run's progress. This module mirrors ``spar/state.py``'s
persistence patterns but models per-``Task`` state instead of a single
debate:

- ``TaskState`` — per-task status, branch, remark ledger, and session ids.
- ``ExecState`` — the full execution state: phase, target/integration
  branches, the ``tasks`` map, and an in-progress-turn marker.
- ``ExecStateStore`` — atomic save/load of ``exec.json``, plus a
  ``fcntl.flock``-based single-instance lock on ``<spar_dir>/lock``.

Remark (de)serialization (``StateRemark``/``ResolvedRemark``/``Severity``)
is reused from ``spar.state``/``spar.verdict`` rather than duplicated.
"""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

from spar.exec.tasklist import Task
from spar.state import (
    LockHeld,
    ResolvedRemark,
    StateError,
    StateRemark,
    _remark_from_dict,
    _remark_to_dict,
    _require_keys,
    _resolved_from_dict,
    _resolved_to_dict,
)

TaskStatus = Literal["pending", "ready", "implementing", "review", "testing", "merged"]


@dataclass
class TaskState:
    """Per-task execution state, including its own remark ledger."""

    task: Task
    status: TaskStatus = "pending"
    branch: str | None = None  # spar/<id>-<side> once created
    pending_remarks: list[StateRemark] = field(default_factory=list)
    resolved_remarks: list[ResolvedRemark] = field(default_factory=list)
    next_remark_id: int = 1
    impl_session_id: str | None = None  # implementing side's exec-phase session
    review_session_id: str | None = None  # reviewing side's exec-phase session


@dataclass
class ExecState:
    """The full execution state — the one object the FSM executor mutates."""

    phase: Literal["execution", "test", "done"] = "execution"
    target_branch: str = ""
    target_base_oid: str = ""
    integration_branch: str = "spar/integration"
    tasks: dict[str, TaskState] = field(default_factory=dict)
    turn_in_progress: dict | None = None  # {"task_id","role","hash_before"}
    fix_tasks_opened: int = 0  # integration-fix tasks opened so far (§7 cap)

    def mark_ready(self) -> None:
        """Promote every ``pending`` task whose deps are all ``merged`` to ``ready``."""
        for task_state in self.tasks.values():
            if task_state.status != "pending":
                continue
            if all(self.tasks[dep].status == "merged" for dep in task_state.task.deps):
                task_state.status = "ready"

    def next_task(self) -> TaskState | None:
        """Return the first ``ready`` task in id order, or ``None``."""
        ready = [ts for ts in self.tasks.values() if ts.status == "ready"]
        if not ready:
            return None
        return min(ready, key=lambda ts: ts.task.id)

    def all_merged(self) -> bool:
        """Return ``True`` iff every task is ``merged`` (``False`` if there are none)."""
        return bool(self.tasks) and all(ts.status == "merged" for ts in self.tasks.values())


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------


def _task_to_dict(task: Task) -> dict:
    return {
        "id": task.id,
        "description": task.description,
        "side": task.side,
        "model": task.model,
        "review_model": task.review_model,
        "deps": list(task.deps),
        "files": list(task.files),
        "test": task.test,
    }


def _task_from_dict(data: Any) -> Task:
    _require_keys(
        data,
        ["id", "description", "side", "model", "review_model", "deps", "files", "test"],
        "task",
    )
    return Task(
        id=data["id"],
        description=data["description"],
        side=data["side"],
        model=data["model"],
        review_model=data["review_model"],
        deps=tuple(data["deps"]),
        files=tuple(data["files"]),
        test=data["test"],
    )


def _task_state_to_dict(task_state: TaskState) -> dict:
    return {
        "task": _task_to_dict(task_state.task),
        "status": task_state.status,
        "branch": task_state.branch,
        "pending_remarks": [_remark_to_dict(r) for r in task_state.pending_remarks],
        "resolved_remarks": [_resolved_to_dict(r) for r in task_state.resolved_remarks],
        "next_remark_id": task_state.next_remark_id,
        "impl_session_id": task_state.impl_session_id,
        "review_session_id": task_state.review_session_id,
    }


def _task_state_from_dict(data: Any) -> TaskState:
    _require_keys(
        data,
        [
            "task",
            "status",
            "branch",
            "pending_remarks",
            "resolved_remarks",
            "next_remark_id",
            "impl_session_id",
            "review_session_id",
        ],
        "task state",
    )

    pending_raw = data["pending_remarks"]
    if not isinstance(pending_raw, list):
        raise StateError(f"'pending_remarks' must be a list, got {type(pending_raw).__name__}")

    resolved_raw = data["resolved_remarks"]
    if not isinstance(resolved_raw, list):
        raise StateError(f"'resolved_remarks' must be a list, got {type(resolved_raw).__name__}")

    return TaskState(
        task=_task_from_dict(data["task"]),
        status=data["status"],
        branch=data["branch"],
        pending_remarks=[_remark_from_dict(r) for r in pending_raw],
        resolved_remarks=[_resolved_from_dict(r) for r in resolved_raw],
        next_remark_id=data["next_remark_id"],
        impl_session_id=data["impl_session_id"],
        review_session_id=data["review_session_id"],
    )


_EXEC_STATE_KEYS = [
    "phase",
    "target_branch",
    "target_base_oid",
    "integration_branch",
    "tasks",
    "turn_in_progress",
]


def _exec_state_to_dict(state: ExecState) -> dict:
    return {
        "phase": state.phase,
        "target_branch": state.target_branch,
        "target_base_oid": state.target_base_oid,
        "integration_branch": state.integration_branch,
        "tasks": {task_id: _task_state_to_dict(ts) for task_id, ts in state.tasks.items()},
        "turn_in_progress": state.turn_in_progress,
        "fix_tasks_opened": state.fix_tasks_opened,
    }


def _exec_state_from_dict(data: Any) -> ExecState:
    _require_keys(data, _EXEC_STATE_KEYS, "execution state")

    tasks_raw = data["tasks"]
    if not isinstance(tasks_raw, dict):
        raise StateError(f"'tasks' must be an object, got {type(tasks_raw).__name__}")

    return ExecState(
        phase=data["phase"],
        target_branch=data["target_branch"],
        target_base_oid=data["target_base_oid"],
        integration_branch=data["integration_branch"],
        tasks={task_id: _task_state_from_dict(ts) for task_id, ts in tasks_raw.items()},
        turn_in_progress=data["turn_in_progress"],
        # Tolerant default (not in _EXEC_STATE_KEYS): a pre-upgrade exec.json
        # without the key must still load.
        fix_tasks_opened=data.get("fix_tasks_opened", 0),
    )


# --------------------------------------------------------------------------
# ExecStateStore
# --------------------------------------------------------------------------


class ExecStateStore:
    """Manages ``<spar_dir>/exec.json`` and ``<spar_dir>/lock``."""

    def __init__(self, spar_dir: Path):
        self.spar_dir = Path(spar_dir)
        self.exec_path = self.spar_dir / "exec.json"
        self.lock_path = self.spar_dir / "lock"
        self._lock_fd: int | None = None

    # -- persistence --------------------------------------------------

    def save(self, state: ExecState) -> None:
        """Atomically write ``state`` to ``exec.json``.

        Serializes to JSON, writes to ``exec.json.tmp``, fsyncs it, then
        ``os.replace``s it onto ``exec.json``. A prior corrupt/garbage
        ``exec.json`` (e.g. left by a crash) is simply overwritten — the
        replace is unconditional.
        """
        self.spar_dir.mkdir(parents=True, exist_ok=True)
        data = _exec_state_to_dict(state)
        tmp_path = self.spar_dir / "exec.json.tmp"

        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

        os.replace(tmp_path, self.exec_path)

    def load(self) -> ExecState:
        """Read and deserialize ``exec.json``.

        Raises:
            StateError: if the file is missing, contains malformed JSON,
                references an unknown severity name, or is missing a
                required key.
        """
        if not self.exec_path.exists():
            raise StateError(f"execution state file not found: {self.exec_path}")

        try:
            raw = self.exec_path.read_text(encoding="utf-8")
        except OSError as e:
            raise StateError(f"failed to read execution state file {self.exec_path}: {e}") from e

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise StateError(f"malformed JSON in execution state file {self.exec_path}: {e}") from e

        return _exec_state_from_dict(data)

    def exists(self) -> bool:
        return self.exec_path.exists()

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
    def locked(self) -> Iterator["ExecStateStore"]:
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
