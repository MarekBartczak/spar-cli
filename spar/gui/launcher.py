"""``spar-gui``: desktop/terminal entry point for the gui.

``spar gui --dir PATH`` stays the canonical CLI form. This launcher exists for
the two ways a DESKTOP user starts the app, where a bare ``--dir`` is not
enough:

* from a terminal, ``spar-gui`` (optionally with a path) — the current
  directory is the project, exactly like ``spar gui``;
* from the Ubuntu application menu, where the process starts in ``/`` or
  ``$HOME`` and there is no meaningful current directory. The ``.desktop``
  entry therefore passes ``--pick``, which asks for the project directory
  (starting at the most recently opened one). A file manager that appends a
  folder — ``Exec=spar-gui --pick %f`` — still wins over the dialog.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from spar.gui.app import main_gui
from spar.gui.instances import recent_projects

_PICK_FLAG = "--pick"
_FOREGROUND_FLAG = "--foreground"
_HELP_FLAGS = ("-h", "--help")
_FLAGS = (_PICK_FLAG, _FOREGROUND_FLAG)
# Marker the detached child carries, so it runs the window instead of
# forking again forever.
_DETACHED_ENV = "SPAR_GUI_DETACHED"

_USAGE = """usage: spar-gui [PATH | --dir PATH] [--pick]

Launch the spar gui on one project (one window = one project = one process).

  spar-gui                 the current directory is the project
  spar-gui PATH            open PATH (a file resolves to its directory)
  spar-gui --dir PATH      same, for parity with `spar gui --dir PATH`
  spar-gui --pick          ask for the project directory (used by the
                           desktop launcher, which has no useful cwd)
  spar-gui --foreground    keep the process attached to this terminal
                           (default is to detach, like `code`)

A second launch on a project that is already open raises its window.
Qt output from a detached window goes to ~/.cache/spar/gui.log."""


def parse_target(argv: list[str]) -> "tuple[Path | None, bool]":
    """Resolve ``argv`` to ``(project_dir, needs_pick)``.

    A concrete, existing directory always wins over ``--pick``; a file
    argument resolves to its parent (a file manager can hand over a file
    inside the project); a path that does not exist falls back to the picker
    rather than opening a window on nothing.
    """
    args = [a for a in argv if a not in _FLAGS]
    wants_pick = _PICK_FLAG in argv

    raw: str | None = None
    if args[:1] == ["--dir"] and len(args) > 1:
        raw = args[1]
    elif args and not args[0].startswith("-"):
        raw = args[0]

    if raw is not None:
        path = Path(raw).expanduser()
        if path.is_dir():
            return path.resolve(), False
        if path.is_file():
            return path.resolve().parent, False
        return None, True  # named something that isn't there → ask

    if wants_pick:
        return None, True
    return Path.cwd().resolve(), False


def picker_start_dir() -> str:
    """Directory the picker opens in: the last project, else ``$HOME``."""
    recent = recent_projects()
    return recent[0] if recent else str(Path.home())


def ask_for_project_dir() -> "str | None":
    """Ask for the project directory. ``None`` when the user cancels."""
    from PySide6.QtWidgets import QApplication, QFileDialog

    QApplication.instance() or QApplication(sys.argv[:1])
    selected = QFileDialog.getExistingDirectory(
        None,
        "Wybierz katalog projektu dla spar",
        picker_start_dir(),
        QFileDialog.Option.ShowDirsOnly,
    )
    return selected or None


def should_detach(argv: list[str], env: "dict[str, str]") -> bool:
    """True when this invocation must hand the window to a new session.

    A GUI started from a terminal has to give the prompt back immediately
    (what `code` does). The re-launched child carries ``SPAR_GUI_DETACHED``
    so it runs the window instead of forking again.
    """
    if env.get(_DETACHED_ENV):
        return False
    return _FOREGROUND_FLAG not in argv


def detached_command(argv: list[str]) -> list[str]:
    """argv for the child: this module, same arguments."""
    return [sys.executable, "-m", "spar.gui.launcher", *argv]


def _log_path() -> Path:
    cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    directory = Path(cache) / "spar"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "gui.log"


def spawn_detached(argv: list[str]) -> bool:
    """Start the window in its own session. False if that failed.

    Qt warnings and tracebacks would vanish with the terminal, so they are
    appended to ``~/.cache/spar/gui.log`` instead of /dev/null.
    """
    env = dict(os.environ)
    env[_DETACHED_ENV] = "1"
    try:
        log = open(_log_path(), "ab", buffering=0)
    except OSError:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen(
            detached_command(argv),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,   # own session+pgid: survives the shell
            close_fds=True,
        )
        return True
    except OSError as exc:
        print(f"spar-gui: could not detach ({exc}); running here", file=sys.stderr)
        return False
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def main(argv: "list[str] | None" = None) -> int:
    """Entry point for the ``spar-gui`` console script."""
    args = list(sys.argv[1:] if argv is None else argv)
    if any(a in _HELP_FLAGS for a in args):
        print(_USAGE)
        return 0
    if should_detach(args, dict(os.environ)) and spawn_detached(args):
        return 0
    target, needs_pick = parse_target(args)
    if needs_pick:
        selected = ask_for_project_dir()
        if selected is None:
            return 0
        return main_gui(["--dir", selected])
    return main_gui(["--dir", str(target)])


if __name__ == "__main__":  # pragma: no cover - console-script shim
    raise SystemExit(main())
