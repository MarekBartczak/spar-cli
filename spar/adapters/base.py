"""Base adapter for AI CLI implementations.

Adapters are the only place that knows a given CLI's flag syntax. The
orchestrator (a later task) interacts exclusively with the ``Adapter``
protocol and ``TurnResult`` — it never builds argv itself.
"""

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol


@dataclass(frozen=True)
class TurnResult:
    """Result of a single adapter turn."""

    session_id: str | None  # None = could not determine
    reply_text: str  # agent's final message
    events_path: Path  # raw stream/JSON dump saved for the transcript
    exit_code: int


class SessionLost(Exception):
    """Raised when resuming a session fails; orchestrator will start fresh."""

    pass


class AdapterError(Exception):
    """Raised when the CLI fails (non-zero exit, timeout, unparseable output)."""

    pass


class Adapter(Protocol):
    """Shared contract implemented by each AI CLI adapter."""

    name: str

    def run_turn(
        self,
        prompt: str,
        session_id: str | None,
        timeout_sec: int,
        on_event: Callable[[str], None] | None = None,
    ) -> TurnResult: ...


def run_cli(
    cmd: list[str],
    timeout_sec: int,
    events_path: Path,
    stdin_text: str | None = None,
    cwd: Path | None = None,
    on_line: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` via ``subprocess.Popen``, streaming stdout live.

    Stdout is consumed line by line on a dedicated reader thread: each line is
    appended to ``events_path`` (opened once, flushed per line so a partial
    stream survives a crash/timeout) and, when ``on_line`` is given, passed to
    it newline-free. Stderr is drained concurrently on its own thread — an
    undrained stderr pipe fills (~64 KB) and blocks the child, which would
    otherwise look like a timeout. Both are returned in the
    ``CompletedProcess`` exactly as today.

    ``stdin_text`` (when not ``None``) is written to the child's stdin and the
    pipe closed immediately, matching ``subprocess.run(input=...)`` semantics;
    otherwise stdin is ``DEVNULL`` so a command waiting on stdin cannot hang.

    A timeout kills the child, joins the readers, and raises ``AdapterError``
    (the partial stream is already on disk). ``on_line`` exceptions are caught
    and dropped — a bad callback never kills the turn. Non-zero exit codes are
    never raised here; the caller decides how to interpret them.

    ``on_line=None`` keeps caller-visible behavior identical to a buffered run.
    """
    events_path.parent.mkdir(parents=True, exist_ok=True)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    events_file = events_path.open("w", encoding="utf-8")

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )

    def _read_stdout() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                stdout_chunks.append(line)
                try:
                    events_file.write(line)
                    events_file.flush()
                except Exception:
                    pass
                if on_line is not None:
                    try:
                        on_line(line.rstrip("\n"))
                    except Exception:
                        # A misbehaving callback must never kill the turn.
                        pass
        finally:
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except Exception:
                pass

    def _read_stderr() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                stderr_chunks.append(line)
        finally:
            try:
                proc.stderr.close()  # type: ignore[union-attr]
            except Exception:
                pass

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    if stdin_text is not None:
        try:
            proc.stdin.write(stdin_text)  # type: ignore[union-attr]
        except Exception:
            pass
        finally:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except Exception:
                pass

    try:
        proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        t_out.join()
        t_err.join()
        events_file.close()
        raise AdapterError(f"timeout after {timeout_sec}s") from exc

    t_out.join()
    t_err.join()
    events_file.close()

    return subprocess.CompletedProcess(
        args=cmd,
        returncode=proc.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
    )
