#!/usr/bin/env python3
"""Fake ``codex`` binary for adapter tests.

Never invoke the real ``codex`` CLI in tests — this script stands in for
it, driven entirely by environment variables:

- ``FAKE_CODEX_ARGS_FILE``: if set, this script appends its argv (as a
  JSON line) to the given file, so tests can assert the exact argv contract.
- ``FAKE_CODEX_STDOUT``: literal stdout to print. Defaults to three JSONL
  lines: a ``session.created`` event with a ``session_id``, a line of
  noise that is not valid JSON, and an ``agent_message`` event.
- ``FAKE_CODEX_LAST_MSG``: if set, this script writes this string to the
  path given after ``--output-last-message`` in its argv. Defaults to
  ``"fake final reply"``.
- ``FAKE_CODEX_NO_LAST_MSG``: if set (to any truthy value), the last-message
  file is not written at all, simulating codex failing to produce one.
- ``FAKE_CODEX_EXIT``: process exit code. Defaults to 0.
- ``FAKE_CODEX_STDERR``: literal stderr to print.
- ``FAKE_CODEX_SLEEP``: seconds to sleep before producing output. Used to
  simulate a hang for timeout tests.
"""

import json
import os
import sys
import time

DEFAULT_STDOUT = (
    "\n".join(
        [
            json.dumps({"type": "session.created", "session_id": "fake-codex-session-1"}),
            "not json at all",
            json.dumps({"type": "agent_message", "message": "fake final reply"}),
        ]
    )
    + "\n"
)


def main() -> int:
    argv = sys.argv

    args_file = os.environ.get("FAKE_CODEX_ARGS_FILE")
    if args_file:
        with open(args_file, "a") as f:
            f.write(json.dumps(argv) + "\n")

    sleep_sec = float(os.environ.get("FAKE_CODEX_SLEEP", "0"))
    if sleep_sec:
        time.sleep(sleep_sec)

    if not os.environ.get("FAKE_CODEX_NO_LAST_MSG"):
        if "--output-last-message" in argv:
            idx = argv.index("--output-last-message")
            last_msg_path = argv[idx + 1]
            last_msg = os.environ.get("FAKE_CODEX_LAST_MSG", "fake final reply")
            with open(last_msg_path, "w") as f:
                f.write(last_msg)

    stdout = os.environ.get("FAKE_CODEX_STDOUT", DEFAULT_STDOUT)
    sys.stdout.write(stdout)

    stderr = os.environ.get("FAKE_CODEX_STDERR", "")
    if stderr:
        sys.stderr.write(stderr)

    exit_code = int(os.environ.get("FAKE_CODEX_EXIT", "0"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
