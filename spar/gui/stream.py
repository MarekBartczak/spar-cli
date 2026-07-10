"""Live stream pane: ``LiveLogTailer`` + ``StreamPane``.

``LiveLogTailer`` is a ``QTimer``-driven incremental reader of a single
``live.log`` file. The path is INJECTED at construction time and is never
derived from ``os.getcwd()`` -- the GUI may be launched with ``--dir`` while
running from an unrelated cwd, so a cwd-relative path would silently follow
the wrong project's log (mirrors the ``project_dir``-scoping rule already
applied to ``SparRunner`` -- see ``spar/gui/runner.py`` reviews #1/#2).

The read loop mirrors ``spar.watch.follow``'s semantics (missing file ->
wait; a partial (no trailing newline) line -> wait for the rest; truncation
(a fresh run recreated the file, shorter) -> reopen from 0) but is
reimplemented here rather than imported, because ``spar.watch`` is a stdlib
module with no Qt dependency and must stay that way (task brief: "do NOT
import Qt into spar/watch"). Unlike ``follow`` (which tails from the END of
the file by default, i.e. only lines appended after the viewer starts), this
tailer always starts from position 0: the GUI pane is usually opened well
after a run has started (e.g. after a gate already went pending) and should
show the whole live history it has, not just what happens to append next.

``StreamPane`` is the read-only ``QPlainTextEdit`` view: per-prefix colors
(``[side ...]`` -- ported from ``spar.watch.colorize``'s regex, colors from
``spar.gui.theme.TOKENS`` rather than watch's ANSI palette), a bold
gate-color line for ``gate '<name>' pending``, client-side filter chips
re-rendered from an in-memory ring buffer, a follow/auto-scroll toggle and a
plain ``QTextDocument.find``-based search box.
"""

from __future__ import annotations

import re
import zlib
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from spar.gui.theme import TOKENS

__all__ = ["LiveLogTailer", "StreamPane"]

# Ported from spar/watch.py (colorize / follow) -- kept in sync by hand since
# that module must stay Qt-free (task brief).
_PREFIX_RE = re.compile(r"^\[([^\]]+)\](.*)$")
_GATE_PENDING_RE = re.compile(r"gate '[^']*' pending")

# Role colors pulled from TOKENS for the deterministic per-prefix hash below
# (never hardcode hex here -- everything must trace back to a TOKENS value).
_ROLE_PALETTE_KEYS = ["claude", "codex", "spar-log", "ok", "warn", "gate"]

_RING_MAX = 20000

_FILTER_ALL = "wszystko"
_FILTER_SPAR = "spar"


def _color_for_prefix(prefix: str) -> str:
    """Deterministic TOKENS color for a ``[<prefix>]`` line.

    The prefix's first token (the side, e.g. ``claude``/``codex``/``A``/``B``)
    is looked up directly in ``TOKENS`` when it names a known side; anything
    else gets a stable hash-based pick from the role palette (mirrors
    ``spar.watch._color_for``'s crc32-mod-palette approach, but restricted to
    TOKENS values instead of an ANSI code list).
    """
    side = prefix.split()[0] if prefix.split() else prefix
    if side in TOKENS:
        return TOKENS[side]
    keys = _ROLE_PALETTE_KEYS
    idx = zlib.crc32(side.encode("utf-8")) % len(keys)
    return TOKENS[keys[idx]]


