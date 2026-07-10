"""``spar ui``: spawn a live viewer (``spar watch``) beside the user's session.

Pure decision function (:func:`pick_spawn_argv`) plus a thin, always-succeeds
spawner (:func:`main_ui`). Detection cascade, first hit wins:

1. Already inside tmux (``TMUX`` in the environment) -> split the current
   window.
2. ``warp-terminal`` on PATH -> **deliberately not implemented**. Warp's
   "launch configuration" mechanism (a YAML file dropped under
   ``~/.local/share/warp-terminal/launch_configurations/``) is undocumented,
   version-dependent, and there is no reliable way from here to confirm it
   will actually open a new window running ``spar watch`` rather than
   silently no-op. Per the brief: "if writing/using the launch config is at
   all uncertain at implementation time, print the instruction instead."
   That uncertainty is real, so this step always falls through to the next
   one -- recognizing warp-terminal is present buys nothing until someone
   has verified the launch-config contract against a live Warp install.
3. First available of ``x-terminal-emulator`` / ``gnome-terminal`` /
   ``konsole`` / ``xterm``. ``gnome-terminal`` needs ``--`` before the
   command; the rest take ``-e``. The command is passed as *separate* argv
   items (``["term", "-e", "spar", "watch"]``), never a single joined
   string -- several emulators treat one ``"spar watch"`` argument as a
   literal executable name to look up rather than a command line.
4. Nothing found -> ``None``; the caller prints a manual instruction.

Spawning a viewer must never fail a pipeline: :func:`main_ui` always
returns 0, whether it spawned something, printed the fallback instruction,
or the spawn itself raised.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from typing import Callable

__all__ = ["pick_spawn_argv", "main_ui"]

_MANUAL_INSTRUCTION = "Open a split/terminal and run: spar watch"

_TERMINAL_CASCADE = ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")


def pick_spawn_argv(
    env: dict, which: Callable[[str], "str | None"]
) -> "list[str] | None":
    """Decide how to spawn a ``spar watch`` viewer window; see module docstring."""
    if env.get("TMUX"):
        return ["tmux", "split-window", "-h", "spar watch"]

    # Step 2 (warp-terminal) is intentionally a no-op -- see module
    # docstring. It always falls through to step 3/4 below.

    for term in _TERMINAL_CASCADE:
        if which(term):
            if term == "gnome-terminal":
                return ["gnome-terminal", "--", "spar", "watch"]
            return [term, "-e", "spar", "watch"]

    return None


def main_ui(argv=None) -> int:
    """Entry point for the ``spar ui`` subcommand. Always exits 0."""
    parser = argparse.ArgumentParser(
        prog="spar ui",
        description="Open a live viewer window running 'spar watch'",
    )
    parser.parse_args(argv)

    spawn_argv = pick_spawn_argv(dict(os.environ), shutil.which)
    if spawn_argv is None:
        print(_MANUAL_INSTRUCTION)
        return 0

    try:
        subprocess.Popen(
            spawn_argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        print(_MANUAL_INSTRUCTION)
    return 0
