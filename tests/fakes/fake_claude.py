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
"""

import json
import os
import sys
import time

DEFAULT_STDOUT = json.dumps({"session_id": "fake-session-123", "result": "fake reply"})


def main() -> int:
    args_file = os.environ.get("FAKE_CLAUDE_ARGS_FILE")
    if args_file:
        with open(args_file, "a") as f:
            f.write(json.dumps(sys.argv) + "\n")

    sleep_sec = float(os.environ.get("FAKE_CLAUDE_SLEEP", "0"))
    if sleep_sec:
        time.sleep(sleep_sec)

    stdout = os.environ.get("FAKE_CLAUDE_STDOUT", DEFAULT_STDOUT)
    sys.stdout.write(stdout)

    stderr = os.environ.get("FAKE_CLAUDE_STDERR", "")
    if stderr:
        sys.stderr.write(stderr)

    exit_code = int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
