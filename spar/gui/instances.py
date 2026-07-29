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

from PySide6.QtCore import QObject, QProcess, QSettings, Signal
from PySide6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket

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


_RAISE_MESSAGE = b"raise\n"
_PROBE_TIMEOUT_MS = 1000


def local_server_name(project_dir: "str | Path") -> str:
    """QLocalServer name owned by the window serving ``project_dir``."""
    return f"spar-gui-{project_key(project_dir)}"


class SingleInstanceGuard(QObject):
    """One GUI window per project directory (ADR 0007).

    The FIRST process for a project claims a named local socket; later
    processes fail the claim and instead ask the owner to raise its window
    (WebStorm behavior), then exit. This is deliberately NOT the same
    mechanism as the engine's ``.spar/lock``: that lock guards the ENGINE
    (a headless CLI run holds it with no window to raise), and a foreign
    lock still means read-only ``RunnerState.LOCKED``.

    ``try_claim()`` owns the whole decision INCLUDING delivering the raise
    request: ``False`` means "an owner exists and has already been asked to
    raise its window", so the caller just exits. Callers must never send a
    second ``request_raise()`` — the owner would bounce twice.
    """

    raise_requested = Signal()

    def __init__(
        self,
        project_dir: "str | Path",
        parent=None,
        *,
        server_factory=None,
    ):
        super().__init__(parent)
        self.name = local_server_name(project_dir)
        self._server = None
        # Injection seam for tests: the stale-socket branch cannot be
        # reproduced with real sockets (removeServer() succeeds against a
        # live listener), so the branch tests script `listen` outcomes.
        self._server_factory = server_factory or (lambda parent: QLocalServer(parent))
        self._owned = False
        # Raises can arrive while MainWindow is still being built (FilesView
        # pumps the event loop), i.e. before any receiver exists.
        self._attached = False
        self._pending_raise = False

    # -- owner side ----------------------------------------------------
    def try_claim(self) -> bool:
        """True if this process now owns the project's window slot."""
        server = self._server_factory(self)
        server.newConnection.connect(self._on_connection)
        if server.listen(self.name):
            self._server = server
            self._owned = True
            return True
        if server.serverError() != QAbstractSocket.SocketError.AddressInUseError:
            # Unknown listen failure: never make the gui unlaunchable.
            print(
                f"spar gui: single-instance socket unavailable ({self.name}); "
                "continuing without it",
                file=sys.stderr,
            )
            return True
        # Address in use: a live owner, or a socket file left by a crash.
        if self.request_raise(timeout_ms=_PROBE_TIMEOUT_MS):
            return False  # live owner answered — caller must exit
        QLocalServer.removeServer(self.name)
        if server.listen(self.name):
            self._server = server
            self._owned = True
            return True
        print(
            f"spar gui: could not claim {self.name}; continuing without "
            "single-instance protection",
            file=sys.stderr,
        )
        return True

    def _on_connection(self) -> None:
        if self._server is None:
            return
        conn = self._server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(lambda: self._on_ready(conn))
        conn.disconnected.connect(conn.deleteLater)

    def _on_ready(self, conn) -> None:
        if _RAISE_MESSAGE.strip() in bytes(conn.readAll()).strip().splitlines():
            if self._attached:
                self.raise_requested.emit()
            else:
                # No window slot yet — buffer it, `attach()` replays it.
                self._pending_raise = True
        conn.disconnectFromServer()

    def attach(self, receiver) -> None:
        """Connect the window's raise slot and replay a buffered raise.

        Called ONCE, right after the window exists. Anything that arrived
        during window construction is delivered here instead of being lost.
        """
        self.raise_requested.connect(receiver)
        self._attached = True
        if self._pending_raise:
            self._pending_raise = False
            receiver()

    def release(self) -> None:
        """Give up ownership (idempotent).

        A guard that never claimed MUST NOT touch the name: removeServer()
        works against a live listener, so a losing process calling release()
        would free the WINNER's name and let the next launch open a
        duplicate window.
        """
        if not self._owned:
            return
        if self._server is not None:
            self._server.close()
            self._server = None
        self._owned = False
        QLocalServer.removeServer(self.name)

    # -- newcomer side -------------------------------------------------
    def request_raise(self, timeout_ms: int = _PROBE_TIMEOUT_MS) -> bool:
        """Ask the owning process to raise its window. True if delivered."""
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(timeout_ms):
            return False
        written = sock.write(_RAISE_MESSAGE)
        sock.flush()
        # Do NOT use waitForBytesWritten() as delivery proof: after a
        # successful flush() it returns False (nothing left pending) even
        # though the owner DID receive the message. Taking that as "no
        # owner" would remove a live owner's socket name and open a
        # duplicate window.
        if sock.bytesToWrite() > 0:
            sock.waitForBytesWritten(timeout_ms)
        delivered = written == len(_RAISE_MESSAGE) and sock.bytesToWrite() == 0
        sock.disconnectFromServer()
        return bool(delivered)
