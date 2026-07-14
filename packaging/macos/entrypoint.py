"""PyInstaller entry point for the Intel macOS application bundle.

The normal project entry point stays ``spar.cli``.  This file only handles
Finder-specific startup concerns: recovering a useful shell PATH, asking for
a project directory, and routing child engine processes back into the frozen
executable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_ENGINE_SWITCH = "--spar-engine"


def _login_shell_path() -> str | None:
    """Best-effort PATH lookup for apps launched outside a terminal."""
    shell = os.environ.get("SHELL", "/bin/zsh")
    try:
        result = subprocess.run(
            [shell, "-lic", "printf %s \"$PATH\""],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value or None


def _bootstrap_macos_path() -> None:
    """Merge terminal and common tool locations into the Finder environment."""
    candidates: list[str] = []
    shell_path = _login_shell_path()
    if shell_path:
        candidates.extend(shell_path.split(os.pathsep))
    candidates.extend(os.environ.get("PATH", "").split(os.pathsep))
    candidates.extend(
        [
            "/usr/local/bin",       # Homebrew on Intel Macs
            "/opt/homebrew/bin",   # harmless when the DMG is tested elsewhere
            str(Path.home() / ".local" / "bin"),
            str(Path.home() / ".npm-global" / "bin"),
        ]
    )

    unique: list[str] = []
    for item in candidates:
        if item and item not in unique:
            unique.append(item)
    os.environ["PATH"] = os.pathsep.join(unique)


def _choose_project_dir() -> str | None:
    from PySide6.QtWidgets import QApplication, QFileDialog

    QApplication.instance() or QApplication(sys.argv[:1])
    selected = QFileDialog.getExistingDirectory(
        None,
        "Wybierz katalog projektu dla Spar",
        str(Path.home()),
        QFileDialog.Option.ShowDirsOnly,
    )
    return selected or None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    args = [arg for arg in args if not arg.startswith("-psn_")]

    _bootstrap_macos_path()

    if args[:1] == [_ENGINE_SWITCH]:
        from spar.cli import main as cli_main

        return cli_main(args[1:])

    from spar.gui.app import main_gui

    if not args:
        selected = _choose_project_dir()
        if selected is None:
            return 0
        args = ["--dir", selected]
    return main_gui(args)


if __name__ == "__main__":
    raise SystemExit(main())
