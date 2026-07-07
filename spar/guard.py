"""Artifact contract guard: pre-turn snapshot, post-turn checks, rollback.

A :class:`Guard` protects the shared artifact and the rest of the repo from
a misbehaving side. Before each turn the orchestrator calls
:meth:`Guard.pre_turn` to snapshot the artifact's size and a full file
inventory of the repo (plus, in a git repo, ``git status --porcelain``).
After the turn the orchestrator calls the guard (``guard(ctx)``) which runs
three checks in order — artifact sanity, "not gutted" (no drastic shrink),
and "no foreign changes" (nothing outside the artifact was touched) — and
raises :class:`GuardViolation` on the first failure. A foreign-changes
violation first attempts to roll back what it safely can (deleting new
files, ``git checkout --`` for files that were clean before the turn) and
reports anything it could not safely touch as "manual cleanup required".

``GuardContext`` and ``GuardViolation`` are defined in :mod:`spar.orchestrator`
(the hook contract) and re-exported here so both import paths work.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from spar.orchestrator import GuardContext, GuardViolation

__all__ = ["Guard", "GuardContext", "GuardViolation"]


_ALWAYS_SKIP_DIR_NAMES = {".git", "__pycache__"}


def _walk_inventory(repo_dir: Path, spar_dir: Path) -> dict[str, tuple[int, int]]:
    """Map ``relative posix path -> (mtime_ns, size)`` for every file under
    ``repo_dir``, skipping ``.git``, ``__pycache__`` (by name, anywhere in the
    tree) and ``spar_dir`` (by resolved path, wherever it lives).
    """
    repo_dir = repo_dir.resolve()
    skip_abs = {spar_dir.resolve()}

    inventory: dict[str, tuple[int, int]] = {}
    for root, dirs, files in os.walk(repo_dir):
        root_path = Path(root)
        kept = []
        for d in dirs:
            if d in _ALWAYS_SKIP_DIR_NAMES:
                continue
            if (root_path / d).resolve() in skip_abs:
                continue
            kept.append(d)
        dirs[:] = kept

        for fname in files:
            fpath = root_path / fname
            try:
                st = fpath.stat()
            except OSError:
                continue
            rel = fpath.relative_to(repo_dir).as_posix()
            inventory[rel] = (st.st_mtime_ns, st.st_size)
    return inventory


def _git_status_porcelain(repo_dir: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout


def _dirty_paths_from_porcelain(porcelain: str) -> set[str]:
    """Every path mentioned by ``git status --porcelain`` — staged, modified,
    or untracked. Absence from this set means "clean" (tracked, unmodified).
    """
    dirty: set[str] = set()
    for line in porcelain.splitlines():
        if not line or len(line) < 4:
            continue
        path_part = line[3:]
        if " -> " in path_part:  # rename: "old -> new"
            _, path_part = path_part.split(" -> ", 1)
        path_part = path_part.strip()
        if path_part.startswith('"') and path_part.endswith('"'):
            path_part = path_part[1:-1]
        dirty.add(path_part)
    return dirty


@dataclass
class _Snapshot:
    artifact_size: int
    inventory: dict[str, tuple[int, int]]
    dirty_paths: set[str] = field(default_factory=set)


class Guard:
    """Snapshot-check-rollback guard hook (see :class:`spar.orchestrator.GuardHook`)."""

    def __init__(
        self,
        repo_dir: Path,
        artifact_path: Path,
        spar_dir: Path,
        shrink_threshold: float = 0.6,
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.artifact_path = Path(artifact_path)
        self.spar_dir = Path(spar_dir)
        self.shrink_threshold = shrink_threshold
        self._is_git = (self.repo_dir / ".git").exists()
        self._snapshot: _Snapshot | None = None

    # -- pre-turn --------------------------------------------------------

    def pre_turn(self) -> None:
        """Snapshot artifact size, file inventory, and (in a git repo) the
        working-tree status, ahead of the upcoming turn.
        """
        artifact_size = (
            self.artifact_path.stat().st_size if self.artifact_path.exists() else 0
        )
        inventory = _walk_inventory(self.repo_dir, self.spar_dir)
        dirty_paths: set[str] = set()
        if self._is_git:
            dirty_paths = _dirty_paths_from_porcelain(_git_status_porcelain(self.repo_dir))
        self._snapshot = _Snapshot(
            artifact_size=artifact_size, inventory=inventory, dirty_paths=dirty_paths
        )

    # -- post-turn ---------------------------------------------------------

    def __call__(self, ctx: GuardContext) -> None:
        self._check_artifact_sane(ctx)
        self._check_not_gutted(ctx)
        self._check_no_foreign_changes(ctx)

    def _check_artifact_sane(self, ctx: GuardContext) -> None:
        p = ctx.artifact_path
        if not p.exists() or not p.is_file():
            raise GuardViolation(f"artifact contract: missing or not a file: {p}")
        data = p.read_bytes()
        if len(data) == 0:
            raise GuardViolation(f"artifact contract: artifact is empty: {p}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GuardViolation(
                f"artifact contract: artifact is not valid UTF-8: {p} ({exc})"
            ) from exc

    def _check_not_gutted(self, ctx: GuardContext) -> None:
        if self._snapshot is None:
            return
        pre_size = self._snapshot.artifact_size
        if pre_size <= 0:
            return  # a freshly created artifact never trips the shrink check
        post_size = ctx.artifact_path.stat().st_size
        min_allowed = pre_size * (1 - self.shrink_threshold)
        if post_size < min_allowed:
            shrink_pct = (1 - post_size / pre_size) * 100
            raise GuardViolation(
                f"artifact shrank by {shrink_pct:.0f}% "
                f"(pre={pre_size}b, post={post_size}b, "
                f"threshold={self.shrink_threshold * 100:.0f}%)"
            )

    def _check_no_foreign_changes(self, ctx: GuardContext) -> None:
        if self._snapshot is None:
            return

        current_inventory = _walk_inventory(self.repo_dir, self.spar_dir)
        artifact_rel = self._artifact_rel()
        pre_inv = self._snapshot.inventory

        new_paths: list[str] = []
        deleted_paths: list[str] = []
        changed_paths: list[str] = []

        for path in sorted(set(pre_inv) | set(current_inventory)):
            if path == artifact_rel:
                continue
            pre = pre_inv.get(path)
            post = current_inventory.get(path)
            if pre is None and post is not None:
                new_paths.append(path)
            elif pre is not None and post is None:
                deleted_paths.append(path)
            elif pre is not None and post is not None and pre != post:
                changed_paths.append(path)

        if not new_paths and not deleted_paths and not changed_paths:
            return

        rollback_msgs = self._rollback(new_paths, changed_paths, deleted_paths)

        foreign = new_paths + deleted_paths + changed_paths
        msg = "foreign changes detected outside the artifact: " + ", ".join(foreign)
        if rollback_msgs:
            msg += "; " + "; ".join(rollback_msgs)
        raise GuardViolation(msg)

    # -- rollback ------------------------------------------------------

    def _rollback(
        self, new_paths: list[str], changed_paths: list[str], deleted_paths: list[str]
    ) -> list[str]:
        msgs: list[str] = []

        for rel in new_paths:
            full = self.repo_dir / rel
            try:
                full.unlink()
                self._remove_empty_parents(full)
            except OSError as exc:
                msgs.append(f"rollback failed for new file {rel}: {exc}")

        manual: list[str] = []
        checkoutable: list[str] = []
        for rel in changed_paths + deleted_paths:
            if not self._is_git:
                manual.append(rel)
                continue
            if rel in self._snapshot.dirty_paths:
                # already dirty (or untracked) before the turn: no safe baseline
                manual.append(rel)
                continue
            checkoutable.append(rel)

        for rel in checkoutable:
            try:
                result = subprocess.run(
                    ["git", "checkout", "--", rel],
                    cwd=self.repo_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except OSError as exc:
                manual.append(rel)
                msgs.append(f"git checkout raised for {rel}: {exc}")
                continue
            if result.returncode != 0:
                manual.append(rel)
                msgs.append(f"git checkout failed for {rel}: {result.stderr.strip()}")

        if manual:
            msgs.append("manual cleanup required: " + ", ".join(sorted(set(manual))))
        return msgs

    def _remove_empty_parents(self, path: Path) -> None:
        repo_dir = self.repo_dir.resolve()
        parent = path.resolve().parent
        while parent != repo_dir and repo_dir in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _artifact_rel(self) -> str | None:
        try:
            return self.artifact_path.resolve().relative_to(self.repo_dir.resolve()).as_posix()
        except ValueError:
            return None
