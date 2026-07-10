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

__all__ = ["repo_state", "create_repo", "create_initial_commit", "ensure_project_config"]

_INITIAL_COMMIT_MESSAGE = "spar: initial state"

_CONFIG_TEMPLATE = """\
# spar project configuration — adjust models to what your CLIs accept
# and SET test_command before running `spar exec`.

[sides.claude]
models = ["opus", "sonnet", "haiku"]
default_model = "sonnet"
# planning (debate) runs on the strongest model
debate_model = "opus"
# models allowed to IMPLEMENT / REVIEW tasks (floors)
impl_models = ["opus", "sonnet"]
review_models = ["opus", "sonnet"]

[sides.codex]
models = ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4"]
default_model = "gpt-5.5"
debate_model = "gpt-5.6-sol"
review_models = ["gpt-5.6-sol", "gpt-5.5", "gpt-5.4"]

[debate]
max_rounds = 8

[execution]
# REQUIRED before `spar exec`: the command gating the final merge
# (and the per-task fallback), e.g. "python3 -m pytest -q tests"
# or "make test".
test_command = ""
max_review_rounds = 3
max_fix_tasks = 2
# build artifacts the scope guard must ignore, e.g.:
# scope_ignore = ["__pycache__/", "*.pyc", "build/"]
"""


def ensure_project_config(project_dir: "str | Path") -> bool:
    """Create a starter ``.spar/config.toml`` when the project has none.

    A fresh project without a config is broken: empty model catalogs make
    ``--tasks`` validation reject every plan. Returns ``True`` when the
    starter file was written, ``False`` when a config already existed (in
    which case this is a strict no-op -- an existing file is never
    overwritten).
    """
    config_path = Path(project_dir) / ".spar" / "config.toml"
    if config_path.exists():
        return False
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    return True


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


def _ensure_spar_gitignored(project_dir: "str | Path") -> None:
    """Make sure ``.spar/`` is gitignored before the initial commit.

    spar's runtime state (live.log, session.json, transcripts) lives in
    ``.spar/`` and mutates constantly; left untracked it dirties the work
    tree and ``spar exec`` then refuses to start ("target not clean").
    Appends to an existing .gitignore, creates one otherwise; no-op when
    ``.spar`` is already covered.
    """
    from pathlib import Path as _P

    gi = _P(project_dir) / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if any(line.strip().rstrip("/") == ".spar" for line in existing.splitlines()):
        return
    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    gi.write_text(existing + prefix + ".spar/\n", encoding="utf-8")


def create_repo(project_dir: "str | Path") -> None:
    """Initialize a fresh repo in ``project_dir`` with an initial commit."""
    _run(project_dir, "init", "-b", "master")
    _ensure_commit_identity(project_dir)
    _ensure_spar_gitignored(project_dir)
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
    _ensure_spar_gitignored(project_dir)
    _run(project_dir, "add", "-A")
    _run(project_dir, "commit", "--allow-empty", "-m", _INITIAL_COMMIT_MESSAGE)
