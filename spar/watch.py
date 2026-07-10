"""``spar watch``: a stdlib ANSI viewer following ``.spar/live.log``.

Core is the testable :func:`follow` generator (tail -f semantics: default
seeks to the end and yields only newly appended lines; ``from_start=True``
yields the whole file first). It tolerates the file not existing yet (waits
for it to appear) and truncation (a new run started -- reopen from 0).

:func:`colorize` is a pure function that ANSI-colors a display line: a
stable-per-prefix color for ``[<prefix>]`` lines, a highlight for spar's own
``spar:``/``spar exec:`` log lines, and a bright banner for any line
containing ``gate '<name>' pending`` (the moment a human's attention is
most needed).

:func:`main_watch` wires the two together into the ``spar watch`` CLI
command: loop ``follow`` -> ``print(colorize(line))``, exiting cleanly
(rc 0) on Ctrl+C.
"""

from __future__ import annotations

import argparse
import re
import time
import zlib
from pathlib import Path
from typing import Callable, Iterator

__all__ = ["follow", "colorize", "main_watch"]

_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
# Foreground colors (avoid black/white so the palette works on light and
# dark terminal backgrounds alike).
_PALETTE = [31, 32, 33, 34, 35, 36, 91, 92, 93, 94, 95, 96]

_PREFIX_RE = re.compile(r"^\[([^\]]+)\](.*)$")
_GATE_PENDING_RE = re.compile(r"gate '[^']*' pending")


def follow(
    path: Path,
    from_start: bool = False,
    poll_sec: float = 0.25,
    stop: Callable[[], bool] = lambda: False,
) -> Iterator[str]:
    """Yield lines appended to ``path``.

    Tolerates the file not existing yet (waits for it) and truncation
    (reopens from 0 -- a new run started). Runs until ``stop()`` returns
    True; checked once per loop iteration, so it may yield already-pending
    lines before honoring a stop request.
    """
    path = Path(path)
    fh = None
    pos = 0
    # Only the very first open respects ``from_start`` (seek to end unless
    # asked to print everything); a reopen after truncation always starts
    # fresh from 0 regardless of the original flag -- a new run started.
    first_open = True
    try:
        while not stop():
            if fh is None:
                try:
                    fh = path.open("r", encoding="utf-8", errors="replace")
                except OSError:
                    time.sleep(poll_sec)
                    continue
                if first_open and not from_start:
                    fh.seek(0, 2)
                    pos = fh.tell()
                else:
                    fh.seek(pos)
                first_open = False

            line = fh.readline()
            if line:
                if line.endswith("\n"):
                    pos = fh.tell()
                    yield line[:-1]
                    continue
                # Partial line (writer hasn't flushed the newline yet):
                # rewind and wait for the rest.
                fh.seek(pos)
                time.sleep(poll_sec)
                continue

            # No new data right now -- check for truncation (a fresh run
            # started and .spar/live.log was recreated shorter).
            try:
                size = path.stat().st_size
            except OSError:
                fh.close()
                fh = None
                time.sleep(poll_sec)
                continue
            if size < pos:
                fh.close()
                fh = None
                pos = 0
                continue
            time.sleep(poll_sec)
    finally:
        if fh is not None:
            fh.close()


def _color_for(prefix: str) -> int:
    """Deterministic (process- and platform-independent) color for a prefix."""
    return _PALETTE[zlib.crc32(prefix.encode("utf-8")) % len(_PALETTE)]


def colorize(line: str) -> str:
    """ANSI-color a single display line for the ``spar watch`` viewer."""
    if _GATE_PENDING_RE.search(line):
        # Bright banner: a gate is pending -- this is the line a human
        # tailing the log most needs to notice.
        return f"{_BOLD}\x1b[93;41m{line}{_RESET}"

    match = _PREFIX_RE.match(line)
    if match:
        prefix, rest = match.groups()
        color = _color_for(prefix)
        return f"\x1b[{color}m[{prefix}]{_RESET}{rest}"

    if line.startswith("spar exec:") or line.startswith("spar:"):
        return f"{_BOLD}\x1b[36m{line}{_RESET}"

    return line


def main_watch(argv=None) -> int:
    """Entry point for the ``spar watch`` subcommand."""
    parser = argparse.ArgumentParser(
        prog="spar watch",
        description="Follow .spar/live.log with colorized live output",
    )
    parser.add_argument(
        "--from-start", dest="from_start", action="store_true",
        help="Print the whole log from the beginning, then keep following "
             "(default: only new lines from now on)",
    )
    args = parser.parse_args(argv)

    path = Path(".spar/live.log")
    try:
        for line in follow(path, from_start=args.from_start):
            print(colorize(line))
    except KeyboardInterrupt:
        pass
    return 0
