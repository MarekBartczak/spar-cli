"""``SparRunner`` — the GUI's process pilot for the spar engine.

The runner owns *at most one* :class:`QProcess` at a time and drives it
through the spar CLI (``python -m spar.cli ...``). Everything here is scoped
to a single ``project_dir`` chosen once at GUI launch:

* every spawn calls ``setWorkingDirectory(project_dir)`` — the engine writes
  its state under a *cwd-relative* ``.spar`` (see ``StreamSink(Path(".spar"))``
  in ``spar/cli.py``), so the child MUST run with cwd == ``project_dir`` or it
  would read/write the wrong project (review #1);
* the lock probe, status reads and stale-``exec.json`` archival all use
  absolute paths under ``project_dir / ".spar"`` — never cwd-relative
  (reviews #2, #6).

``derive_state`` is a *pure* function (no Qt, no I/O) mapping
``(alive, exit_code, status)`` to a :class:`RunnerState`; it is unit-tested
exhaustively against the full exit-code table. POSIX-only by design: stop
sends a real ``SIGINT`` via ``os.kill`` (review #3) because Qt's
``terminate()``/``kill()`` send SIGTERM/SIGKILL, which the engine does not
treat as a clean interrupt.
"""

from __future__ import annotations

import fcntl
import os
import signal
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, Signal

from spar.status import build_status

__all__ = ["RunnerState", "derive_state", "SparRunner"]