class LiveLogTailer(QObject):
    """Incremental ``QTimer``-driven reader of an injected ``live.log`` path."""

    lines = Signal(list)  # list[str], one signal per poll with >=1 new lines

    def __init__(
        self,
        log_path: "str | Path",
        parent: QObject | None = None,
        interval_ms: int = 250,
    ) -> None:
        super().__init__(parent)
        # Injected, absolute-or-as-given path -- never derived from cwd
        # (review #9: the pane may run with cwd != project_dir).
        self.log_path = Path(log_path)

        self._fh = None
        self._pos = 0

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.poll)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def poll(self) -> None:
        """One non-blocking read attempt; emits ``lines`` if any completed.

        Mirrors ``spar.watch.follow``'s single-iteration body: open-if-needed,
        drain complete (newline-terminated) lines, detect truncation, and
        return -- never blocks/sleeps (the QTimer provides the polling
        cadence instead of ``follow``'s internal ``time.sleep``).
        """
        if self._fh is None:
            try:
                self._fh = self.log_path.open("r", encoding="utf-8", errors="replace")
            except OSError:
                return
            self._fh.seek(self._pos)

        batch: list[str] = []
        while True:
            line = self._fh.readline()
            if line:
                if line.endswith("\n"):
                    self._pos = self._fh.tell()
                    batch.append(line[:-1])
                    continue
                # Partial line (writer hasn't flushed the newline yet):
                # rewind and wait for the rest next poll.
                self._fh.seek(self._pos)
                break

            # No new data right now -- check for truncation (a fresh run
            # started and .spar/live.log was recreated shorter).
            try:
                size = self.log_path.stat().st_size
            except OSError:
                self._fh.close()
                self._fh = None
                self._pos = 0
                break
            if size < self._pos:
                self._fh.close()
                self._fh = None
                self._pos = 0
            break

        if batch:
            self.lines.emit(batch)


