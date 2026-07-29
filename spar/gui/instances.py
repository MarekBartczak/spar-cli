"""Multi-instance plumbing: one window = one project = one OS process.

Every GUI process serves exactly ONE project directory (ADR 0007). This
module owns everything that has to agree on *which* project a process is:

* ``project_key`` — a stable short digest of the RESOLVED project path,
  used both as the QSettings prefix for that project's layout state and as
  the basis for its QLocalServer name, so ``.``, ``~/p/../p`` and a
  trailing slash all mean one window and one settings scope.
* the recent-projects list (global, shared by every window),
* the detached-spawn command for opening another project in a new window,
* :class:`SingleInstanceGuard` — claim-or-raise for a project's window.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PySide6.QtCore import QSettings

_RECENT_KEY = "recent_projects"
_RECENT_MAX = 10


def _resolved(project_dir: "str | Path") -> Path:
    return Path(project_dir).expanduser().resolve()


def project_key(project_dir: "str | Path") -> str:
    """Stable 12-hex-char identity of a project directory."""
    digest = hashlib.sha1(str(_resolved(project_dir)).encode("utf-8"))
    return digest.hexdigest()[:12]


def scoped(project_dir: "str | Path", key: str) -> str:
    """QSettings key ``key`` namespaced to one project."""
    return f"projects/{project_key(project_dir)}/{key}"


def _settings() -> QSettings:
    return QSettings("spar", "gui")


def recent_projects() -> list[str]:
    """Most-recent-first project paths that still exist on disk (max 10)."""
    raw = _settings().value(_RECENT_KEY)
    if raw is None:
        return []
    if isinstance(raw, str):  # QSettings collapses 1-elem lists
        raw = [raw]
    out: list[str] = []
    for item in raw:
        path = str(item)
        if path not in out and Path(path).is_dir():
            out.append(path)
    return out[:_RECENT_MAX]


def push_recent_project(project_dir: "str | Path") -> None:
    """Record ``project_dir`` as the most recently opened project."""
    path = str(_resolved(project_dir))
    rest = [p for p in recent_projects() if p != path]
    _settings().setValue(_RECENT_KEY, [path] + rest[: _RECENT_MAX - 1])
