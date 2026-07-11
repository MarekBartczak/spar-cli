"""Pliki module (ADR 0006 tranche A): project tree + Pygments editor + finder.

Qt-free helpers (lexer pick, fuzzy scorer, file index) live ABOVE the
``if _HAS_QT:`` guard so the module imports on a plain interpreter and their
tests run under a plain ``python3`` (no importorskip) — mirroring
orchestrator.py / rails.py. The Qt layer (FileEditor, FilesView,
FileFinderOverlay, ...) is only defined when PySide6 is importable.
"""
from __future__ import annotations

import os
import time as _time
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

    from PySide6.QtCore import (
        QCoreApplication,
        QDeadlineTimer,
        QDir,
        QEvent,
        QEventLoop,
        QObject,
        QSettings,
        QSortFilterProxyModel,
        QStringListModel,
    )
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QFileSystemModel,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListView,
        QPushButton,
        QSplitter,
        QTabWidget,
        QTreeView,
        QVBoxLayout,
    )

    from spar.gui.runner import RunnerState

    # States in which the tree is being mutated by the engine → read-only
    # editor (ADR 0006 item 4). Everything else (IDLE/DONE/RESUMABLE/ERROR/
    # ABORTED) is editable.
    _READ_ONLY_STATES = frozenset(
        {RunnerState.RUNNING, RunnerState.GATE_PENDING, RunnerState.LOCKED}
    )

    class _TreeFilterProxy(QSortFilterProxyModel):
        """Hides ``.git`` entirely (ADR 0006 item 2). Everything else,
        including hidden dotfiles and ``.spar`` (collapsed), passes."""

        def filterAcceptsRow(self, row, parent):  # noqa: N802 (Qt override)
            src = self.sourceModel()
            idx = src.index(row, 0, parent)
            return src.fileName(idx) != ".git"

    class EditorTab(QWidget):
        """One tab page: a per-tab "changed on disk" banner over a
        FileEditor. Keeps FileEditor a pure QPlainTextEdit subclass."""

        def __init__(self, path, parent=None):
            super().__init__(parent)
            self.path = Path(path)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(0)

            self.disk_banner = QWidget(self)
            self.disk_banner.setObjectName("diskBanner")
            banner_row = QHBoxLayout(self.disk_banner)
            banner_row.setContentsMargins(6, 2, 6, 2)
            label = QLabel("plik zmienił się na dysku", self.disk_banner)
            self.reload_button = QPushButton("Przeładuj", self.disk_banner)
            self.reload_button.clicked.connect(self._on_reload_clicked)
            banner_row.addWidget(label, stretch=1)
            banner_row.addWidget(self.reload_button)
            self.disk_banner.setVisible(False)
            layout.addWidget(self.disk_banner)

            self.editor = FileEditor(self.path, self)
            self.editor.load_from_disk()
            self.editor.disk_conflict.connect(self._show_disk_banner)
            self.editor.disk_reloaded.connect(self._hide_disk_banner)
            layout.addWidget(self.editor, stretch=1)

        def is_dirty(self) -> bool:
            return self.editor.is_dirty()

        def save(self) -> bool:
            ok = self.editor.save()
            if ok:
                self._hide_disk_banner()
            return ok

        def set_read_only(self, ro: bool) -> None:
            self.editor.set_read_only(ro)

        def _show_disk_banner(self) -> None:
            self.disk_banner.setVisible(True)

        def _hide_disk_banner(self) -> None:
            self.disk_banner.setVisible(False)

        def _on_reload_clicked(self) -> None:
            self.editor.reload_from_disk()
            self._hide_disk_banner()

    class FilesView(QWidget):
        """Pliki module: project tree | tabbed Pygments editor (ADR 0006)."""

        def __init__(self, project_dir, parent=None):
            super().__init__(parent)
            self.setObjectName("filesView")
            self.project_dir = Path(project_dir)
            self._read_only = False
            self._settings = QSettings("spar", "gui")
            self._tabs_by_path: dict[str, EditorTab] = {}

            outer = QVBoxLayout(self)
            outer.setContentsMargins(0, 0, 0, 0)
            self.splitter = QSplitter(self)
            self.splitter.setObjectName("filesSplitter")

            # -- tree --
            self.model = QFileSystemModel(self)
            self.model.setFilter(
                QDir.Filter.AllEntries | QDir.Filter.Hidden
                | QDir.Filter.NoDotAndDotDot
            )
            # QFileSystemModel populates ASYNCHRONOUSLY (review #10): its
            # directory listing arrives on a later event loop turn. A plain
            # QSortFilterProxyModel index mapped BEFORE the project dir is
            # listed is unstable — as the source fills in, the proxy rebuilds
            # its mappings and the plain index silently reseats onto a
            # different node (observed: the root slid up to the parent dir)
            # or dangles (use-after-free → segfault while a caller polls
            # ``rowCount(root)``). Block briefly until the project directory
            # has been listed, THEN map the root, so ``tree.rootIndex()`` is
            # correct and stable the moment construction returns.
            self._dir_loaded = False
            self.model.directoryLoaded.connect(self._on_dir_loaded)
            self.model.setRootPath(str(self.project_dir))
            self.proxy = _TreeFilterProxy(self)
            self.proxy.setSourceModel(self.model)
            self._wait_dir_loaded()
            self.tree = QTreeView(self.splitter)
            self.tree.setObjectName("filesTree")
            self.tree.setModel(self.proxy)
            self.tree.setRootIndex(
                self.proxy.mapFromSource(self.model.index(str(self.project_dir)))
            )
            for col in (1, 2, 3):  # size / type / date-modified
                self.tree.hideColumn(col)
            self.tree.setHeaderHidden(True)
            self.tree.doubleClicked.connect(self._on_tree_double_clicked)

            # -- editor side (banner over tabs) --
            right = QWidget(self.splitter)
            right_layout = QVBoxLayout(right)
            right_layout.setContentsMargins(0, 0, 0, 0)
            right_layout.setSpacing(0)
            self.read_only_banner = QLabel("run w toku — tylko podgląd", right)
            self.read_only_banner.setObjectName("filesReadOnlyBanner")
            self.read_only_banner.setVisible(False)
            right_layout.addWidget(self.read_only_banner)
            self.tabs = QTabWidget(right)
            self.tabs.setObjectName("filesTabs")
            self.tabs.setTabsClosable(True)
            self.tabs.tabCloseRequested.connect(self._close_tab)
            right_layout.addWidget(self.tabs, stretch=1)

            self.splitter.addWidget(self.tree)
            self.splitter.addWidget(right)
            self.splitter.setStretchFactor(0, 1)
            self.splitter.setStretchFactor(1, 3)
            outer.addWidget(self.splitter)

            self._restore_split_state()
            self.splitter.splitterMoved.connect(self._save_split_state)

            # Ctrl+S saves the current tab (review #2). Scoped to this
            # widget so it only fires while Pliki has focus. It NO-OPS while
            # the read-only matrix is engaged (review #4) — the read-only
            # banner is the user feedback.
            self._save_shortcut = QShortcut(
                QKeySequence(QKeySequence.StandardKey.Save), self
            )
            # WidgetWithChildren so the chord fires whenever the tree or any
            # editor tab (a descendant) holds focus, without depending on
            # top-level window activation (review #13).
            self._save_shortcut.setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut
            )
            self._save_shortcut.activated.connect(self._save_current)

        # The QShortcut above is the real-display path (it consumes the chord
        # before it reaches any widget). Headless/offscreen — and any host
        # where the top-level window is never activated — never gives the
        # focus widget the shortcut map needs, so the chord is delivered to
        # the editor as a plain key event instead. This filter catches that
        # case and routes the SAME chord to _save_current (review #13). On a
        # real display the shortcut has already eaten the event, so this
        # never double-fires.
        def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
            if (
                event.type() == QEvent.Type.KeyPress
                and event.matches(QKeySequence.StandardKey.Save)
            ):
                self._save_current()
                return True
            return super().eventFilter(obj, event)

        # -- async tree population ----------------------------------------
        def _on_dir_loaded(self, path) -> None:
            if Path(path) == self.project_dir:
                self._dir_loaded = True

        def _wait_dir_loaded(self, timeout_ms: int = 2000) -> None:
            """Pump the event loop until the project directory has been
            listed (or a bounded deadline elapses so a slow/vanished path
            can't hang construction). Needed so the mapped tree root is
            stable before it is handed out (see the tree comment above)."""
            if self._dir_loaded:
                return
            deadline = QDeadlineTimer(timeout_ms)
            app = QCoreApplication.instance()
            while not self._dir_loaded and not deadline.hasExpired():
                if app is not None:
                    app.processEvents(
                        QEventLoop.ProcessEventsFlag.AllEvents, 10
                    )
                else:  # pragma: no cover - a QApplication always exists here
                    break

        # -- opening -------------------------------------------------------
        def _on_tree_double_clicked(self, proxy_index) -> None:
            src = self.proxy.mapToSource(proxy_index)
            if self.model.isDir(src):
                return
            self.open_file(self.model.filePath(src))

        def open_file(self, path) -> None:
            key = str(Path(path))
            existing = self._tabs_by_path.get(key)
            if existing is not None:
                self.tabs.setCurrentWidget(existing)
                return
            tab = EditorTab(path, self.tabs)
            tab.set_read_only(self._read_only)
            tab.editor.installEventFilter(self)  # Ctrl+S bridge (review #13)
            index = self.tabs.addTab(tab, self._tab_label(tab))
            self._tabs_by_path[key] = tab
            tab.editor.document().modificationChanged.connect(
                lambda _mod, t=tab: self._refresh_tab_label(t)
            )
            self.tabs.setCurrentIndex(index)

        # -- saving --------------------------------------------------------
        def _save_current(self) -> bool:
            """Ctrl+S handler: save the active tab. NO-OP while read-only
            (review #4) — the engine owns the tree during a run; the
            read_only_banner is already the visible cue, so we just refuse
            and leave the buffer dirty."""
            if self._read_only:
                self.read_only_banner.setVisible(True)  # ensure the cue is up
                return False
            tab = self.tabs.currentWidget()
            if tab is None:
                return False
            return tab.save()

        def _dirty_prompt_buttons(self):
            """Buttons for an unsaved-changes prompt. While read-only
            (review #4) Save is NOT offered — a write is refused in that
            state, so only Discard/Cancel make sense."""
            if self._read_only:
                return (
                    QMessageBox.StandardButton.Discard
                    | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
            return (
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )

        # -- tab labels ----------------------------------------------------
        def _tab_label(self, tab: EditorTab) -> str:
            marker = "• " if tab.is_dirty() else ""
            lock = " 🔒" if self._read_only else ""
            return f"{marker}{tab.path.name}{lock}"

        def _refresh_tab_label(self, tab: EditorTab) -> None:
            idx = self.tabs.indexOf(tab)
            if idx != -1:
                self.tabs.setTabText(idx, self._tab_label(tab))

        def _refresh_all_labels(self) -> None:
            for i in range(self.tabs.count()):
                self.tabs.setTabText(i, self._tab_label(self.tabs.widget(i)))

        # -- closing -------------------------------------------------------
        def _close_tab(self, index: int) -> None:
            tab = self.tabs.widget(index)
            if tab is None:
                return
            if tab.is_dirty():
                buttons, default = self._dirty_prompt_buttons()  # review #4
                reply = QMessageBox.question(
                    self, "Niezapisane zmiany",
                    f"Plik {tab.path.name} ma niezapisane zmiany. Zapisać?",
                    buttons, default,
                )
                if reply == QMessageBox.StandardButton.Cancel:
                    return
                if reply == QMessageBox.StandardButton.Save and not tab.save():
                    return  # save failed → keep the tab open
                # Discard falls through: the tab (and its buffer) is dropped.
            self.tabs.removeTab(index)
            self._tabs_by_path.pop(str(tab.path), None)
            tab.deleteLater()

        # -- read-only matrix ---------------------------------------------
        def set_state(self, state) -> None:
            self._read_only = state in _READ_ONLY_STATES
            self.read_only_banner.setVisible(self._read_only)
            for tab in self._tabs_by_path.values():
                tab.set_read_only(self._read_only)
            self._refresh_all_labels()

        # -- unsaved guards -----------------------------------------------
        def has_unsaved(self) -> bool:
            return any(t.is_dirty() for t in self._tabs_by_path.values())

        def confirm_discard_if_dirty(self) -> bool:
            if not self.has_unsaved():
                return True
            buttons, default = self._dirty_prompt_buttons()  # review #4
            reply = QMessageBox.question(
                self, "Niezapisane zmiany",
                "W edytorze są niezapisane zmiany. Zapisać przed przełączeniem?",
                buttons, default,
            )
            if reply == QMessageBox.StandardButton.Cancel:
                return False
            if reply == QMessageBox.StandardButton.Save:
                for tab in self._tabs_by_path.values():
                    if tab.is_dirty() and not tab.save():
                        return False
                return True
            # Discard (review #5): returning True is NOT enough — the dirty
            # buffers survive in memory, so the edits reappear and the prompt
            # fires again on the next switch. Actually revert each dirty tab:
            # reload it from disk, or drop the tab if its file vanished.
            for tab in list(self._tabs_by_path.values()):
                if not tab.is_dirty():
                    continue
                if tab.path.exists():
                    tab.editor.reload_from_disk()  # clears the modified flag
                else:
                    self._drop_tab(tab)
            return True

        def _drop_tab(self, tab) -> None:
            idx = self.tabs.indexOf(tab)
            if idx != -1:
                self.tabs.removeTab(idx)
            self._tabs_by_path.pop(str(tab.path), None)
            tab.deleteLater()

        # -- splitter persistence -----------------------------------------
        def _restore_split_state(self) -> None:
            state = self._settings.value("files/tree_split")
            if state is not None:
                self.splitter.restoreState(state)

        def _save_split_state(self, *_args) -> None:
            self._settings.setValue("files/tree_split", self.splitter.saveState())

    _FINDER_STALE_SECONDS = 5.0
    _FINDER_MAX_RESULTS = 200
    _DOUBLE_SHIFT_WINDOW = 0.4  # seconds

    class FileFinderOverlay(QWidget):
        """Frameless double-Shift fuzzy file finder (ADR 0006 item 5)."""

        file_chosen = Signal(str)

        def __init__(self, project_dir, parent=None):
            super().__init__(parent)
            self.project_dir = Path(project_dir)
            self.setObjectName("fileFinder")
            self.setWindowFlags(Qt.WindowType.Popup)
            self._index: list[str] = []
            self._indexed_at = 0.0

            layout = QVBoxLayout(self)
            layout.setContentsMargins(6, 6, 6, 6)
            self.query = QLineEdit(self)
            self.query.setObjectName("finderQuery")
            self.query.setPlaceholderText("Szukaj pliku… (Esc zamyka)")
            self.query.textChanged.connect(self._on_query_changed)
            # review #8: focus lives in the QLineEdit, which CONSUMES Return
            # before the overlay's keyPressEvent sees it. Wire the line
            # edit's own returnPressed so Enter reliably accepts.
            self.query.returnPressed.connect(self._accept_current)
            layout.addWidget(self.query)
            self.list = QListView(self)
            self.list.setObjectName("finderList")
            self._model = QStringListModel(self)
            self.list.setModel(self._model)
            self.list.doubleClicked.connect(lambda _idx: self._accept_current())
            layout.addWidget(self.list)
            self.resize(560, 360)

        def refresh_index(self, force: bool = False) -> None:
            if force or (_time.monotonic() - self._indexed_at) > _FINDER_STALE_SECONDS:
                self._index = build_file_index(self.project_dir)
                self._indexed_at = _time.monotonic()

        def popup(self) -> None:
            self.refresh_index()
            self.query.clear()
            self._on_query_changed("")
            parent = self.parentWidget()
            if parent is not None:
                center = parent.mapToGlobal(parent.rect().center())
                self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)
            self.show()
            self.query.setFocus()

        def _on_query_changed(self, text: str) -> None:
            matches = filter_paths(text, self._index)[:_FINDER_MAX_RESULTS]
            self._model.setStringList(matches)
            if matches:
                self.list.setCurrentIndex(self._model.index(0, 0))

        def _accept_current(self) -> None:
            idx = self.list.currentIndex()
            if not idx.isValid():
                return
            self.file_chosen.emit(idx.data())
            self.close()

        def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
            # Enter is handled by query.returnPressed (review #8); here we
            # only need Escape to dismiss the overlay.
            if event.key() == Qt.Key.Key_Escape:
                self.close()
                return
            super().keyPressEvent(event)

    class DoubleShiftFilter(QObject):
        """Application-level event filter firing on two bare Shift presses
        within 400 ms with no intervening non-Shift key (ADR 0006 item 5).
        ``_now`` is injectable for deterministic tests."""

        triggered = Signal()

        def __init__(self, parent=None):
            super().__init__(parent)
            self._last_shift = None
            self._now = _time.monotonic

        def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
            if event.type() == QEvent.Type.KeyPress:
                if getattr(event, "isAutoRepeat", lambda: False)():
                    return False
                if event.key() == Qt.Key.Key_Shift:
                    # review #7: require a BARE Shift. On the Shift keypress
                    # the only modifier that may legitimately be reported is
                    # ShiftModifier itself; Ctrl/Alt/Meta held alongside must
                    # NOT count (e.g. Ctrl+Shift shortcuts double-tapped).
                    mods = event.modifiers()
                    if mods not in (
                        Qt.KeyboardModifier.NoModifier,
                        Qt.KeyboardModifier.ShiftModifier,
                    ):
                        self._last_shift = None
                        return False
                    now = self._now()
                    if self._last_shift is not None and (now - self._last_shift) <= _DOUBLE_SHIFT_WINDOW:
                        self._last_shift = None
                        self.triggered.emit()
                    else:
                        self._last_shift = now
                else:
                    self._last_shift = None  # any other key breaks the pair
            return False  # never consume the event