class StreamPane(QWidget):
    """Read-only live transcript view: colors, filter chips, follow, search."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("streamPane")

        self._ring: deque[str] = deque(maxlen=_RING_MAX)
        self._active_filter: tuple[str, str | None] = (_FILTER_ALL, None)
        self._known_sides: list[str] = []
        self._known_tasks: list[str] = []
        self._chip_buttons: dict[tuple[str, str | None], QPushButton] = {}
        self._following = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        controls = QHBoxLayout()
        self.chips_layout = QHBoxLayout()
        controls.addLayout(self.chips_layout)
        controls.addStretch(1)

        self.follow_button = QPushButton("Śledź", self)
        self.follow_button.setObjectName("followButton")
        self.follow_button.setCheckable(True)
        self.follow_button.setChecked(True)
        self.follow_button.toggled.connect(self._on_follow_toggled)
        controls.addWidget(self.follow_button)

        self.jump_button = QPushButton("↓ na żywo", self)
        self.jump_button.setObjectName("jumpToLiveButton")
        self.jump_button.hide()
        self.jump_button.clicked.connect(self._jump_to_live)
        controls.addWidget(self.jump_button)

        self.search_edit = QLineEdit(self)
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText("Szukaj…")
        self.search_edit.returnPressed.connect(self._on_search)
        controls.addWidget(self.search_edit)

        layout.addLayout(controls)

        self.text = QPlainTextEdit(self)
        self.text.setObjectName("streamText")
        self.text.setReadOnly(True)
        self.text.setMaximumBlockCount(_RING_MAX)
        self.text.verticalScrollBar().valueChanged.connect(self._on_scroll)
        layout.addWidget(self.text)

        self._add_chip(_FILTER_ALL, None, _FILTER_ALL)
        self._add_chip(_FILTER_SPAR, None, _FILTER_SPAR)

    # ------------------------------------------------------------------
    # Feeding
    # ------------------------------------------------------------------
    def feed_lines(self, lines: list[str]) -> None:
        """Append raw lines to the ring buffer and render the ones now visible."""
        for line in lines:
            self._ring.append(line)
            self._register_prefix(line)
            if self._line_matches(line):
                self._append_line(line)
        if lines and self._following:
            self._scroll_to_bottom()

    def _register_prefix(self, line: str) -> None:
        match = _PREFIX_RE.match(line)
        if not match:
            return
        parts = match.group(1).split()
        if not parts:
            return
        side = parts[0]
        if side not in self._known_sides:
            self._known_sides.append(side)
            self._add_chip("side", side, side)
        if len(parts) > 1:
            task = parts[1]
            if task not in self._known_tasks:
                self._known_tasks.append(task)
                self._add_chip("task", task, task)

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------
    def set_filter(self, kind: str, value: str | None = None) -> None:
        """Set the active client-side filter and re-render from the ring buffer."""
        self._active_filter = (kind, value)
        self._rerender_all()

    def _line_matches(self, line: str) -> bool:
        kind, value = self._active_filter
        if kind == _FILTER_ALL:
            return True
        if kind == _FILTER_SPAR:
            match = _PREFIX_RE.match(line)
            return match is None
        match = _PREFIX_RE.match(line)
        if not match:
            return False
        parts = match.group(1).split()
        if not parts:
            return False
        if kind == "side":
            return parts[0] == value
        if kind == "task":
            return len(parts) > 1 and parts[1] == value
        return True

    def _rerender_all(self) -> None:
        self.text.clear()
        for line in self._ring:
            if self._line_matches(line):
                self._append_line(line)
        if self._following:
            self._scroll_to_bottom()

    # ------------------------------------------------------------------
    # Chips
    # ------------------------------------------------------------------
    def _add_chip(self, kind: str, value: str | None, label: str) -> None:
        key = (kind, value)
        if key in self._chip_buttons:
            return
        button = QPushButton(label, self)
        button.setObjectName(f"filterChip_{kind}_{value or ''}")
        button.setCheckable(True)
        button.setChecked(kind == _FILTER_ALL)
        button.clicked.connect(lambda _checked=False, k=kind, v=value: self._on_chip_clicked(k, v))
        self._chip_buttons[key] = button
        self.chips_layout.addWidget(button)

    def _on_chip_clicked(self, kind: str, value: str | None) -> None:
        for key, button in self._chip_buttons.items():
            button.setChecked(key == (kind, value))
        self.set_filter(kind, value)

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------
    def _append_line(self, line: str) -> None:
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)

        default_fmt = QTextCharFormat()
        default_fmt.setForeground(QColor(TOKENS["text"]))

        if _GATE_PENDING_RE.search(line):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(TOKENS["gate"]))
            fmt.setFontWeight(QFont.Weight.Bold)
            cursor.insertText(line, fmt)
        else:
            match = _PREFIX_RE.match(line)
            if match:
                prefix, rest = match.groups()
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(_color_for_prefix(prefix)))
                cursor.insertText(f"[{prefix}]", fmt)
                cursor.insertText(rest, default_fmt)
            elif line.startswith("spar exec:") or line.startswith("spar:"):
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(TOKENS["spar-log"]))
                fmt.setFontWeight(QFont.Weight.Bold)
                cursor.insertText(line, fmt)
            else:
                cursor.insertText(line, default_fmt)
        cursor.insertBlock()

    # ------------------------------------------------------------------
    # Follow / auto-scroll
    # ------------------------------------------------------------------
    def _scroll_to_bottom(self) -> None:
        bar = self.text.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_scroll(self, value: int) -> None:
        bar = self.text.verticalScrollBar()
        at_bottom = value >= bar.maximum() - 2
        if not at_bottom and self._following:
            self._following = False
            self.follow_button.setChecked(False)
            self.jump_button.show()

    def _on_follow_toggled(self, checked: bool) -> None:
        self._following = checked
        if checked:
            self.jump_button.hide()
            self._scroll_to_bottom()

    def _jump_to_live(self) -> None:
        self._following = True
        self.follow_button.setChecked(True)
        self.jump_button.hide()
        self._scroll_to_bottom()

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------
    def _on_search(self) -> None:
        query = self.search_edit.text()
        if not query:
            return
        found = self.text.find(query)
        if not found:
            # Wrap around: jump to the start and try once more.
            cursor = self.text.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            self.text.setTextCursor(cursor)
            self.text.find(query)
