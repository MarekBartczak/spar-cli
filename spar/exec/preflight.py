"""Preflight validation of per-task test commands.

A FRESH ``spar exec`` refuses to start when any task's ``test`` command names
a tool that does not exist on this machine. Live incident driving this: a
plan wrote ``python -m py_compile`` on a python3-only host, and the failure
only surfaced deep into the run (exit 127 in the per-task test loop). The
mid-run 126/127 gate and the ``fix:<command>`` decision already handle a
broken command discovered *during* a run; this module catches it *before any
work starts* — no integration branch, no adapter turn, no state file.

Resume (``spar exec --continue``) is deliberately exempt: a command corrected
via ``fix:`` is already persisted in state, and the 126/127 gate covers
anything still broken.
"""

from __future__ import annotations

import re
import shlex
import shutil
from typing import Callable, Iterable

from spar.exec.tasklist import Task

__all__ = ["first_command_token", "preflight_test_commands"]

# Shell keywords/builtins the ``shell=True`` (``/bin/sh``) test runner resolves
# WITHOUT a binary on PATH — ``shutil.which`` would wrongly flag them as
# missing and refuse the run. Covers POSIX special built-ins, the common
# regular built-ins, and reserved words that can begin a command (so a command
# opening with ``.``/``eval``/``if``/``{``/``(`` is not treated as a missing
# binary). Live false positive that motivated the widening:
# ``. venv/bin/activate && pytest`` was refused with exit 2.
_SHELL_BUILTINS = frozenset(
    {
        # POSIX special built-ins
        ".", ":", "break", "continue", "eval", "exec", "exit", "export",
        "readonly", "return", "set", "shift", "times", "trap", "unset",
        # regular built-ins (dash/POSIX resolve these without a PATH binary)
        "alias", "bg", "cd", "command", "echo", "false", "fc", "fg",
        "getopts", "hash", "jobs", "kill", "local", "printf", "pwd", "read",
        "test", "[", "true", "type", "ulimit", "umask", "unalias", "wait",
        # reserved words that can begin a command
        "!", "{", "}", "(", ")", "case", "do", "done", "elif", "else",
        "esac", "fi", "for", "if", "in", "then", "until", "while",
    }
)

# Leading ``VAR=value`` environment assignments (``PYTHONPATH=x python3 …``).
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def first_command_token(cmd: str) -> str | None:
    """The first shell token of ``cmd`` that names the command to run.

    Leading ``VAR=value`` environment assignments are skipped; quoting is
    handled via :mod:`shlex`. Returns ``None`` when there is no command token
    (empty string, assignments only) or the command cannot be tokenized
    (unbalanced quote) — callers treat ``None`` as "nothing to validate"
    rather than guessing.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return None
    for tok in tokens:
        if _ENV_ASSIGN.match(tok):
            continue
        return tok
    return None


def preflight_test_commands(
    tasks: Iterable[Task],
    which: Callable[[str], str | None] = shutil.which,
) -> list[str]:
    """Validate every task's ``test`` command against this machine's PATH.

    Returns one human-readable problem line per task whose command's first
    token is neither a shell builtin nor found by ``which`` (injectable for
    tests), e.g.::

        [t1] test 'python -m py_compile a.py' uses 'python' — not found on
        this machine (try 'python3'?)

    The ``python`` → ``python3`` suggestion is the one special-cased hint
    (the incident that motivated this check); an empty/absent ``test`` is
    skipped (the task falls back to the global test command).
    """
    problems: list[str] = []
    for task in tasks:
        cmd = task.test
        if not cmd:
            continue
        # Conservative: a command using shell substitution/expansion cannot be
        # validated by first-token extraction (``n=$(($(cat f)+1)); …`` has no
        # plain command word). Never block a run on a guess — skip it; a
        # genuinely broken command is still caught mid-run by the 126/127 gate.
        if "$(" in cmd or "`" in cmd:
            continue
        tok = first_command_token(cmd)
        if tok is None or tok in _SHELL_BUILTINS or "$" in tok or which(tok):
            continue
        hint = ""
        if tok == "python" and which("python3"):
            hint = " (try 'python3'?)"
        problems.append(
            f"[{task.id}] test {cmd!r} uses {tok!r} — not found on this "
            f"machine{hint}"
        )
    return problems
