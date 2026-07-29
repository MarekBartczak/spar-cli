"""Artifact contract guard: pre-turn snapshot, post-turn checks, rollback.

A :class:`Guard` protects the shared artifact and the rest of the repo from
a misbehaving side. Before each turn the orchestrator calls
:meth:`Guard.pre_turn` to snapshot the artifact's size and a full file
inventory of the repo (plus, in a git repo, ``git status --porcelain=v1 -z``).
After the turn the orchestrator calls the guard (``guard(ctx)``) which runs
three checks in order — artifact sanity, "not gutted" (no drastic shrink),
and "no foreign changes" (nothing outside the artifact was touched) — and
raises :class:`GuardViolation` on the first failure. A foreign-changes
violation first attempts to roll back what it safely can (deleting new
files, ``git checkout --`` for *tracked* files that were clean before the
turn) and reports anything it could not safely touch as "manual cleanup
required".

The inventory skips dependency/cache trees by name (``_ALWAYS_SKIP_DIR_NAMES``)
plus whatever ``[execution] scope_ignore`` matches, so build artifacts a side
legitimately produces by running the test suite never count as foreign
changes.

``GuardContext`` and ``GuardViolation`` are defined in :mod:`spar.orchestrator`
(the hook contract) and re-exported here so both import paths work.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

from spar.orchestrator import GuardContext, GuardViolation

__all__ = ["Guard", "GuardContext", "GuardViolation"]


# Dependency and cache trees: never sources, always huge, and routinely
# rewritten by the very build/test commands a side legitimately runs during
# its turn (e.g. vitest writing node_modules/.vite/**/results.json). Walking
# them costs tens of thousands of stat() calls per turn and produces foreign
# changes that no rollback can undo, so they are skipped outright. Ambiguous
# output dirs (dist/, build/, target/, coverage/) are NOT hardcoded here --
# use ``[execution] scope_ignore`` for those.
_ALWAYS_SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".vite",
    ".turbo",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".parcel-cache",
    ".gradle",
}


class _IgnoreMatcher:
    """Minimal gitignore-flavoured matcher for ``scope_ignore`` patterns.

    A pattern without ``/`` matches any path component (``*.pyc``,
    ``coverage``, ``node_modules/``); a pattern with ``/`` matches the
    repo-relative posix path, either as a glob or as a directory prefix
    (``app/backend/dist/``). Trailing slashes are stripped -- a pattern
    matches files and directories alike, which is what a scope guard wants.
    """

    def __init__(self, patterns: Iterable[str]) -> None:
        self.name_pats: list[str] = []
        self.path_pats: list[str] = []
        for raw in patterns:
            pat = raw.strip().rstrip("/")
            if not pat:
                continue
            if "/" in pat:
                self.path_pats.append(pat)
            else:
                self.name_pats.append(pat)

    def __bool__(self) -> bool:
        return bool(self.name_pats or self.path_pats)

    def __call__(self, rel: str) -> bool:
        base = rel.rsplit("/", 1)[-1]
        for pat in self.name_pats:
            if fnmatch(base, pat):
                return True
        for pat in self.path_pats:
            if fnmatch(rel, pat) or rel.startswith(f"{pat.rstrip('*')}/"):
                return True
        return False


def _walk_inventory(
    repo_dir: Path, spar_dir: Path, ignore: Callable[[str], bool] | None = None
) -> tuple[dict[str, tuple[int, int]], set[str]]:
    """Map ``relative posix path -> (mtime_ns, size)`` for every file under
    ``repo_dir``, plus the set of relative posix paths of every directory
    seen, skipping ``_ALWAYS_SKIP_DIR_NAMES`` (by name, anywhere in the tree),
    ``spar_dir`` (by resolved path, wherever it lives) and anything ``ignore``
    matches (by repo-relative posix path).
    """
    repo_dir = repo_dir.resolve()
    skip_abs = {spar_dir.resolve()}

    inventory: dict[str, tuple[int, int]] = {}
    dirs_seen: set[str] = set()
    for root, dirs, files in os.walk(repo_dir):
        root_path = Path(root)
        kept = []
        for d in dirs:
            if d in _ALWAYS_SKIP_DIR_NAMES:
                continue
            if (root_path / d).resolve() in skip_abs:
                continue
            rel_dir = (root_path / d).relative_to(repo_dir).as_posix()
            if ignore is not None and ignore(rel_dir):
                continue
            kept.append(d)
        dirs[:] = kept
        for d in kept:
            rel_dir = (root_path / d).relative_to(repo_dir).as_posix()
            dirs_seen.add(rel_dir)

        for fname in files:
            fpath = root_path / fname
            rel = fpath.relative_to(repo_dir).as_posix()
            if ignore is not None and ignore(rel):
                continue
            try:
                st = fpath.stat()
            except OSError:
                continue
            inventory[rel] = (st.st_mtime_ns, st.st_size)
    return inventory, dirs_seen


def _git_status_porcelain_z(repo_dir: Path) -> bytes:
    """Raw NUL-separated output of ``git status --porcelain=v1 -z``.

    The ``-z`` form disables C-quoting entirely, so paths with non-ASCII
    characters, quotes, backslashes, etc. come back byte-for-byte instead of
    escaped (as they would under the default ``core.quotepath`` with plain
    ``--porcelain``). Records are NUL-terminated; rename/copy records are two
    consecutive NUL-terminated fields (``XY new\\0old\\0``).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z"],
            cwd=repo_dir,
            capture_output=True,
            text=False,
            check=False,
        )
    except OSError:
        return b""
    if result.returncode != 0:
        return b""
    return result.stdout


