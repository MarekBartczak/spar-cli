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

    sys.stdout.write(stdout)

    stderr = os.environ.get("FAKE_CLAUDE_STDERR", "")
    if stderr:
        sys.stderr.write(stderr)

    exit_code = int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
