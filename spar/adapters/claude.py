"""Claude Code adapter for debates.

Knows the exact argv contract for invoking the ``claude`` CLI in
non-interactive mode and turns its JSON output into a ``TurnResult``.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from spar.adapters.base import AdapterError, SessionLost, TurnResult, run_cli


_ARG_KEYS = ("file_path", "path", "command", "pattern", "url")
_MAX_ARG_LEN = 100


def _result_display_line(obj: dict) -> str:
    """Format the terminal ``result`` event's display line."""
    duration_ms = obj.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        return f"done ({duration_ms / 1000:.1f}s)"
    return "done"


def _truncate(text: str, limit: int = _MAX_ARG_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _tool_line(name: object, raw_json: str) -> str:
    """Render ``tool: <name> <arg>`` from the buffered raw JSON fragments.

    Unparseable JSON (or an empty buffer) falls back to bare ``tool: <name>``.
    Otherwise prefers the first present key of ``_ARG_KEYS``; failing that,
    a compacted dump of the whole parsed value. The argument is truncated to
    ``_MAX_ARG_LEN`` characters.
    """
    try:
        parsed = json.loads(raw_json)
    except (json.JSONDecodeError, ValueError):
        return f"tool: {name}"

    arg: str | None = None
    if isinstance(parsed, dict):
        for key in _ARG_KEYS:
            if key in parsed:
                arg = str(parsed[key])
                break
        if arg is None:
            arg = json.dumps(parsed, separators=(",", ":"))
    else:
        arg = json.dumps(parsed, separators=(",", ":"))

    return f"tool: {name} {_truncate(arg)}"


class _DisplayMapper:
    """Stateful per-turn mapper from parsed stream-json events to display lines.

    A ``content_block_start`` for a ``tool_use`` block does not emit
    immediately: the tool's INPUT arrives afterward as ``input_json_delta``
    fragments (``delta.partial_json`` string chunks), keyed by the shared
    ``event.index``, and terminated by ``content_block_stop``. This mapper
    buffers those fragments per index and emits a single ``tool: <name> <arg>``
    line once the block closes (see ``_tool_line``).

    Safety net: if a ``result`` event or a NEW ``content_block_start`` for the
    SAME index arrives while a tool is still buffered (a missing stop), the
    buffered tool is flushed first — a tool must never be silently swallowed.

    ``map`` returns a list of zero or more display lines for one parsed
    event, since the safety net can produce a flush line in addition to the
    event's own line (e.g. a flushed tool plus the terminal ``done`` line).
    """

    def __init__(self) -> None:
        self._pending: dict[object, dict] = {}

    def _flush(self, index: object) -> str:
        entry = self._pending.pop(index)
        return _tool_line(entry["name"], "".join(entry["buffer"]))

    def _flush_all(self) -> list[str]:
        lines = [self._flush(index) for index in list(self._pending.keys())]
        return lines

    def map(self, obj: object) -> list[str]:
        if not isinstance(obj, dict):
            return []
        kind = obj.get("type")
        if kind == "stream_event":
            event = obj.get("event")
            if not isinstance(event, dict):
                return []
            etype = event.get("type")
            index = event.get("index")
            if etype == "content_block_delta":
                delta = event.get("delta")
                if not isinstance(delta, dict):
                    return []
                dtype = delta.get("type")
                if dtype == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        return [text]
                    return []
                if dtype == "input_json_delta" and index in self._pending:
                    partial = delta.get("partial_json")
                    if isinstance(partial, str):
                        self._pending[index]["buffer"].append(partial)
                    return []
                return []
            if etype == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    lines: list[str] = []
                    if index in self._pending:
                        lines.append(self._flush(index))
                    self._pending[index] = {"name": block.get("name"), "buffer": []}
                    return lines
                return []
            if etype == "content_block_stop":
                if index in self._pending:
                    return [self._flush(index)]
                return []
            return []
        if kind == "result":
            lines = self._flush_all()
            lines.append(_result_display_line(obj))
            return lines
        return []


def _extract_result(stdout: str) -> tuple[bool, str | None, str | None]:
    """Scan the stream-json JSONL for the terminal ``result`` event.

    Returns ``(found, reply_text, session_id)``. The last ``result`` event
    wins. Malformed/non-JSON lines are skipped silently (still persisted raw
    in the events file by ``run_cli``).
    """
    found = False
    reply_text: str | None = None
    session_id: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = True
            reply_text = obj.get("result")
            session_id = obj.get("session_id")
    return found, reply_text, session_id


class ClaudeAdapter:
    """Adapter for the Claude Code CLI."""

    def __init__(
        self,
        command: str = "claude",
        model: str = "",
        cwd: Path | None = None,
        events_dir: Path | None = None,
        side_name: str = "claude",
        readonly: bool = False,
    ) -> None:
        self.command = command
        self.model = model
        self.cwd = cwd
        self.events_dir = events_dir if events_dir is not None else Path(".spar/transcript")
        self.side_name = side_name
        self.name = side_name
        self.readonly = readonly

    def _build_argv(self, prompt: str, session_id: str | None) -> list[str]:
        model_flags = ["--model", self.model] if self.model else []
        # Headless (`-p`) claude cannot prompt for permission, so without these
        # it silently refuses to touch the artifact. acceptEdits auto-approves
        # file writes/edits. --allowedTools is variadic: it must be followed by
        # another flag (--permission-mode), never by the positional prompt, or
        # it swallows the prompt ("Input must be provided ... with --print").
        # So: allowlist (one comma token) first, then permission-mode last.
        # A readonly adapter (reviewer role) gets NO write tools and no
        # auto-approving permission mode: reviews must not touch the repo.
        # The non-readonly (implementer) branch also gets Bash/Grep/Glob: it
        # needs a shell to compile/lint its own work before the per-task
        # test runs, matching the codex adapter's --sandbox workspace-write,
        # which already grants a full shell. The scope guard and self-commit
        # handling in the review loop police the results either way.
        if self.readonly:
            perm_flags = ["--allowedTools", "Read"]
        else:
            perm_flags = [
                "--allowedTools",
                "Read,Edit,Write,Bash,Grep,Glob",
                "--permission-mode",
                "acceptEdits",
            ]
        # stream-json + --verbose + --include-partial-messages makes claude
        # emit the incremental event stream (content_block deltas, tool_use
        # starts, a terminal result event) instead of a single buffered JSON
        # document, so turns can be surfaced live via the on_event callback.
        fmt_flags = [
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if session_id is not None:
            return [
                self.command,
                "-p",
                "--resume",
                session_id,
                *fmt_flags,
                *perm_flags,
                *model_flags,
                prompt,
            ]
        return [
            self.command,
            "-p",
            *fmt_flags,
            *perm_flags,
            *model_flags,
            prompt,
        ]

    def _events_path(self) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        pid = os.getpid()
        return self.events_dir / f"{self.side_name}-{timestamp}-{pid}.json"

    def run_turn(
        self,
        prompt: str,
        session_id: str | None,
        timeout_sec: int,
        on_event: Callable[[str], None] | None = None,
    ) -> TurnResult:
        argv = self._build_argv(prompt, session_id)
        events_path = self._events_path()

        on_line = self._make_on_line(on_event) if on_event is not None else None

        result = run_cli(argv, timeout_sec, events_path, cwd=self.cwd, on_line=on_line)

        if result.returncode != 0:
            if session_id is not None:
                raise SessionLost(
                    f"resume failed for session {session_id!r} (exit {result.returncode})"
                )
            stderr_excerpt = (result.stderr or "")[:500]
            raise AdapterError(
                f"claude exited with code {result.returncode}: {stderr_excerpt}"
            )

        found, reply_text, new_session_id = _extract_result(result.stdout)
        if not found:
            raise AdapterError("claude output missing terminal 'result' event")
        if reply_text is None:
            raise AdapterError("claude 'result' event missing 'result' field")

        return TurnResult(
            session_id=new_session_id,
            reply_text=reply_text,
            events_path=events_path,
            exit_code=result.returncode,
        )

    @staticmethod
    def _make_on_line(
        on_event: Callable[[str], None],
    ) -> Callable[[str], None]:
        """Wrap ``on_event`` as a raw-line handler for ``run_cli``.

        Each JSONL line is parsed and mapped, via a fresh per-turn
        ``_DisplayMapper``, to zero or more display lines; unparseable or
        display-less lines produce none. ``run_cli`` already guards against
        callback exceptions, so a raising ``on_event`` never kills the turn.
        """
        mapper = _DisplayMapper()

        def on_line(line: str) -> None:
            line = line.strip()
            if not line:
                return
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                return
            for display in mapper.map(obj):
                on_event(display)

        return on_line
