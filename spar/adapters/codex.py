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
from typing import Callable

from spar.adapters.base import AdapterError, SessionLost, TurnResult, run_cli


def _display_line(obj: object) -> str | None:
    """Map one parsed codex JSONL event to a human-readable display line.

    Returns ``None`` for events with no display output. Codex wraps items in
    ``item.completed`` events: an ``agent_message`` item carries the reply
    text; command/tool items carry the executed command (shown ``exec: ...``).
    The terminal ``turn.completed`` maps to ``done``.
    """
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    if kind == "item.completed":
        item = obj.get("item")
        if not isinstance(item, dict):
            return None
        itype = item.get("type")
        if itype == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
            return None
        if itype in ("command_execution", "command", "exec_command", "local_shell_call"):
            command = item.get("command") or item.get("cmd")
            if isinstance(command, str) and command:
                return f"exec: {command}"
            return None
        return None
    if kind == "turn.completed":
        return "done"
    return None


def _make_on_line(on_event: Callable[[str], None]) -> Callable[[str], None]:
    """Wrap ``on_event`` as a raw-line handler for ``run_cli``.

    Each JSONL line is parsed and mapped to a display line; unparseable or
    display-less lines are dropped. ``run_cli`` already guards against callback
    exceptions, so a raising ``on_event`` never kills the turn.
    """

    def on_line(line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return
        display = _display_line(obj)
        if display is not None:
            on_event(display)

    return on_line


class CodexAdapter:
    """Adapter for the Codex CLI."""

    def __init__(
        self,
        command: str = "codex",
        model: str = "",
        cwd: Path | None = None,
        events_dir: Path | None = None,
        side_name: str = "codex",
        readonly: bool = False,
    ) -> None:
        self.command = command
        self.model = model
        self.cwd = cwd
        self.events_dir = events_dir if events_dir is not None else Path(".spar/transcript")
        self.side_name = side_name
        self.name = side_name
        self.readonly = readonly

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
            # A readonly adapter (reviewer role) must not write to the repo.
            "read-only" if self.readonly else "workspace-write",
            # --cd must be ABSOLUTE: run_cli already launches codex with its
            # subprocess cwd set to this same directory, so codex would resolve
            # a relative --cd against it a second time (.spar/worktrees/x/.spar/
            # worktrees/x) and die with "No such file or directory".
            *(["--cd", str(Path(self.cwd).resolve())] if self.cwd else []),
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

    def run_turn(
        self,
        prompt: str,
        session_id: str | None,
        timeout_sec: int,
        on_event: Callable[[str], None] | None = None,
    ) -> TurnResult:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        events_path = self._events_path(timestamp)
        last_msg_path = self._last_msg_path(timestamp)

        # Codex writes the last-message file itself (possibly under a
        # different subprocess cwd), so it must exist as an absolute path.
        last_msg_path.parent.mkdir(parents=True, exist_ok=True)
        last_msg_path = last_msg_path.resolve()

        argv = self._build_argv(prompt, session_id, last_msg_path)

        on_line = _make_on_line(on_event) if on_event is not None else None

        result = run_cli(argv, timeout_sec, events_path, cwd=self.cwd, on_line=on_line)

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
