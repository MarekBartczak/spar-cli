"""Pliki module (ADR 0006 tranche A): project tree + Pygments editor + finder.

Qt-free helpers (lexer pick, fuzzy scorer, file index) live ABOVE the
``if _HAS_QT:`` guard so the module imports on a plain interpreter and their
tests run under a plain ``python3`` (no importorskip) — mirroring
orchestrator.py / rails.py. The Qt layer (FileEditor, FilesView,
FileFinderOverlay, ...) is only defined when PySide6 is importable.
"""
from __future__ import annotations

import os
from pathlib import Path

from pygments.lexers import get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

__all__ = [
    "pick_lexer",
    "fuzzy_score",
    "filter_paths",
    "build_file_index",
]

# Directories never walked for the finder index (ADR 0006). ``.git`` is also
# hidden from the tree via _TreeFilterProxy; the others are noise for a
# name index.
_FINDER_SKIP_DIRS = frozenset({".git", "node_modules", ".venv", "__pycache__"})


def pick_lexer(filename: str):
    """Return a Pygments lexer chosen by *filename* alone.

    Unknown extensions fall back to ``TextLexer`` (plain, no highlighting)
    so arbitrary files still open. Pure: matches by name, never reads the
    file, no Qt.
    """
    try:
        return get_lexer_for_filename(filename)
    except ClassNotFound:
        return TextLexer()


def fuzzy_score(query: str, candidate: str) -> "int | None":
    """Case-insensitive subsequence fuzzy match.

    Returns ``None`` when *query*'s chars are not an in-order subsequence of
    *candidate*; otherwise a score where higher is better. An empty query
    returns ``-len(candidate)`` (everything matches; shorter paths rank
    first). Bonuses: +5 per char contiguous with the previous match, +12
    when a matched char starts the basename, +4 when a matched char follows
    a path separator. A tie-break of ``-len(candidate)`` keeps tighter
    matches ahead. Pure.
    """
    q = query.lower()
    c = candidate.lower()
    if not q:
        return -len(candidate)
    base_start = c.rfind("/") + 1
    score = 0
    cursor = 0
    prev = -2
    for ch in q:
        found = c.find(ch, cursor)
        if found == -1:
            return None
        if found == prev + 1:
            score += 5
        if found == base_start:
            score += 12
        elif found > 0 and c[found - 1] == "/":
            score += 4
        prev = found
        cursor = found + 1
    return score - len(candidate)


def filter_paths(query: str, paths: "list[str]") -> "list[str]":
    """Rank *paths* by :func:`fuzzy_score` (desc), dropping non-matches.

    Ties break by path for a stable, deterministic order. Pure.
    """
    scored = []
    for p in paths:
        s = fuzzy_score(query, p)
        if s is not None:
            scored.append((s, p))
    scored.sort(key=lambda sp: (-sp[0], sp[1]))
    return [p for _s, p in scored]


def build_file_index(
    root: "str | Path", skip_dirs: "frozenset[str]" = _FINDER_SKIP_DIRS
) -> "list[str]":
    """Walk *root*, returning sorted project-relative POSIX file paths.

    Prunes *skip_dirs* in place while walking. Touches the filesystem but no
    Qt — unit-tested with tmp dirs under a plain python3.
    """
    root = Path(root)
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for name in filenames:
            out.append(Path(dirpath, name).relative_to(root).as_posix())
    out.sort()
    return out


try:  # pragma: no cover - exercised via the two interpreters
    from PySide6.QtCore import Qt  # noqa: F401  (probe only)

    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False


if _HAS_QT:
    pass  # Qt widgets added in Tasks 2–6.
