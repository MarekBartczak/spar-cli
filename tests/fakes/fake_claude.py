#!/usr/bin/env python3
"""Fake ``claude`` binary for adapter tests.

Never invoke the real ``claude`` CLI in tests — this script stands in for
it, driven entirely by environment variables:

- ``FAKE_CLAUDE_ARGS_FILE``: if set, this script appends its argv (as a
  JSON line) to the given file, so tests can assert the exact argv contract.
- ``FAKE_CLAUDE_STDOUT``: literal stdout to print. Defaults to a valid JSON
  document with a ``session_id`` and a ``result``.
- ``FAKE_CLAUDE_EXIT``: process exit code. Defaults to 0.
- ``FAKE_CLAUDE_SLEEP``: seconds to sleep before producing output. Used to
  simulate a hang for timeout tests.

Stream-json mode
----------------

When the argv carries ``--output-format stream-json`` (the real adapter's
streaming switch), this script emits the real stream-json JSONL shapes
instead of a single JSON document. It takes the "reply document" (from
``FAKE_CLAUDE_STDOUT`` or the scripted ``<n>.json`` file), and if it parses
as a JSON object, re-emits it as:

  - (optional) a ``content_block_start`` event for a ``tool_use`` block when
    ``FAKE_CLAUDE_STREAM_TOOL`` names a tool;
  - a ``content_block_delta`` ``text_delta`` carrying the ``result`` text;
  - a terminal ``{"type":"result", ...}`` event carrying ``result`` and
    ``session_id`` (and ``duration_ms`` from ``FAKE_CLAUDE_DURATION_MS`` or
    the reply document, if present).

This lets every existing scripted ``<n>.json`` reply keep working unchanged:
the adapter reads ``result``/``session_id`` from the terminal event exactly
as it used to read them from the single JSON document. A reply document that
is NOT valid JSON (malformed-output tests) is emitted verbatim so the adapter
still sees no terminal ``result`` event. When the argv does not request
stream-json, the reply document is emitted verbatim (legacy ``json`` mode).

- ``FAKE_CLAUDE_STREAM_TOOL``: if set, emit a ``tool_use`` content_block_start
  with this name before the text delta (stream-json mode only).
- ``FAKE_CLAUDE_DURATION_MS``: if set, put this ``duration_ms`` on the
  terminal result event (stream-json mode only).

Scripted multi-turn mode (for end-to-end debate tests)
--------------------------------------------------------

- ``FAKE_CLAUDE_SCRIPT_DIR``: a directory holding a numbered script for a
  multi-turn debate. On every invocation this script atomically increments
  a counter file (``<dir>/.calls``) to get its call number ``N``, then:

  - appends a delimited record of this call's argv to ``<dir>/prompts.log``
    (so tests can assert exactly what the orchestrator sent);
  - if ``<dir>/N.json`` exists, its content becomes stdout verbatim (this is
    the *full* stdout JSON document — scripts control ``session_id`` and
    ``result``, including the ``<verdict>`` block inside ``result``);
  - if ``<dir>/N.artifact`` exists, its content is written to the path given
    by ``FAKE_CLAUDE_ARTIFACT_PATH`` (simulates the agent editing the shared
    artifact this turn);
  - if ``<dir>/N.foreign`` exists, its content is written to a new file next
    to the artifact (simulates the agent touching a file outside the
    contract, for guard tests).
  - if ``<dir>/N.files.json`` exists, it is parsed as a JSON object mapping
    relative path -> file content, and every entry is written under the
    process's current working directory (creating parent directories as
    needed). This is how the execution-engine e2e tests simulate an
    implementer Task writing its scoped files onto the task branch: the
    impl adapter runs with ``cwd`` set to the task's worktree, so a relative
    path here lands exactly where the real scope guard expects it.
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_STDOUT = json.dumps({"session_id": "fake-session-123", "result": "fake reply"})


def _next_counter(counter_path: Path) -> int:
    """Atomically increment and return the integer in ``counter_path``."""
    fd = os.open(counter_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 64).decode("utf-8").strip()
        n = (int(raw) if raw else 0) + 1
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(n).encode("utf-8"))
        return n
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _run_scripted(script_dir: Path, argv: list[str]) -> str | None:
    """Handle ``FAKE_CLAUDE_SCRIPT_DIR`` mode. Returns stdout override, if any."""
    n = _next_counter(script_dir / ".calls")

    with open(script_dir / "prompts.log", "a") as f:
        f.write(f"=== call {n} argv ===\n")
        f.write(json.dumps(argv) + "\n")

    artifact_file = script_dir / f"{n}.artifact"
    if artifact_file.exists():
        artifact_path = os.environ.get("FAKE_CLAUDE_ARTIFACT_PATH")
        if artifact_path:
            Path(artifact_path).write_text(artifact_file.read_text(encoding="utf-8"), encoding="utf-8")

    foreign_file = script_dir / f"{n}.foreign"
    if foreign_file.exists():
        artifact_path = os.environ.get("FAKE_CLAUDE_ARTIFACT_PATH")
        if artifact_path:
            foreign_path = Path(artifact_path).parent / f"foreign-claude-{n}.txt"
            foreign_path.write_text(foreign_file.read_text(encoding="utf-8"), encoding="utf-8")

    files_file = script_dir / f"{n}.files.json"
    if files_file.exists():
        mapping = json.loads(files_file.read_text(encoding="utf-8"))
        for rel_path, content in mapping.items():
            target = Path.cwd() / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    reply_file = script_dir / f"{n}.json"
    if reply_file.exists():
        return reply_file.read_text(encoding="utf-8")
    return None


def _is_stream_json(argv: list[str]) -> bool:
    """True when the argv requests ``--output-format stream-json``."""
    try:
        idx = argv.index("--output-format")
    except ValueError:
        return False
    return idx + 1 < len(argv) and argv[idx + 1] == "stream-json"


def _emit_stream(doc: dict) -> str:
    """Re-emit a reply document as real stream-json JSONL."""
    lines: list[str] = []

    tool = os.environ.get("FAKE_CLAUDE_STREAM_TOOL")
    if tool:
        lines.append(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_start",
                        "content_block": {"type": "tool_use", "name": tool},
                    },
                }
            )
        )

    result_text = doc.get("result")
    if isinstance(result_text, str) and result_text:
        lines.append(
            json.dumps(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": result_text},
                    },
                }
            )
        )

    result_event: dict = {"type": "result"}
    if "result" in doc:
        result_event["result"] = doc["result"]
    if "session_id" in doc:
        result_event["session_id"] = doc["session_id"]
    duration_ms = os.environ.get("FAKE_CLAUDE_DURATION_MS")
    if duration_ms:
        result_event["duration_ms"] = int(duration_ms)
    elif "duration_ms" in doc:
        result_event["duration_ms"] = doc["duration_ms"]
    lines.append(json.dumps(result_event))

    return "\n".join(lines) + "\n"


def main() -> int:
    args_file = os.environ.get("FAKE_CLAUDE_ARGS_FILE")
    if args_file:
        with open(args_file, "a") as f:
            f.write(json.dumps(sys.argv) + "\n")

    sleep_sec = float(os.environ.get("FAKE_CLAUDE_SLEEP", "0"))
    if sleep_sec:
        time.sleep(sleep_sec)

    stdout = os.environ.get("FAKE_CLAUDE_STDOUT", DEFAULT_STDOUT)

    script_dir = os.environ.get("FAKE_CLAUDE_SCRIPT_DIR")
    if script_dir:
        scripted_stdout = _run_scripted(Path(script_dir), sys.argv)
        if scripted_stdout is not None:
            stdout = scripted_stdout

    if _is_stream_json(sys.argv):
        try:
            doc = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            doc = None
        if isinstance(doc, dict):
            stdout = _emit_stream(doc)

    sys.stdout.write(stdout)

    stderr = os.environ.get("FAKE_CLAUDE_STDERR", "")
    if stderr:
        sys.stderr.write(stderr)

    exit_code = int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