def _engine_base_command() -> list[str]:
    """Return the command used by the GUI to start its headless engine.

    A normal source/pip installation can spawn the package with ``-m`` on
    every supported POSIX platform.  A PyInstaller bundle cannot: in a frozen
    process ``sys.executable`` is the bundled application executable, not a
    Python interpreter.  The macOS bundle entry point owns the private switch
    below and routes it straight to :func:`spar.cli.main`.

    Keeping the distinction here means the engine and all regular Linux paths
    remain platform-neutral; only the packaging entry point knows how a macOS
    app bundle starts.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--spar-engine"]
    return [sys.executable, "-m", "spar.cli"]


class RunnerState(Enum):
    """Lifecycle state of the spar process, driving toolbar enablement."""

    IDLE = "idle"
    RUNNING = "running"
    GATE_PENDING = "gate_pending"
    RESUMABLE = "resumable"
    ABORTED = "aborted"
    DONE = "done"
    ERROR = "error"
    LOCKED = "locked"


def derive_state(
    alive: bool, exit_code: int | None, status: dict, lock_held: bool = False
) -> RunnerState:
    """Map process liveness + last exit code + on-disk status to a state.

    Pure: no Qt, no filesystem. The full table (review #7):

    * alive, no pending gate      -> ``RUNNING``
    * alive, pending gate         -> ``GATE_PENDING``
    * exit 10 / not-alive+pending -> ``GATE_PENDING``
    * exit 130 / 4 / crash (<0)   -> ``RESUMABLE``
    * exit 5                      -> ``ABORTED``
    * exit 2                      -> ``ERROR``
    * exit 3                      -> ``LOCKED``
    * exit 0                      -> ``DONE``
    * no exit this session:
        pending gate              -> ``GATE_PENDING``
        phase == "done"           -> ``DONE``
        phase is None (fresh)     -> ``IDLE``
        any other phase           -> ``RESUMABLE`` (interrupted, resumable)
    """
    pending = bool(status.get("pending_gate"))

    if alive:
        return RunnerState.GATE_PENDING if pending else RunnerState.RUNNING

    # Exit 3 covers BOTH "another process holds the lock" and plain
    # state-guard refusals (dirty tree, leftovers, missing plan). Only a
    # CONFIRMED foreign lock is read-only LOCKED (live finding: a dirty-tree
    # refusal froze the whole toolbar); otherwise the status-based mapping
    # below applies, keeping the toolbar actionable.
    if exit_code == 3:
        exit_code = None if not lock_held else exit_code

    if exit_code is not None:
        if exit_code == 3:
            return RunnerState.LOCKED
        if exit_code == 2:
            return RunnerState.ERROR
        if exit_code == 5:
            return RunnerState.ABORTED
        if exit_code == 10:
            return RunnerState.GATE_PENDING
        if exit_code in (130, 4) or exit_code < 0:
            return RunnerState.RESUMABLE
        if exit_code == 0:
            return RunnerState.DONE
        # Any other non-zero code: interrupted but resumable.
        return RunnerState.RESUMABLE

    # No process ran this session — derive from persisted status alone.
    if pending:
        return RunnerState.GATE_PENDING
    phase = status.get("phase")
    if phase == "done":
        return RunnerState.DONE
    if phase is None:
        return RunnerState.IDLE
    return RunnerState.RESUMABLE


# Sentinel exit code used internally to represent a crashed child (QProcess
# CrashExit) so ``derive_state`` routes it to RESUMABLE via the ``< 0`` arm.
_CRASH_EXIT = -1


class SparRunner(QObject):
    """Owns one spar :class:`QProcess` and its lifecycle for ``project_dir``."""

    started = Signal(str)  # the full command line that was spawned
    finished = Signal(int)  # the child's exit code (or _CRASH_EXIT on crash)
    state_changed = Signal(object)  # RunnerState
    #: Human-readable one-off announcements (double-start guard rejections,
    #: the auto-exec chain kicking off, ...) -- the gui surfaces these as
    #: notice lines in the StreamPane (smoke-feedback round 2, fixes 1/2).
    notice = Signal(str)

    #: Base command; overridable in tests to point at a fake spar script.
    def __init__(self, project_dir: "str | Path", parent: QObject | None = None):
        super().__init__(parent)
        self.project_dir = Path(project_dir)
        self.spar_dir = self.project_dir / ".spar"

        # ``[program, *fixed_args]`` prepended to every spawn. Tests replace
        # this with ``[sys.executable, "<fake>.py"]`` to inject a fake engine.
        self._base_cmd: list[str] = _engine_base_command()

        self._process: QProcess | None = None
        self._last_exit: int | None = None
        self._auto_exec = False
        self._remarks_path: Path | None = None
        self._task_file_path: Path | None = None
        self._state: RunnerState | None = None

        self._tmp_dir = Path(tempfile.mkdtemp(prefix="spar-gui-"))

        # Poll the derived state so external changes (a lock freeing, a
        # sibling process finishing) are reflected without an explicit event.
        self._poll = QTimer(self)
        self._poll.setInterval(750)
        self._poll.timeout.connect(self._refresh_state)
        self._poll.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start_debate(
        self, task_text: str, sides, first: str, tasks: bool
    ) -> None:
        """Spawn a fresh debate, writing ``task_text`` to a temp task-file.

        If the persisted status is a *finished* execution (``phase == "done"``)
        the stale ``exec.json`` is archived first (review #6) so status/resume
        routing flips back to the new debate instead of the old exec.

        No-ops (with a :attr:`notice`) if a child is already alive (fix 2).
        """
        if self._guard_busy("nowa debata"):
            return
        self._archive_stale_exec()

        task_path = self._tmp_dir / f"task-{int(time.time() * 1000)}.md"
        task_path.write_text(task_text, encoding="utf-8")
        self._task_file_path = task_path

        sides_str = sides if isinstance(sides, str) else ",".join(sides)
        args = [
            "--task-file", str(task_path),
            "--sides", sides_str,
            "--first", first,
            "--headless", "--quiet",
        ]
        if tasks:
            args.append("--tasks")
        self._spawn(args)

    def start_exec(self) -> None:
        """Spawn a fresh ``spar exec`` run over the agreed plan's tasks.

        No-ops (with a :attr:`notice`) if a child is already alive (fix 2) --
        this is the guard that prevents a manual "Start exec" click racing
        the auto-exec chain (see :meth:`_on_finished`) into a lock error.
        """
        if self._guard_busy("start exec"):
            return
        self._spawn(["exec", "--headless", "--quiet"])

    def resume(self, gate_value: str | None, auto_exec: bool = False) -> None:
        """Resume the interrupted run, routing debate vs exec by phase.

        ``phase`` ``None``/``"debate"`` -> ``spar --continue``; any exec phase
        -> ``spar exec --continue``. ``gate_value`` (when given) is passed as
        ``--gate``. ``auto_exec`` is only meaningful for a consensus *accept*:
        on a clean (exit 0) finish it chains :meth:`start_exec` (review #6).

        No-ops (with a :attr:`notice`) if a child is already alive (fix 2).
        """
        if self._guard_busy("wznów"):
            return
        self._auto_exec = bool(auto_exec)

        phase = self._read_status().get("phase")
        if phase in (None, "debate"):
            args = ["--continue", "--headless", "--quiet"]
        else:
            args = ["exec", "--continue", "--headless", "--quiet"]
        if gate_value is not None:
            args += ["--gate", gate_value]
        self._spawn(args)

    def resume_with_remarks(self, text: str) -> None:
        """Write ``text`` (one remark per line) to a temp file and resume.

        The runner owns the file: it is created WITHOUT delete-on-close (the
        child opens it after spawn) and is unlinked in :meth:`_on_finished`
        (review #4).

        No-ops (with a :attr:`notice`) if a child is already alive (fix 2) --
        checked up front so a busy runner never even writes the orphaned
        remarks temp file.
        """
        if self._guard_busy("wznów"):
            return
        remarks_path = self._tmp_dir / f"remarks-{int(time.time() * 1000)}.txt"
        body = text if text.endswith("\n") else text + "\n"
        remarks_path.write_text(body, encoding="utf-8")
        self._remarks_path = remarks_path
        self.resume(f"remarks:{remarks_path}")

    def stop(self) -> None:
        """Interrupt the child with a real POSIX ``SIGINT`` and wait (review #3).

        Qt's ``terminate()``/``kill()`` map to SIGTERM/SIGKILL, which the engine
        does not treat as a clean interrupt; ``SIGINT`` triggers the engine's
        KeyboardInterrupt path (exit 130 with state saved).
        """
        proc = self._process
        if proc is None or proc.state() == QProcess.ProcessState.NotRunning:
            return
        pid = proc.processId()
        if pid > 0:
            os.kill(pid, signal.SIGINT)
        proc.waitForFinished(5000)

    def probe_lock(self) -> bool:
        """Return ``True`` if another process holds ``project_dir/.spar/lock``.

        A real non-blocking ``flock`` attempt on the absolute lock path (never
        cwd-relative — review #2). ``spar status`` never touches the lock, so
        this probe is the only way to notice a running sibling.
        """
        lock_path = self.spar_dir / "lock"
        if not lock_path.exists():
            return False
        try:
            fd = os.open(str(lock_path), os.O_RDWR)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True  # held by someone else
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)

    def current_state(self) -> RunnerState:
        """Compute the live state (probes lock, reads status)."""
        alive = self._is_alive()
        if not alive and self.probe_lock():
            return RunnerState.LOCKED
        return derive_state(
            alive, self._last_exit, self._read_status(), lock_held=self.probe_lock()
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _is_alive(self) -> bool:
        return (
            self._process is not None
            and self._process.state() != QProcess.ProcessState.NotRunning
        )

    def _guard_busy(self, action: str) -> bool:
        """Return ``True`` (emitting a :attr:`notice`) if a child is already
        alive, so the caller can no-op instead of racing a second spawn onto
        the ``.spar`` lock (fix 2)."""
        if not self._is_alive():
            return False
        self.notice.emit(f"▶ {action} zignorowany — proces już działa")
        return True

    def _read_status(self) -> dict:
        # The status files may be mid-write; never let that crash the GUI.
        try:
            return build_status(self.spar_dir)
        except Exception:
            return {"phase": None, "pending_gate": None, "tasks": {}, "artifact": None, "branches": None}

    def _archive_stale_exec(self) -> None:
        if self._read_status().get("phase") != "done":
            return
        exec_json = self.spar_dir / "exec.json"
        if exec_json.exists():
            exec_json.rename(self.spar_dir / f"exec.json.prev-{int(time.time())}")

    def _spawn(self, args: list[str]) -> None:
        self._last_exit = None
        program, *fixed = self._base_cmd
        full_args = [*fixed, *args]

        proc = QProcess(self)
        proc.setWorkingDirectory(str(self.project_dir))  # review #1
        proc.setProgram(program)
        proc.setArguments(full_args)

        cmd = " ".join([program, *full_args])
        proc.started.connect(lambda: self.started.emit(cmd))
        proc.finished.connect(self._on_finished)

        self._process = proc
        proc.start()
        self._refresh_state()

    def _on_finished(self, exit_code: int, exit_status) -> None:
        crashed = exit_status == QProcess.ExitStatus.CrashExit
        effective = _CRASH_EXIT if crashed else exit_code
        self._last_exit = effective

        # Remarks / task temp files outlived the spawn on purpose; reclaim now.
        for attr in ("_remarks_path", "_task_file_path"):
            path = getattr(self, attr)
            if path is not None:
                try:
                    path.unlink()
                except OSError:
                    pass
                setattr(self, attr, None)

        self.finished.emit(effective)

        # Auto-exec chain: a consensus accept that exited cleanly launches exec.
        if self._auto_exec and effective == 0:
            self._auto_exec = False
            self.notice.emit("▶ konsensus przyjęty — startuję exec…")
            self.start_exec()
            return
        self._auto_exec = False
        self._refresh_state()

    def _refresh_state(self) -> None:
        state = self.current_state()
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
