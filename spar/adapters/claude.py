"""Claude Code adapter for debates.

Knows the exact argv contract for invoking the ``claude`` CLI in
non-interactive mode and turns its JSON output into a ``TurnResult``.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from spar.adapters.base import AdapterError, SessionLost, TurnResult, run_cli


class ClaudeAdapter:
    """Adapter for the Claude Code CLI."""

    def __init__(
        self,
        command: str = "claude",
        model: str = "",
        cwd: Path | None = None,
        events_dir: Path | None = None,
        side_name: str = "claude",
    ) -> None:
        self.command = command
        self.model = model
        self.cwd = cwd
        self.events_dir = events_dir if events_dir is not None else Path(".spar/transcript")
        self.side_name = side_name
        self.name = side_name

    def _build_argv(self, prompt: str, session_id: str | None) -> list[str]:
        model_flags = ["--model", self.model] if self.model else []
        if session_id is not None:
            return [
                self.command,
                "-p",
                "--resume",
                session_id,
                "--output-format",
                "json",
                *model_flags,
                prompt,
            ]
        return [
            self.command,
            "-p",
            "--output-format",
            "json",
            *model_flags,
            prompt,
        ]

    def _events_path(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        pid = os.getpid()
        return self.events_dir / f"{self.side_name}-{timestamp}-{pid}.json"

    def run_turn(self, prompt: str, session_id: str | None, timeout_sec: int) -> TurnResult:
        argv = self._build_argv(prompt, session_id)
        events_path = self._events_path()

        result = run_cli(argv, timeout_sec, events_path, cwd=self.cwd)

        if result.returncode != 0:
            if session_id is not None:
                raise SessionLost(
                    f"resume failed for session {session_id!r} (exit {result.returncode})"
                )
            stderr_excerpt = (result.stderr or "")[:500]
            raise AdapterError(
                f"claude exited with code {result.returncode}: {stderr_excerpt}"
            )

        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise AdapterError(f"could not parse claude output as JSON: {exc}") from exc

        if "result" not in payload:
            raise AdapterError("claude output JSON missing 'result' field")

        reply_text = payload["result"]
        new_session_id = payload.get("session_id")

        return TurnResult(
            session_id=new_session_id,
            reply_text=reply_text,
            events_path=events_path,
            exit_code=result.returncode,
        )
