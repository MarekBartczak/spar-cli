"""Codex adapter for debates.

Knows the exact argv contract for invoking the ``codex`` CLI in
non-interactive (``exec``) mode. Codex streams JSONL events on stdout (not
a single JSON document) and writes its final agent reply to a separate
"last message" file, so this adapter parses both.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from spar.adapters.base import AdapterError, SessionLost, TurnResult, run_cli


class CodexAdapter:
    """Adapter for the Codex CLI."""

    def __init__(
        self,
        command: str = "codex",
        model: str = "",
        cwd: Path | None = None,
        events_dir: Path | None = None,
        side_name: str = "codex",
    ) -> None:
        self.command = command
        self.model = model
        self.cwd = cwd
        self.events_dir = events_dir if events_dir is not None else Path(".spar/transcript")
        self.side_name = side_name
        self.name = side_name

    def _events_path(self, timestamp: str) -> Path:
        pid = os.getpid()
        return self.events_dir / f"{self.side_name}-{timestamp}-{pid}.jsonl"

    def _last_msg_path(self, timestamp: str) -> Path:
        pid = os.getpid()
        return self.events_dir / f"{self.side_name}-last-{timestamp}-{pid}.md"

    def _build_argv(
        self, prompt: str, session_id: str | None, last_msg_path: Path
    ) -> list[str]:
        globals_ = [
            "--json",
            "--sandbox",
            "workspace-write",
            *(["--cd", str(self.cwd)] if self.cwd else []),
            *(["-m", self.model] if self.model else []),
            "--output-last-message",
            str(last_msg_path),
        ]
        if session_id is not None:
            return [self.command, "exec", *globals_, "resume", session_id, prompt]
        return [self.command, "exec", *globals_, prompt]

    @staticmethod
    def _extract_session_id(stdout: str) -> str | None:
        """Scan the JSONL event stream for the first session id.

        Tolerates malformed/non-JSON lines (skips them). Current codex emits
        the id as ``thread_id`` on a ``thread.started`` event; older builds
        used ``session_id`` (top-level or nested under ``msg``). All forms are
        accepted so the adapter survives CLI version drift in either direction.
        """
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if "thread_id" in obj:
                return obj["thread_id"]
            if "session_id" in obj:
                return obj["session_id"]
            msg = obj.get("msg")
            if isinstance(msg, dict):
                if "thread_id" in msg:
                    return msg["thread_id"]
                if "session_id" in msg:
                    return msg["session_id"]
        return None

    def run_turn(self, prompt: str, session_id: str | None, timeout_sec: int) -> TurnResult:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        events_path = self._events_path(timestamp)
        last_msg_path = self._last_msg_path(timestamp)

        # Codex writes the last-message file itself (possibly under a
        # different subprocess cwd), so it must exist as an absolute path.
        last_msg_path.parent.mkdir(parents=True, exist_ok=True)
        last_msg_path = last_msg_path.resolve()

        argv = self._build_argv(prompt, session_id, last_msg_path)

        result = run_cli(argv, timeout_sec, events_path, cwd=self.cwd)

        if result.returncode != 0:
            if session_id is not None:
                raise SessionLost(
                    f"resume failed for session {session_id!r} (exit {result.returncode})"
                )
            stderr_excerpt = (result.stderr or "")[:500]
            raise AdapterError(
                f"codex exited with code {result.returncode}: {stderr_excerpt}"
            )

        new_session_id = self._extract_session_id(result.stdout)

        if not last_msg_path.exists():
            raise AdapterError("codex produced no final message")
        reply_text = last_msg_path.read_text()
        if not reply_text:
            raise AdapterError("codex produced no final message")

        return TurnResult(
            session_id=new_session_id,
            reply_text=reply_text,
            events_path=events_path,
            exit_code=result.returncode,
        )