def _tracked_paths(repo_dir: Path) -> set[str]:
    """Every path git tracks in ``repo_dir`` (``git ls-files -z``).

    A path outside this set has no committed baseline, so ``git checkout --``
    on it always fails with "did not match any file(s) known to git" -- the
    rollback must classify it as manual cleanup instead of attempting a
    checkout. Untracked-but-not-dirty is not a contradiction: gitignored
    paths are absent from plain ``git status --porcelain`` output too.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repo_dir,
            capture_output=True,
            text=False,
            check=False,
        )
    except OSError:
        return set()
    if result.returncode != 0:
        return set()
    return {
        field_.decode("utf-8", errors="surrogateescape")
        for field_ in result.stdout.split(b"\0")
        if field_
    }


def _dirty_paths_from_porcelain(raw: bytes) -> set[str]:
    """Every path mentioned by ``git status --porcelain=v1 -z`` output —
    staged, modified, or untracked. Absence from this set means "clean"
    (tracked, unmodified).

    ``raw`` is NUL-separated records: each record is ``XY <path>``, except
    rename/copy records (``XY`` starting with ``R``/``C``) which are followed
    by a *second* NUL-terminated field holding the old path — both fields
    must be consumed or the parse desyncs on the next record.
    """
    dirty: set[str] = set()
    fields = raw.split(b"\0")
    i = 0
    while i < len(fields):
        record = fields[i]
        i += 1
        if not record:
            continue
        status = record[:2]
        path_part = record[3:].decode("utf-8", errors="surrogateescape")
        dirty.add(path_part)
        if status[0:1] in (b"R", b"C"):
            # rename/copy: next NUL-terminated field is the old path.
            if i < len(fields):
                old_path = fields[i].decode("utf-8", errors="surrogateescape")
                dirty.add(old_path)
                i += 1
    return dirty


@dataclass
class _Snapshot:
    artifact_size: int
    inventory: dict[str, tuple[int, int]]
    dirty_paths: set[str] = field(default_factory=set)
    dirs: set[str] = field(default_factory=set)
    tracked_paths: set[str] = field(default_factory=set)


class Guard:
    """Snapshot-check-rollback guard hook (see :class:`spar.orchestrator.GuardHook`)."""

    def __init__(
        self,
        repo_dir: Path,
        artifact_path: Path,
        spar_dir: Path,
        shrink_threshold: float = 0.6,
        scope_ignore: Iterable[str] = (),
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.artifact_path = Path(artifact_path)
        self.spar_dir = Path(spar_dir)
        self.shrink_threshold = shrink_threshold
        self._is_git = (self.repo_dir / ".git").exists()
        self._snapshot: _Snapshot | None = None
        matcher = _IgnoreMatcher(scope_ignore)
        self._ignore: Callable[[str], bool] | None = matcher if matcher else None

    # -- pre-turn --------------------------------------------------------

    def pre_turn(self) -> None:
        """Snapshot artifact size, file inventory, and (in a git repo) the
        working-tree status, ahead of the upcoming turn.
        """
        artifact_size = (
            self.artifact_path.stat().st_size if self.artifact_path.exists() else 0
        )
        inventory, dirs = _walk_inventory(self.repo_dir, self.spar_dir, self._ignore)
        dirty_paths: set[str] = set()
        tracked: set[str] = set()
        if self._is_git:
            dirty_paths = _dirty_paths_from_porcelain(_git_status_porcelain_z(self.repo_dir))
            tracked = _tracked_paths(self.repo_dir)
        self._snapshot = _Snapshot(
            artifact_size=artifact_size,
            inventory=inventory,
            dirty_paths=dirty_paths,
            dirs=dirs,
            tracked_paths=tracked,
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

        current_inventory, _current_dirs = _walk_inventory(
            self.repo_dir, self.spar_dir, self._ignore
        )
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
            if rel not in self._snapshot.tracked_paths:
                # untracked before the turn (typically gitignored build output):
                # git has no baseline, so `git checkout --` would only fail with
                # "did not match any file(s) known to git".
                manual.append(rel)
                continue
            if rel in self._snapshot.dirty_paths:
                # already dirty before the turn: no safe baseline
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
        pre_turn_dirs = self._snapshot.dirs if self._snapshot is not None else set()
        parent = path.resolve().parent
        while parent != repo_dir and repo_dir in parent.parents:
            rel = parent.relative_to(repo_dir).as_posix()
            if rel in pre_turn_dirs:
                # existed before the turn (e.g. a committed empty directory);
                # never delete directories we didn't create ourselves.
                break
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
