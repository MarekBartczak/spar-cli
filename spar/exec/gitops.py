"""Thin wrappers over the ``git`` CLI: branches, worktrees, merge, ancestor
checks, and diffs.

Every function shells out to a real ``git`` binary via ``subprocess.run`` and
converts a non-zero exit status into :class:`GitError` (carrying stderr).
Path-emitting subcommands (``status``, ``diff``) are invoked with
``-c core.quotePath=false`` so non-ASCII paths are not octal-escaped, and
their output is split by line (never shell-word-split) to recover paths
verbatim.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


class GitError(Exception):
    """Raised when a ``git`` invocation fails (non-zero, unexpected exit)."""


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_ok(repo: Path, *args: str) -> subprocess.CompletedProcess:
    result = _run(repo, *args)
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed with exit {result.returncode}")
    return result


def current_branch(repo: Path) -> str:
    result = _run_ok(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return result.stdout.strip()


def rev_parse(repo: Path, ref: str) -> str:
    result = _run_ok(repo, "rev-parse", ref)
    return result.stdout.strip()


def is_clean(repo: Path) -> bool:
    result = _run_ok(repo, "-c", "core.quotePath=false", "status", "--porcelain")
    return result.stdout.strip("\n") == ""


def create_branch(repo: Path, name: str, base: str) -> None:
    _run_ok(repo, "branch", name, base)


def checkout(repo: Path, ref: str) -> None:
    _run_ok(repo, "checkout", ref)


def delete_branch(repo: Path, name: str) -> None:
    _run_ok(repo, "branch", "-D", name)


def add_worktree(repo: Path, path: Path, branch: str) -> None:
    _run_ok(repo, "worktree", "add", str(path), branch)


def remove_worktree(repo: Path, path: Path) -> None:
    _run_ok(repo, "worktree", "remove", str(path))


def merge_no_ff(repo: Path, branch: str, message: str) -> None:
    _run_ok(repo, "merge", "--no-ff", "-m", message, branch)


def is_ancestor(repo: Path, maybe_ancestor: str, ref: str) -> bool:
    result = _run(repo, "merge-base", "--is-ancestor", maybe_ancestor, ref)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitError(result.stderr.strip() or f"git merge-base --is-ancestor failed with exit {result.returncode}")


def diff(repo: Path, base: str, ref: str) -> str:
    result = _run_ok(repo, "-c", "core.quotePath=false", "diff", f"{base}..{ref}")
    return result.stdout


def changed_files(repo: Path, base: str, ref: str) -> tuple[str, ...]:
    result = _run_ok(repo, "-c", "core.quotePath=false", "diff", "--name-only", f"{base}..{ref}")
    lines = result.stdout.strip("\n").split("\n")
    return tuple(line for line in lines if line)
