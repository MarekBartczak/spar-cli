#!/usr/bin/env python3
"""Fake ``codex`` binary for adapter tests.

Never invoke the real ``codex`` CLI in tests — this script stands in for
it, driven entirely by environment variables:

- ``FAKE_CODEX_ARGS_FILE``: if set, this script appends its argv (as a
  JSON line) to the given file, so tests can assert the exact argv contract.
- ``FAKE_CODEX_STDOUT``: literal stdout to print. Defaults to the current
  codex event schema: a ``thread.started`` event carrying ``thread_id``, a
  ``turn.started`` event, a line of noise that is not valid JSON, an
  ``item.completed`` event wrapping an ``agent_message`` item, and a
  ``turn.completed`` event.
- ``FAKE_CODEX_LAST_MSG``: if set, this script writes this string to the
  path given after ``--output-last-message`` in its argv. Defaults to
  ``"fake final reply"``.
- ``FAKE_CODEX_NO_LAST_MSG``: if set (to any truthy value), the last-message
  file is not written at all, simulating codex failing to produce one.
- ``FAKE_CODEX_EXIT``: process exit code. Defaults to 0.
- ``FAKE_CODEX_STDERR``: literal stderr to print.
- ``FAKE_CODEX_SLEEP``: seconds to sleep before producing output. Used to
  simulate a hang for timeout tests.

Scripted multi-turn mode (for end-to-end debate tests)
--------------------------------------------------------

- ``FAKE_CODEX_SCRIPT_DIR``: a directory holding a numbered script for a
  multi-turn debate. On every invocation this script atomically increments
  a counter file (``<dir>/.calls``) to get its call number ``N``, then:

  - appends a delimited record of this call's argv to ``<dir>/prompts.log``
    (so tests can assert exactly what the orchestrator sent);
  - if ``<dir>/N.jsonl`` exists, its content becomes the stdout JSONL event
    stream verbatim;
  - if ``<dir>/N.md`` exists, its content is written to the
    ``--output-last-message`` path (the reply text the adapter reads);
  - if ``<dir>/N.artifact`` exists, its content is written to the path given
    by ``FAKE_CODEX_ARTIFACT_PATH`` (simulates the agent editing the shared
    artifact this turn);
  - if ``<dir>/N.foreign`` exists, its content is written to a new file next
    to the artifact (simulates the agent touching a file outside the
    contract, for guard tests).
"""

import fcntl
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_STDOUT = (
    "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "fake-codex-session-1"}),
            json.dumps({"type": "turn.started"}),
            "not json at all",
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "item_0", "type": "agent_message", "text": "fake final reply"},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
        ]
    )
    + "\n"
)


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


def _run_scripted(script_dir: Path, argv: list[str]) -> tuple[str | None, str | None]:
    """Handle ``FAKE_CODEX_SCRIPT_DIR`` mode.

    Returns ``(stdout_override, last_msg_override)``, either of which may be
    ``None`` if the numbered file for this call is absent.
    """
    n = _next_counter(script_dir / ".calls")

    with open(script_dir / "prompts.log", "a") as f:
        f.write(f"=== call {n} argv ===\n")
        f.write(json.dumps(argv) + "\n")

    artifact_file = script_dir / f"{n}.artifact"
    if artifact_file.exists():
        artifact_path = os.environ.get("FAKE_CODEX_ARTIFACT_PATH")
        if artifact_path:
            Path(artifact_path).write_text(artifact_file.read_text(encoding="utf-8"), encoding="utf-8")

    foreign_file = script_dir / f"{n}.foreign"
    if foreign_file.exists():
        artifact_path = os.environ.get("FAKE_CODEX_ARTIFACT_PATH")
        if artifact_path:
            foreign_path = Path(artifact_path).parent / f"foreign-codex-{n}.txt"
            foreign_path.write_text(foreign_file.read_text(encoding="utf-8"), encoding="utf-8")

    stdout_override = None
    jsonl_file = script_dir / f"{n}.jsonl"
    if jsonl_file.exists():
        stdout_override = jsonl_file.read_text(encoding="utf-8")

    last_msg_override = None
    md_file = script_dir / f"{n}.md"
    if md_file.exists():
        last_msg_override = md_file.read_text(encoding="utf-8")

    return stdout_override, last_msg_override


def main() -> int:
    argv = sys.argv

    args_file = os.environ.get("FAKE_CODEX_ARGS_FILE")
    if args_file:
        with open(args_file, "a") as f:
            f.write(json.dumps(argv) + "\n")

    sleep_sec = float(os.environ.get("FAKE_CODEX_SLEEP", "0"))
    if sleep_sec:
        time.sleep(sleep_sec)

    stdout = os.environ.get("FAKE_CODEX_STDOUT", DEFAULT_STDOUT)
    last_msg = os.environ.get("FAKE_CODEX_LAST_MSG", "fake final reply")

    script_dir = os.environ.get("FAKE_CODEX_SCRIPT_DIR")
    if script_dir:
        stdout_override, last_msg_override = _run_scripted(Path(script_dir), argv)
        if stdout_override is not None:
            stdout = stdout_override
        if last_msg_override is not None:
            last_msg = last_msg_override

    if not os.environ.get("FAKE_CODEX_NO_LAST_MSG"):
        if "--output-last-message" in argv:
            idx = argv.index("--output-last-message")
            last_msg_path = argv[idx + 1]
            with open(last_msg_path, "w") as f:
                f.write(last_msg)

    sys.stdout.write(stdout)

    stderr = os.environ.get("FAKE_CODEX_STDERR", "")
    if stderr:
        sys.stderr.write(stderr)

    exit_code = int(os.environ.get("FAKE_CODEX_EXIT", "0"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
