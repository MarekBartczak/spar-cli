"""Base adapter for AI CLI implementations.

Adapters are the only place that knows a given CLI's flag syntax. The
orchestrator (a later task) interacts exclusively with the ``Adapter``
protocol and ``TurnResult`` — it never builds argv itself.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
        self, prompt: str, session_id: str | None, timeout_sec: int
    ) -> TurnResult: ...


def run_cli(
    cmd: list[str],
    timeout_sec: int,
    events_path: Path,
    stdin_text: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` via subprocess, capturing stdout+stderr as text.

    The raw stdout is always written to ``events_path`` (even on failure).
    A timeout is converted into ``AdapterError``. Non-zero exit codes are
    never raised here — the caller decides how to interpret them.
    """
    events_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        result = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        events_path.write_text(partial)
        raise AdapterError(f"timeout after {timeout_sec}s") from exc

    events_path.write_text(result.stdout)
    return result
