"""Read-only viewers: the Plan (markdown) dialog and the Diff (git) dialog.

Both are opened from the toolbar's ``Plan``/``Diff`` buttons (wired in
``spar/gui/app.py``). Neither touches ``project_dir`` outside of what the
caller hands it: branch names for the diff come FROM the status dict's
``branches`` field (never hard-coded -- a repo may not use ``main`` as its
target) and the artifact path comes from ``status['artifact']``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PySide6.QtGui import QColor, QTextCharFormat
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QPlainTextEdit,
    QTextBrowser,
    QVBoxLayout,
)

from spar.gui.theme import TOKENS

__all__ = ["show_plan_dialog", "show_diff_dialog", "diff_command"]


def _dialog(parent, title: str, widget) -> QDialog:
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    dialog.resize(900, 700)
    layout = QVBoxLayout(dialog)
    layout.addWidget(widget)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, parent=dialog)
    buttons.rejected.connect(dialog.reject)
    buttons.accepted.connect(dialog.accept)
    buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(dialog.accept)
    layout.addWidget(buttons)
    return dialog


def show_plan_dialog(parent, artifact_path: "str | Path | None") -> None:
    """Render ``artifact.md`` as markdown in a read-only dialog.

    A no-op (does nothing visible beyond an empty viewer) when there is no
    artifact yet -- callers gate the toolbar action on its presence, but this
    function stays defensive since the file can vanish between poll and
    click.
    """
    browser = QTextBrowser(parent)
    browser.setObjectName("planViewer")
    browser.setOpenExternalLinks(True)

    text = ""
    if artifact_path:
        path = Path(artifact_path)
        if path.exists():
            text = path.read_text(encoding="utf-8")
    browser.setMarkdown(text)

    dialog = _dialog(parent, "Plan", browser)
    dialog.exec()


def diff_command(project_dir: "str | Path", branches: dict) -> list[str]:
    """Build the ``git diff <target>..<integration>`` argv for ``branches``.

    Branch names come straight from the status dict (``branches['target']``/
    ``branches['integration']``) -- never hard-coded, since a repo's target
    branch need not be ``main``.
    """
    target = branches["target"]
    integration = branches["integration"]
    return ["git", "-C", str(project_dir), "diff", f"{target}..{integration}"]


def _colorize_diff(view: QPlainTextEdit, text: str) -> None:
    """Color +/- lines using the ``ok``/``gate`` theme tokens."""
    view.setPlainText(text)
    doc = view.document()

    added_fmt = QTextCharFormat()
    added_fmt.setForeground(QColor(TOKENS["ok"]))
    removed_fmt = QTextCharFormat()
    removed_fmt.setForeground(QColor(TOKENS["gate"]))

    block = doc.firstBlock()
    cursor = view.textCursor()
    while block.isValid():
        line = block.text()
        fmt = None
        if line.startswith("+") and not line.startswith("+++"):
            fmt = added_fmt
        elif line.startswith("-") and not line.startswith("---"):
            fmt = removed_fmt
        if fmt is not None:
            cursor.setPosition(block.position())
            cursor.setPosition(block.position() + len(line), mode=cursor.MoveMode.KeepAnchor)
            cursor.setCharFormat(fmt)
        block = block.next()


def show_diff_dialog(parent, project_dir: "str | Path", branches: "dict | None") -> None:
    """Run ``git diff <target>..<integration>`` and show it, colored, in a
    monospace read-only viewer.

    A no-op when ``branches`` is absent (debate-only state, no exec branches
    yet) -- callers gate the toolbar action on its presence, defensive here
    for the same race as :func:`show_plan_dialog`.
    """
    if not branches:
        return

    view = QPlainTextEdit(parent)
    view.setObjectName("diffViewer")
    view.setReadOnly(True)
    font = view.font()
    font.setFamily("monospace")
    view.setFont(font)

    try:
        result = subprocess.run(
            diff_command(project_dir, branches),
            capture_output=True,
            text=True,
            check=False,
        )
        text = result.stdout or result.stderr or "(no diff output)"
    except OSError as exc:
        text = f"git diff failed: {exc}"

    _colorize_diff(view, text)

    dialog = _dialog(parent, "Diff", view)
    dialog.exec()
