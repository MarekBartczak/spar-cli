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
    # NOTE (review #1): QFileSystemWatcher lives in QtCore, NOT QtWidgets.
    # Qt is imported here too (used by _paint_line_numbers); do not add a
    # second QtCore import line below.
    from PySide6.QtCore import (
        QFileSystemWatcher,
        QRect,
        QSize,
        Qt,
        QTimer,
        Signal,
    )
    from PySide6.QtGui import (
        QColor,
        QFont,
        QPainter,
        QSyntaxHighlighter,
        QTextCharFormat,
        QTextFormat,
    )
    from PySide6.QtWidgets import (
        QMessageBox,
        QPlainTextEdit,
        QTextEdit,
        QWidget,
    )
    from pygments import lex
    from pygments.token import Token

    from spar.gui.theme import TOKENS

    # review #6: watcher re-arm poll while a path is momentarily absent
    # during an atomic replace. Small interval, bounded count (~0.5 s total)
    # so a genuine deletion stops retrying instead of spinning forever.
    _WATCH_REARM_INTERVAL_MS = 25
    _WATCH_REARM_MAX_RETRIES = 20

    def _build_token_formats() -> dict:
        """Map Pygments token types to QTextCharFormat, coloured from the
        app's dark TOKENS palette. Lookup walks a token's ``.parent`` chain
        (pygments _TokenType exposes ``.parent``) so subtypes inherit."""
        def fmt(hex_color: str, *, italic: bool = False, bold: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(hex_color))
            if italic:
                f.setFontItalic(True)
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            return f

        t = TOKENS
        return {
            Token.Keyword: fmt(t["claude"], bold=True),
            Token.Name.Builtin: fmt(t["claude"]),
            Token.Name.Function: fmt(t["spar-log"]),
            Token.Name.Class: fmt(t["spar-log"], bold=True),
            Token.Name.Decorator: fmt(t["spar-log"]),
            Token.String: fmt(t["ok"]),
            Token.Number: fmt(t["codex"]),
            Token.Comment: fmt(t["muted"], italic=True),
            Token.Operator: fmt(t["text"]),
        }

    class PygmentsHighlighter(QSyntaxHighlighter):
        """Per-block Pygments highlighter. Lexer picked by filename; the
        per-block lexing is a deliberate magnifier-grade approximation
        (multiline constructs may not span blocks perfectly) — ADR 0006's
        "no LSP, no IDE" boundary."""

        _FORMATS = None  # built lazily (needs a QApplication for QTextCharFormat)

        def __init__(self, document, lexer):
            super().__init__(document)
            self._lexer = lexer
            if PygmentsHighlighter._FORMATS is None:
                PygmentsHighlighter._FORMATS = _build_token_formats()

        def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt override)
            pos = 0
            for token, value in lex(text, self._lexer):
                length = len(value)
                fmt = self._format_for(token)
                if fmt is not None:
                    self.setFormat(pos, length, fmt)
                pos += length

        def _format_for(self, token):
            formats = PygmentsHighlighter._FORMATS
            while token is not None:
                if token in formats:
                    return formats[token]
                token = token.parent
            return None

    class _LineNumberArea(QWidget):
        """Gutter widget delegating paint/size back to its editor."""

        def __init__(self, editor: "FileEditor"):
            super().__init__(editor)
            self._editor = editor

        def sizeHint(self) -> QSize:  # noqa: N802 (Qt override)
            return QSize(self._editor._line_number_area_width(), 0)

        def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
            self._editor._paint_line_numbers(event)

    class FileEditor(QPlainTextEdit):
        """Magnifier/screwdriver editor: gutter + current-line highlight +
        Pygments highlighter + atomic-enough save + disk watcher. NOT an IDE
        (ADR 0006)."""

        disk_reloaded = Signal()   # clean auto-reload happened
        disk_conflict = Signal()   # disk changed while locally modified

        def __init__(self, path: "str | Path", parent: "QWidget | None" = None):
            super().__init__(parent)
            self.path = Path(path)
            self.setObjectName("fileEditor")
            self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            mono = QFont("Monospace")
            mono.setStyleHint(QFont.StyleHint.TypeWriter)
            self.setFont(mono)

            self._gutter = _LineNumberArea(self)
            self.blockCountChanged.connect(self._update_gutter_width)
            self.updateRequest.connect(self._update_gutter_area)
            self.cursorPositionChanged.connect(self._highlight_current_line)
            self._update_gutter_width(0)
            self._highlight_current_line()

            self._highlighter = PygmentsHighlighter(
                self.document(), pick_lexer(self.path.name)
            )

            # Watch the file so an engine write during a read-only run is
            # noticed (ADR 0006 item 4). Always-on is harmless when
            # editable (still no silent clobber of local edits).
            self._watcher = QFileSystemWatcher(self)
            self._watcher.fileChanged.connect(self._on_file_changed)
            # review #6: bounded re-arm retries while the path is briefly
            # absent during an atomic replace (temp write + os.replace).
            self._rearm_retries = 0

        # -- content -------------------------------------------------------
        def load_from_disk(self) -> None:
            text = self.path.read_text(encoding="utf-8", errors="replace")
            self.setPlainText(text)
            self.document().setModified(False)
            if str(self.path) not in self._watcher.files():
                self._watcher.addPath(str(self.path))

        def is_dirty(self) -> bool:
            return self.document().isModified()

        def save(self) -> bool:
            # Read-only matrix enforcement (review #4): a buffer dirtied
            # BEFORE the run started can still reach save() during
            # RUNNING/GATE/LOCKED (via Ctrl+S or a close prompt). Refuse the
            # write outright — the tree belongs to the engine while a run is
            # live. FilesView surfaces the read-only banner; no dialog here.
            if self.isReadOnly():
                return False
            try:
                self.path.write_text(self.toPlainText(), encoding="utf-8")
            except OSError as exc:
                QMessageBox.critical(
                    self, "Nie udało się zapisać",
                    f"Zapis pliku nie powiódł się:\n{self.path}\n\n{exc}",
                )
                return False
            self.document().setModified(False)
            # A successful write re-arms the watcher (some platforms drop the
            # path after our own write).
            if str(self.path) not in self._watcher.files() and self.path.exists():
                self._watcher.addPath(str(self.path))
            return True

        def set_read_only(self, ro: bool) -> None:
            self.setReadOnly(ro)

        def reload_from_disk(self) -> None:
            """Force reload discarding local edits, preserving scroll."""
            self._reload_preserving_scroll()

        def _reload_preserving_scroll(self) -> None:
            bar = self.verticalScrollBar()
            pos = bar.value()
            text = self.path.read_text(encoding="utf-8", errors="replace")
            self.setPlainText(text)
            self.document().setModified(False)
            bar.setValue(min(pos, bar.maximum()))

        def _on_file_changed(self, _path: str = "") -> None:
            # Robustness (review #6): QFileSystemWatcher DROPS the watch
            # after a rename/removal. An atomic replace (temp write +
            # os.replace) makes the path momentarily absent, so a naive
            # "return if missing" would never re-arm and would miss the
            # recreated file. When absent, poll-retry briefly via
            # QTimer.singleShot until the rename lands (or give up), then
            # re-add the path and reload.
            if not self.path.exists():
                if self._rearm_retries < _WATCH_REARM_MAX_RETRIES:
                    self._rearm_retries += 1
                    QTimer.singleShot(
                        _WATCH_REARM_INTERVAL_MS, self._on_file_changed
                    )
                return
            self._rearm_retries = 0
            if str(self.path) not in self._watcher.files():
                self._watcher.addPath(str(self.path))
            if self.is_dirty():
                # Local edits present: never clobber — warn instead.
                self.disk_conflict.emit()
                return
            self._reload_preserving_scroll()
            self.disk_reloaded.emit()

        # -- gutter --------------------------------------------------------
        def _line_number_area_width(self) -> int:
            digits = max(2, len(str(max(1, self.blockCount()))))
            return 12 + self.fontMetrics().horizontalAdvance("9") * digits

        def _update_gutter_width(self, _count: int) -> None:
            self.setViewportMargins(self._line_number_area_width(), 0, 0, 0)

        def _update_gutter_area(self, rect, dy: int) -> None:
            if dy:
                self._gutter.scroll(0, dy)
            else:
                self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
            if rect.contains(self.viewport().rect()):
                self._update_gutter_width(0)

        def resizeEvent(self, event) -> None:  # noqa: N802 (Qt override)
            super().resizeEvent(event)
            cr = self.contentsRect()
            self._gutter.setGeometry(
                QRect(cr.left(), cr.top(), self._line_number_area_width(), cr.height())
            )

        def _paint_line_numbers(self, event) -> None:
            painter = QPainter(self._gutter)
            painter.fillRect(event.rect(), QColor(TOKENS["panel"]))
            block = self.firstVisibleBlock()
            number = block.blockNumber()
            top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
            bottom = top + self.blockBoundingRect(block).height()
            painter.setPen(QColor(TOKENS["muted"]))
            while block.isValid() and top <= event.rect().bottom():
                if block.isVisible() and bottom >= event.rect().top():
                    painter.drawText(
                        0, int(top), self._gutter.width() - 6,
                        self.fontMetrics().height(),
                        int(Qt.AlignmentFlag.AlignRight), str(number + 1),
                    )
                block = block.next()
                top = bottom
                bottom = top + self.blockBoundingRect(block).height()
                number += 1
            painter.end()

        def _highlight_current_line(self) -> None:
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(QColor(TOKENS["panel-alt"]))
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            self.setExtraSelections([selection])
