"""``StreamSink``: fan-out for live display lines during a debate/execution run.

Two distinct audiences write through a sink:

- ``event()`` — the raw lines an adapter streams live from the underlying
  model CLI (see ``Adapter.run_turn``'s ``on_event`` callback), tagged with a
  ``[<prefix>]`` (e.g. ``"claude r0"`` for a debate round, ``"A t1 impl"`` for
  an execution task turn) so a human tailing the output can tell which
  side/task/role a line belongs to. Suppressed on stdout by ``--quiet``
  (the model chatter can be very verbose); ALWAYS appended to ``live.log``.
- ``log()`` — spar's own status/protocol lines (what today's ``log=print``
  parameter prints). These are ALWAYS printed to stdout, even under
  ``--quiet``: the operator (or a headless-mode driving agent) still needs
  spar's own progress/gate/error lines. Also always appended to ``live.log``.

``live.log`` is truncated fresh on every :class:`StreamSink` construction.
Since the sink is built once per CLI invocation, a ``--continue`` resume also
truncates it — each invocation starts a fresh live view. This is accepted v1
behavior (see task-2 brief): the log is a live tail, not a durable transcript
(the per-turn transcript files under ``.spar/transcript`` remain untouched).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TextIO

__all__ = ["StreamSink"]


class StreamSink:
    """Fan-out for display lines: stdout (unless quiet) + live.log (always)."""

    def __init__(
        self, spar_dir: Path, quiet: bool = False, stdout: TextIO = sys.stdout
    ) -> None:
        self.quiet = quiet
        self._stdout = stdout
        spar_dir = Path(spar_dir)
        spar_dir.mkdir(parents=True, exist_ok=True)
        # Truncate live.log fresh on every construction -- see module
        # docstring for why a --continue resume truncating it is accepted.
        self._fh = open(spar_dir / "live.log", "w", encoding="utf-8")

    def event(self, prefix: str, line: str) -> None:
        """A ``[<prefix>] <line>`` display line from an adapter turn."""
        text = f"[{prefix}] {line}"
        if not self.quiet:
            print(text, file=self._stdout)
        self._fh.write(text + "\n")
        self._fh.flush()

    def log(self, message: str) -> None:
        """One of spar's own log lines: always stdout, always live.log."""
        print(message, file=self._stdout)
        self._fh.write(message + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
