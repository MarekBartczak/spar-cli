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
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, QSettings

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


def window_title(project_dir: "str | Path") -> str:
    """Title bar text: project name plus its parent, so two same-named
    repos in different trees are distinguishable across windows."""
    path = _resolved(project_dir)
    if path.parent == path:  # filesystem root
        return f"spar — {path}"
    parent = str(path.parent)
    home = str(Path.home())
    if parent == home:
        parent = "~"
    elif parent.startswith(home + "/"):
        parent = "~/" + parent[len(home) + 1:]
    return f"spar — {path.name} ({parent})"


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


def _macos_bundle_path() -> "Path | None":
    """``…/Spar.app`` for a frozen macOS bundle, else None."""
    exe = Path(sys.executable)
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent
    return None


def new_window_command(project_dir: "str | Path") -> list[str]:
    """argv that launches a NEW gui process for ``project_dir``."""
    target = str(_resolved(project_dir))
    if getattr(sys, "frozen", False):
        bundle = _macos_bundle_path() if sys.platform == "darwin" else None
        if bundle is not None:
            # Without -n, `open` just activates the running bundle instead of
            # starting a second instance — which is exactly what a NEW project
            # window must not do.
            return ["open", "-n", "-a", str(bundle), "--args", "--dir", target]
        return [sys.executable, "--dir", target]
    return [sys.executable, "-m", "spar.cli", "gui", "--dir", target]


def spawn_new_window(project_dir: "str | Path") -> bool:
    """Start a detached gui process for ``project_dir`` (fire and forget)."""
    program, *arguments = new_window_command(project_dir)
    ok, _pid = QProcess.startDetached(
        program, arguments, str(_resolved(project_dir))
    )
    return bool(ok)
