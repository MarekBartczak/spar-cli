"""Preflight for the side CLIs a run is about to spawn.

Every side of a debate/execution is an external CLI (``claude``, ``codex``,
...). When one of them is not on ``PATH`` the failure used to surface as a bare
``FileNotFoundError`` from ``Popen`` — *after* the other side had already
burned a full turn, and (under the GUI, which does not read the engine's
stderr) with no visible message at all: the run simply stopped.

This module makes that class of failure impossible to miss: the CLI checks
every selected side's command before any lock is taken or turn is run, and
refuses with a message that names the side, the command and the ``PATH`` that
was searched.
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import Callable, Mapping

__all__ = [
    "SideCommandMissing",
    "missing_side_commands",
    "missing_side_commands_message",
]


class SideCommandMissing(Exception):
    """A selected side's CLI is not executable on this machine."""


def _program_token(command: str) -> str:
    """The program of a configured command (``"codex --foo"`` -> ``"codex"``).

    Falls back to the raw string when it cannot be lexed, so a weird config
    still gets probed (and reported) rather than silently skipped.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return tokens[0] if tokens else command


def missing_side_commands(
    commands: Mapping[str, str],
    which: Callable[[str], str | None] = shutil.which,
) -> list[tuple[str, str]]:
    """Return ``[(side, command), ...]`` for every command that does not resolve.

    Mapping order is preserved (deterministic messages). ``which`` is injectable
    for tests; the default ``shutil.which`` also handles absolute/relative paths
    and the executable bit.
    """
    missing: list[tuple[str, str]] = []
    for side, command in commands.items():
        if not which(_program_token(command)):
            missing.append((side, command))
    return missing


def missing_side_commands_message(
    missing: list[tuple[str, str]], path: str | None = None
) -> str:
    """Human-readable refusal for the result of :func:`missing_side_commands`."""
    searched = os.environ.get("PATH", "") if path is None else path
    listed = ", ".join(f"{side} -> {command!r}" for side, command in missing)
    return (
        f"spar: side CLI not executable: {listed}. "
        "Install it, or point the side at a full path with "
        "'spar -m <side> -setCommand /full/path/to/cli'.\n"
        f"spar: searched PATH={searched}"
    )
