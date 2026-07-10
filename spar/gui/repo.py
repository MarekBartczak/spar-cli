"""Git-repo prerequisite checks for the gui's "Nowa debata" flow.

A debate must not start outside a local git repo (spar drives branches and
merges) and ``spar exec`` additionally needs at least one commit so its
``rev-parse HEAD`` doesn't die on an empty repo. :func:`repo_state` is the
pure(ish) probe -- it only shells out to read-only ``git`` calls -- kept
Qt-free so it is trivially unit-testable against real tmp git repos.
:func:`create_repo` / :func:`create_initial_commit` are the two mutating
actions the toolbar's confirmation dialog can trigger.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

__all__ = ["repo_state", "create_repo", "create_initial_commit"]

_INITIAL_COMMIT_MESSAGE = "spar: initial state"


def _run(project_dir: "str | Path", *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(project_dir), *args],
        capture_output=True,
        text=True,
    )


def repo_state(project_dir: "str | Path") -> str:
    """Classify ``project_dir`` as ``"ok"``, ``"no_head"`` or ``"none"``.

    * ``"none"``    -- not inside a git work tree at all.
    * ``"no_head"`` -- a work tree, but zero commits (no ``HEAD``).
    * ``"ok"``      -- a work tree with at least one commit.
    """
    inside = _run(project_dir, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return "none"
    head = _run(project_dir, "rev-parse", "--verify", "-q", "HEAD")
    if head.returncode != 0:
        return "no_head"
    return "ok"


def create_repo(project_dir: "str | Path") -> None:
    """Initialize a fresh repo in ``project_dir`` with an initial commit."""
    _run(project_dir, "init", "-b", "master")
    _ensure_commit_identity(project_dir)
    _run(project_dir, "add", "-A")
    _run(project_dir, "commit", "--allow-empty", "-m", _INITIAL_COMMIT_MESSAGE)


def _ensure_commit_identity(project_dir: "str | Path") -> None:
    """Set a local, repo-scoped commit identity when none is configured.

    A freshly ``git init``-ed repo with no global ``user.name``/``user.email``
    (common in CI/sandboxed HOME setups) makes any commit fail outright; this
    keeps the "create the repo for me" offer working unconditionally without
    touching the user's global git config.
    """
    if _run(project_dir, "config", "user.email").returncode != 0:
        _run(project_dir, "config", "user.email", "spar@localhost")
    if _run(project_dir, "config", "user.name").returncode != 0:
        _run(project_dir, "config", "user.name", "spar")


def create_initial_commit(project_dir: "str | Path") -> None:
    """Commit whatever is staged/untracked in an existing headless repo."""
    _ensure_commit_identity(project_dir)
    _run(project_dir, "add", "-A")
    _run(project_dir, "commit", "--allow-empty", "-m", _INITIAL_COMMIT_MESSAGE)
