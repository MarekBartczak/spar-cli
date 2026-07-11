"""Pliki module (ADR 0006 tranche A): project tree + Pygments editor + finder.

Qt-free helpers (lexer pick, fuzzy scorer, file index) live ABOVE the
``if _HAS_QT:`` guard so the module imports on a plain interpreter and their
tests run under a plain ``python3`` (no importorskip) — mirroring
orchestrator.py / rails.py. The Qt layer (FileEditor, FilesView,
FileFinderOverlay, ...) is only defined when PySide6 is importable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time as _time
import weakref
from dataclasses import dataclass
from pathlib import Path

from pygments.lexers import get_lexer_for_filename
from pygments.lexers.special import TextLexer
from pygments.util import ClassNotFound

__all__ = [
    "pick_lexer",
    "fuzzy_score",
    "filter_paths",
    "build_file_index",
    "SearchSpec",
    "SearchMatch",
    "compile_search_pattern",
    "search_text",
    "_utf16_offset",
    "passes_search_guards",
    "search_file",
    "search_paths",
    "replace_in_text",
    "_atomic_write_bytes",
    "_RipgrepParseError",
    "ripgrep_available",
    "is_rg_compatible",
    "build_ripgrep_argv",
    "parse_ripgrep_stream",
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


_SEARCH_MAX_BYTES = 2 * 1024 * 1024
_SEARCH_BINARY_SNIFF = 8192


@dataclass(frozen=True)
class SearchSpec:
    """One find-in-files query with its mode toggles. Pure/hashable."""
    query: str
    regex: bool = False
    case_sensitive: bool = False
    whole_word: bool = False


@dataclass(frozen=True)
class SearchMatch:
    """One match: project-relative POSIX path, 1-based line, full line
    text, and the CHARACTER span [start, end) of the match in that line."""
    path: str
    line: int
    text: str
    start: int
    end: int


def compile_search_pattern(spec: "SearchSpec") -> "re.Pattern[str]":
    """Compile *spec* into a regex. Literal queries are re.escape'd; a
    whole-word query is wrapped in ``\\b(?:...)\\b``; case-insensitive
    unless spec.case_sensitive. Raises ``re.error`` on an invalid regex —
    the caller validates and disables search."""
    flags = 0 if spec.case_sensitive else re.IGNORECASE
    body = spec.query if spec.regex else re.escape(spec.query)
    if spec.whole_word:
        body = rf"\b(?:{body})\b"
    return re.compile(body, flags)


def search_text(
    rel: str, text: str, pattern, limit: "int | None" = None
) -> "list[SearchMatch]":
    """Scan already-decoded *text* line by line. Zero-length matches are
    skipped so an accidental empty pattern yields nothing. review #37:
    *limit* stops the scan as soon as that many matches are collected —
    never materialize a pathological file's full match list. review #39:
    a non-positive *limit* returns [] immediately (it must not behave as
    unbounded). Pure."""
    if limit is not None and limit <= 0:
        return []
    out: list[SearchMatch] = []
    for lineno, line in enumerate(text.split("\n"), start=1):
        for m in pattern.finditer(line):
            if m.start() == m.end():
                continue
            out.append(SearchMatch(rel, lineno, line, m.start(), m.end()))
            if limit is not None and len(out) == limit:
                return out
    return out


def _utf16_offset(text: str, char_index: int) -> int:
    """Map a Python code-point offset into *text* to a UTF-16 code-unit
    offset (what QTextCursor.setPosition counts). Non-BMP chars occupy two
    UTF-16 units, so the two indices diverge without this (review #8)."""
    return len(text[:char_index].encode("utf-16-le")) // 2


def passes_search_guards(root, rel: str) -> bool:
    """True when ``root/rel`` passes the python engine's file guards:
    size <= 2 MB and no NUL byte in the first 8 KB. Any OSError => False.
    review #19: this is the SINGLE guard predicate — search_file uses it
    AND the ripgrep path prefilters its file list with it, so rg never
    sees a file the python reference would skip (rg's own binary
    detection differs from the NUL-in-first-8KB rule)."""
    p = Path(root) / rel
    try:
        if p.stat().st_size > _SEARCH_MAX_BYTES:
            return False
        with p.open("rb") as fh:
            sniff = fh.read(_SEARCH_BINARY_SNIFF)
    except OSError:
        return False
    return b"\x00" not in sniff


def search_file(
    root, rel: str, pattern, limit: "int | None" = None
) -> "list[SearchMatch]":
    """Read ``root/rel`` and scan it. Applies passes_search_guards (size +
    binary, review #19); decodes utf-8 errors='replace'. Any OSError ⇒ no
    matches. review #37: *limit* is forwarded to search_text so scanning
    stops at the cap (caller infers truncation via ``len == limit``).
    Touches the filesystem, no Qt."""
    if not passes_search_guards(root, rel):
        return []
    try:
        data = (Path(root) / rel).read_bytes()
    except OSError:
        return []
    return search_text(
        rel, data.decode("utf-8", errors="replace"), pattern, limit=limit
    )


def _stat_fingerprint(path):
    """``(st_mtime_ns, st_size)`` of *path*, or ``None`` when unreadable.
    reviews #23/#25: captured inside the scan itself — the python path
    stats each file BEFORE reading it; the rg path snapshots the WHOLE
    batch BEFORE launching rg (pre-launch dict rel → fingerprint) — so a
    file modified between the scan's read and the GUI row creation keeps
    its SCAN-time fingerprint and replace refuses it ("plik zmienił się")."""
    try:
        st = Path(path).stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def search_paths(root, rel_paths, pattern) -> "list[SearchMatch]":
    """Reference python content search over *rel_paths*, sorted by
    (path, line, start). This is the shape ripgrep must reproduce."""
    out: list[SearchMatch] = []
    for rel in rel_paths:
        out.extend(search_file(root, rel, pattern))
    out.sort(key=lambda m: (m.path, m.line, m.start))
    return out


def replace_in_text(text: str, pattern, replacement: str, *, regex: bool):
    """Return ``(new_text, count)``. Mirrors search_text's match semantics
    exactly. review #40: scans per-LINE (``text.split("\\n")``) like
    search_text — a whole-file finditer would let ``foo\\s+bar`` replace
    across ``foo\\nbar`` though the search never displayed it. review #38:
    within each line, replaces ONLY non-zero-length matches — the same
    skip search_text applies — so a pattern like ``a*`` never edits the
    zero-width positions the results tree never displayed. Manual
    per-line finditer splice (no bare ``pattern.subn``): regex mode
    expands backrefs via ``m.expand(replacement)`` (may raise ``re.error``
    on a bad replacement — the caller catches); literal mode splices
    *replacement* verbatim (``\\1`` is not a backref). *count* is the
    total of per-line replaced non-zero matches."""
    new_lines: list[str] = []
    count = 0
    for line in text.split("\n"):
        pieces: list[str] = []
        pos = 0
        for m in pattern.finditer(line):
            if m.start() == m.end():
                continue
            pieces.append(line[pos:m.start()])
            pieces.append(m.expand(replacement) if regex else replacement)
            pos = m.end()
            count += 1
        pieces.append(line[pos:])
        new_lines.append("".join(pieces))
    return "\n".join(new_lines), count


def _atomic_write_bytes(path, data: bytes) -> None:
    """Write *data* to *path* atomically: a temp file in the same directory
    then ``os.replace`` (same-filesystem rename). Preserves exact bytes —
    no newline translation, no encoding round-trip (review #6). review #32:
    the temp file comes from ``tempfile.mkstemp`` (unique random name,
    O_EXCL-created) — the old predictable sibling ``<file>.spar-tmp`` would
    have OVERWRITTEN a legitimate user file of that exact name and then
    deleted it (unlink in the error path) or renamed it away; mkstemp can
    never collide with an existing file. Because ``os.replace`` swaps in a
    fresh inode, the temp fd is ``fchmod``'d to the ORIGINAL file's
    permission bits BEFORE the rename so executable bits (e.g. a 0o755
    script) survive the replace (review #17; mkstemp's default is 0o600,
    so without this even plain 0o644 files would come out unreadable to
    the group). review #20: *path* is resolved first — os.replace on a
    symlink path would replace the LINK itself with a regular file, so the
    dance always targets the real file (the caller has already done the
    project-root escape check on the resolved path)."""
    path = Path(path).resolve()  # review #20: write the TARGET, not the link
    # review #32: unique + exclusive; keep the .spar-tmp suffix so humans
    # (and the leftover-cleanup test) can still recognise strays.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".spar-tmp"
    )
    tmp = Path(tmp_name)
    try:
        try:
            orig_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            orig_mode = None
        with os.fdopen(fd, "wb") as fh:  # review #32: write via the fd
            if orig_mode is not None:
                # review #17: preserve exec/perm bits (fchmod while the
                # fd is guaranteed open — no window for an fd leak)
                os.fchmod(fd, orig_mode)
            fh.write(data)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


_SEARCH_MAX_RESULTS = 5000  # cap enforced in BOTH engines (review #33)
_RIPGREP_BATCH = 2000  # files per rg invocation (secondary bound)
_RIPGREP_ARGV_BUDGET = 128 * 1024  # review #34: primary argv BYTE bound


class _RipgrepParseError(Exception):
    """rg reported a non-UTF-8 ("bytes") member — the accelerator cannot
    reproduce the python reference shape, so the worker falls back to the
    python scan (review #4)."""


def ripgrep_available() -> bool:
    """True when the ``rg`` binary is on PATH (opportunistic accelerator)."""
    return shutil.which("rg") is not None


def is_rg_compatible(spec: "SearchSpec") -> bool:
    """True only for specs whose rg semantics provably match the python
    reference: LITERAL, case-SENSITIVE, non-whole-word (review #19).
    rg's ``-i`` unicode case-folding and ``-w`` word boundaries diverge
    from python ``re`` (as do the regex dialects), so every other spec
    runs the python path. The ONE place this predicate is encoded."""
    return (not spec.regex) and spec.case_sensitive and not spec.whole_word


def build_ripgrep_argv(root, spec: "SearchSpec", files) -> "list[str]":
    """Argv for ``rg --json`` over an EXPLICIT list of *files* (project-
    relative paths from build_file_index), resolved against *root*. Only
    ever called for an ``is_rg_compatible`` spec (review #19), so the
    flag set is fixed: always ``-F``, never ``-i``/``-w``.

    review #13: handing rg the exact file set the python reference scans —
    rather than a directory root — guarantees the two engines never diverge
    on filesystem semantics (symlinked files, .gitignore, hidden files,
    skip-dirs). The index already applied every skip rule, so no
    ``--no-ignore``/``--hidden``/``-g`` flags are needed. review #19: the
    caller prefilters *files* with ``passes_search_guards`` (size + binary),
    so rg never sees a file python would skip; ``--max-filesize`` stays as
    defence-in-depth. review #41: ``--text`` because the guard only checks
    the FIRST 8KB for NUL — a file whose first NUL falls after that window
    is scanned in full by the python engine, while rg without ``--text``
    stops at the NUL and loses any later match. The caller batches *files*
    via ``_rg_batches`` to respect ARG_MAX."""
    root = Path(root)
    argv = ["rg", "--json", "--no-messages",
            "--max-filesize", str(_SEARCH_MAX_BYTES), "-F", "--text"]
    argv += ["-e", spec.query, "--"]
    argv += [str(root / rel) for rel in files]
    return argv


def _rg_batches(root, files):
    """Split *files* into rg argv batches (review #34): primary bound is
    an estimated argv byte budget (capped at _RIPGREP_ARGV_BUDGET ≈
    128 KB — far below Linux's ~2 MB ARG_MAX, leaving room for the fixed
    flags and the environment), secondary bound _RIPGREP_BATCH files.
    review #36: the budget counts what rg's argv ACTUALLY carries —
    build_ripgrep_argv passes ``str(root / rel)``, so each entry is
    estimated as ``len(os.fsencode(str(root / rel))) + 1`` (NUL); budgeting
    the bare relatives omitted the absolute prefix (a deep *root* times
    thousands of files could silently violate the bound). Best-effort: a
    SINGLE pathological path over the budget still ships alone, and an
    E2BIG at exec surfaces as OSError from Popen, which the caller
    already turns into the python-reference fallback (review #4)."""
    root = Path(root)
    batch: list = []
    size = 0
    for rel in files:
        n = len(os.fsencode(str(root / rel))) + 1
        if batch and (
            size + n > _RIPGREP_ARGV_BUDGET or len(batch) >= _RIPGREP_BATCH
        ):
            yield batch
            batch, size = [], 0
        batch.append(rel)
        size += n
    if batch:
        yield batch


def parse_ripgrep_stream(lines, root):
    """Parse ``rg --json`` stdout *lines*, yielding SearchMatch with the
    SAME shape as search_paths. rg reports byte offsets into each line;
    remap them to CHARACTER offsets so spans match the python reference."""
    root = Path(root)
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        # Non-UTF-8 content → rg emits a "bytes" member instead of "text".
        # We cannot reproduce the python reference shape; signal fallback.
        if "text" not in data.get("path", {}) or "text" not in data.get("lines", {}):
            raise _RipgrepParseError("non-UTF-8 rg match (bytes member)")
        abs_path = Path(data["path"]["text"])
        try:
            rel = abs_path.relative_to(root).as_posix()
        except ValueError:
            rel = abs_path.as_posix()
        line_no = data["line_number"]
        line_text = data["lines"]["text"]
        if line_text.endswith("\n"):
            line_text = line_text[:-1]
        line_bytes = line_text.encode("utf-8", errors="replace")
        for sm in data.get("submatches", []):
            start = len(line_bytes[: sm["start"]].decode("utf-8", errors="replace"))
            end = len(line_bytes[: sm["end"]].decode("utf-8", errors="replace"))
            yield SearchMatch(rel, line_no, line_text, start, end)


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
        QTextCursor,
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

            # Merged ExtraSelections state (review #10): current-line band
            # kept separately from find-match highlights; both applied in one
            # setExtraSelections call, current-line FIRST, matches AFTER.
            self._current_line_selection: "list[QTextEdit.ExtraSelection]" = []
            self._match_selections: "list[QTextEdit.ExtraSelection]" = []

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

        def _apply_extra_selections(self) -> None:
            # review #10: current-line FIRST, matches AFTER, so a match on
            # the active line paints on top of the full-width current line.
            self.setExtraSelections(
                self._current_line_selection + self._match_selections
            )

        def _highlight_current_line(self) -> None:
            selection = QTextEdit.ExtraSelection()
            selection.format.setBackground(QColor(TOKENS["panel-alt"]))
            selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            self._current_line_selection = [selection]
            self._apply_extra_selections()

        def set_match_selections(self, spans: "list[tuple[int, int]]") -> None:
            """Highlight every (char_start, char_end) span with the warn
            colour, merged with the current-line highlight. Char offsets are
            mapped to UTF-16 positions so non-BMP text selects correctly
            (review #8)."""
            text = self.toPlainText()
            sels = []
            for start, end in spans:
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(QColor(TOKENS["warn"]))
                cur = self.textCursor()
                cur.setPosition(_utf16_offset(text, start))
                cur.setPosition(
                    _utf16_offset(text, end), QTextCursor.MoveMode.KeepAnchor
                )
                sel.cursor = cur
                sels.append(sel)
            self._match_selections = sels
            self._apply_extra_selections()

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
        QThread,
        Slot,
    )
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QDialog,
        QFileSystemModel,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListView,
        QPushButton,
        QSplitter,
        QTabWidget,
        QToolButton,
        QTreeView,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
    )

    _SEARCH_ABANDONED_THREADS: "set[QThread]" = set()

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

    class EditorFindBar(QWidget):
        """In-editor Ctrl+F find/replace bar (ADR 0006 tranche B)."""

        def __init__(self, editor, parent=None):
            super().__init__(parent)
            self.setObjectName("editorFindBar")
            self._editor = editor
            row = QHBoxLayout(self)
            row.setContentsMargins(6, 2, 6, 2)
            self.find_field = QLineEdit(self)
            self.find_field.setObjectName("findField")
            self.find_field.setPlaceholderText("Znajdź")
            self.find_field.textChanged.connect(self._on_find_text_changed)
            self.find_field.returnPressed.connect(self.find_next)
            row.addWidget(self.find_field, stretch=1)
            self.case_toggle = QToolButton(self)
            self.case_toggle.setObjectName("searchToggle")
            self.case_toggle.setText("Aa")
            self.case_toggle.setCheckable(True)
            self.case_toggle.clicked.connect(self._rehighlight)
            row.addWidget(self.case_toggle)
            self.prev_button = QPushButton("◀", self)
            self.prev_button.clicked.connect(self.find_prev)
            self.next_button = QPushButton("▶", self)
            self.next_button.clicked.connect(self.find_next)
            row.addWidget(self.prev_button)
            row.addWidget(self.next_button)
            self.replace_field = QLineEdit(self)
            self.replace_field.setObjectName("findReplaceField")
            self.replace_field.setPlaceholderText("Zamień")
            row.addWidget(self.replace_field, stretch=1)
            self.replace_button = QPushButton("Zamień", self)
            self.replace_button.clicked.connect(self.replace_one)
            self.replace_all_button = QPushButton("Zamień wszystko", self)
            self.replace_all_button.clicked.connect(self.replace_all)
            row.addWidget(self.replace_button)
            row.addWidget(self.replace_all_button)
            # review #7: F3 / Shift+F3 jump next/previous. QShortcuts scoped
            # to the bar+children fire even though focus is in a QLineEdit
            # (which would otherwise swallow the key); the .activated signals
            # are also the emit-pins the wiring tests exercise.
            self._f3 = QShortcut(QKeySequence(Qt.Key.Key_F3), self)
            self._f3.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self._f3.activated.connect(self.find_next)
            self._shift_f3 = QShortcut(QKeySequence("Shift+F3"), self)
            self._shift_f3.setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut
            )
            self._shift_f3.activated.connect(self.find_prev)
            self.setVisible(False)

        # -- open / close --
        def apply_read_only(self, ro: bool) -> None:
            # review #7: keep the replace controls in sync with the editor's
            # read-only state even while the bar is already open.
            self.replace_field.setEnabled(not ro)
            self.replace_button.setEnabled(not ro)
            self.replace_all_button.setEnabled(not ro)

        def open(self, prefill: str = "") -> None:
            if prefill:
                self.find_field.setText(prefill)
            self.apply_read_only(self._editor.isReadOnly())
            self.setVisible(True)
            if not self.isActiveWindow():
                # Focus only lands in an ACTIVE window. Right after show()
                # the activation event may still be queued (notably on the
                # offscreen platform), so request activation and flush the
                # pending events; otherwise setFocus below is deferred and
                # F3 keystrokes would miss the bar.
                self.window().activateWindow()
                QCoreApplication.processEvents()
            self.find_field.setFocus()
            self.find_field.selectAll()
            self._rehighlight()

        def close_bar(self) -> None:
            self.setVisible(False)
            self._editor.set_match_selections([])
            self._editor.setFocus()

        def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
            key = event.key()
            if key == Qt.Key.Key_Escape:
                self.close_bar()
                return
            if key == Qt.Key.Key_F3:  # review #7 (also handled by QShortcut)
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.find_prev()
                else:
                    self.find_next()
                return
            super().keyPressEvent(event)

        # -- search (review #8: re-based, never str.lower — case folding can
        #    change length, e.g. "İ".lower() → two code points) --
        def _pattern(self):
            needle = self.find_field.text()
            if not needle:
                return None
            flags = 0 if self.case_toggle.isChecked() else re.IGNORECASE
            return re.compile(re.escape(needle), flags)

        def _on_find_text_changed(self, _t) -> None:
            self._rehighlight()

        def _rehighlight(self) -> None:
            pat = self._pattern()
            spans = []
            if pat is not None:
                text = self._editor.toPlainText()
                spans = [(m.start(), m.end()) for m in pat.finditer(text)]
            self._editor.set_match_selections(spans)

        def find_next(self) -> bool:
            return self._find(forward=True)

        def find_prev(self) -> bool:
            return self._find(forward=False)

        def _find(self, forward: bool) -> bool:
            pat = self._pattern()
            if pat is None:
                return False
            text = self._editor.toPlainText()
            spans = [(m.start(), m.end()) for m in pat.finditer(text)]
            if not spans:
                return False
            cursor = self._editor.textCursor()
            # QTextCursor positions are UTF-16 units; python match offsets
            # are code points. Work in code points throughout and convert
            # only when setting the cursor (review #8).
            sel_start_u16 = cursor.selectionStart()
            sel_end_u16 = cursor.selectionEnd()
            # Convert the cursor's UTF-16 anchor/end to code-point offsets.
            cur_start = len(
                text.encode("utf-16-le")[: sel_start_u16 * 2].decode(
                    "utf-16-le", errors="ignore"
                )
            )
            cur_end = len(
                text.encode("utf-16-le")[: sel_end_u16 * 2].decode(
                    "utf-16-le", errors="ignore"
                )
            )
            if forward:
                nxt = next((s for s in spans if s[0] >= cur_end), None)
                start, end = nxt if nxt is not None else spans[0]  # wrap
            else:
                prev = [s for s in spans if s[1] <= cur_start]
                start, end = prev[-1] if prev else spans[-1]  # wrap
            cur = self._editor.textCursor()
            cur.setPosition(_utf16_offset(text, start))
            cur.setPosition(
                _utf16_offset(text, end), QTextCursor.MoveMode.KeepAnchor
            )
            self._editor.setTextCursor(cur)
            self._editor.centerCursor()
            self._rehighlight()
            return True

        # -- replace --
        def replace_one(self) -> None:
            if self._editor.isReadOnly():
                return
            pat = self._pattern()
            cursor = self._editor.textCursor()
            selected = cursor.selectedText()
            if pat is not None and selected and pat.fullmatch(selected):
                cursor.insertText(self.replace_field.text())
            self.find_next()

        def replace_all(self) -> int:
            if self._editor.isReadOnly():
                return 0
            pat = self._pattern()
            if pat is None:
                return 0
            original = self._editor.toPlainText()
            new_text, n = pat.subn(
                lambda _m: self.replace_field.text(), original
            )
            if n:
                cur = self._editor.textCursor()
                cur.beginEditBlock()
                cur.select(QTextCursor.SelectionType.Document)
                cur.insertText(new_text)
                cur.endEditBlock()
            self._rehighlight()
            return n

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
            self.find_bar = EditorFindBar(self.editor, self)
            layout.addWidget(self.find_bar)
            layout.addWidget(self.editor, stretch=1)

        def open_find(self, prefill: str = "") -> None:
            self.find_bar.open(prefill)

        def is_dirty(self) -> bool:
            return self.editor.is_dirty()

        def save(self) -> bool:
            ok = self.editor.save()
            if ok:
                self._hide_disk_banner()
            return ok

        def set_read_only(self, ro: bool) -> None:
            self.editor.set_read_only(ro)
            # review #7: keep an already-open find bar's replace controls in
            # sync when the run locks/unlocks the editor.
            self.find_bar.apply_read_only(ro)

        def _show_disk_banner(self) -> None:
            self.disk_banner.setVisible(True)

        def _hide_disk_banner(self) -> None:
            self.disk_banner.setVisible(False)

        def _on_reload_clicked(self) -> None:
            self.editor.reload_from_disk()
            self._hide_disk_banner()

    class SearchDialog(QDialog):
        """Floating find-in-files window (WebStorm-style Find in Path).

        Hosts the existing SearchPanel widget — this is a re-hosting, all
        search/replace machinery lives in the panel. Non-modal so the user
        can glance at the editor underneath. Esc (QDialog's default reject)
        and the window close button only HIDE the dialog: the search
        session's QThread stays alive for reopen and is torn down with the
        owning FilesView (stop_search / closeEvent), exactly as before.
        """

        def __init__(self, panel, parent=None):
            super().__init__(parent)
            self.setObjectName("searchDialog")
            self.setWindowTitle("Szukaj w plikach")
            self.setModal(False)
            self.panel = panel
            layout = QVBoxLayout(self)
            layout.setContentsMargins(6, 6, 6, 6)
            panel.setParent(self)
            layout.addWidget(panel)
            self._settings = QSettings("spar", "gui")
            self.resize(700, 500)
            geo = self._settings.value("files/search_dialog_geometry")
            if geo is not None:
                self.restoreGeometry(geo)

        def hideEvent(self, event) -> None:  # noqa: N802 (Qt override)
            # Persist geometry on every hide (Esc, close button, result
            # activation) so the next open — this process or the next —
            # comes back at the same size/position.
            self._settings.setValue(
                "files/search_dialog_geometry", self.saveGeometry()
            )
            super().hideEvent(event)

    class FilesView(QWidget):
        """Pliki module: project tree | tabbed Pygments editor (ADR 0006)."""

        # ADR 0006 tranche B: re-emitted from SearchPanel after the view has
        # opened the tab itself (review #9) — MainWindow only switches the
        # centre stack on it.
        open_location = Signal(str, int, int, int)  # rel, line, start, end

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

            # -- find-in-files floating dialog (ADR 0006 tranche B) --
            self.search_panel = SearchPanel(self.project_dir)
            self.search_panel.dirty_open_paths = self._dirty_open_paths
            # review #9: FilesView itself performs the open (tab + cursor)
            # AND re-emits open_location so MainWindow can switch the centre
            # view. This makes a standalone FilesView work without MainWindow.
            self.search_panel.open_location.connect(self._open_at_location)
            # The dialog reparents the panel into itself; it stays hidden
            # until Ctrl+Shift+F / open_search().
            self.search_dialog = SearchDialog(self.search_panel, self)

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

            # Ctrl+Shift+F opens/focuses find-in-files; Ctrl+F the current
            # tab's find bar. Same context (and the same eventFilter bridge
            # below) as Ctrl+S.
            self._find_in_files_shortcut = QShortcut(
                QKeySequence("Ctrl+Shift+F"), self
            )
            self._find_in_files_shortcut.setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut
            )
            self._find_in_files_shortcut.activated.connect(self.open_search)
            self._find_in_editor_shortcut = QShortcut(
                QKeySequence(QKeySequence.StandardKey.Find), self
            )
            self._find_in_editor_shortcut.setContext(
                Qt.ShortcutContext.WidgetWithChildrenShortcut
            )
            self._find_in_editor_shortcut.activated.connect(
                self._open_find_in_current_tab
            )

        # The QShortcut above is the real-display path (it consumes the chord
        # before it reaches any widget). Headless/offscreen — and any host
        # where the top-level window is never activated — never gives the
        # focus widget the shortcut map needs, so the chord is delivered to
        # the editor as a plain key event instead. This filter catches that
        # case and routes the SAME chord to _save_current (review #13). On a
        # real display the shortcut has already eaten the event, so this
        # never double-fires.
        def eventFilter(self, obj, event):  # noqa: N802 (Qt override)
            if event.type() == QEvent.Type.KeyPress:
                if event.matches(QKeySequence.StandardKey.Save):
                    self._save_current()
                    return True
                if event.matches(QKeySequence.StandardKey.Find):
                    self._open_find_in_current_tab()
                    return True
                if (
                    event.key() == Qt.Key.Key_F
                    and event.modifiers() == (
                        Qt.KeyboardModifier.ControlModifier
                        | Qt.KeyboardModifier.ShiftModifier
                    )
                ):
                    self.open_search()
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

        # -- find-in-files / find-in-editor (ADR 0006 tranche B) -----------
        def open_search(self) -> None:
            # hidden → show; visible → raise/focus. A second Ctrl+Shift+F is
            # NOT a toggle-close (Esc closes the dialog).
            if self.search_dialog.isVisible():
                self.search_dialog.raise_()
                self.search_dialog.activateWindow()
            else:
                self.search_dialog.show()
            self.search_panel.focus_query()

        def _open_find_in_current_tab(self) -> None:
            tab = self.tabs.currentWidget()
            if tab is None:
                return
            cursor = tab.editor.textCursor()
            tab.open_find(cursor.selectedText())

        def _open_at_location(self, rel: str, line: int, start: int, end: int) -> None:
            # review #9: open here, then re-emit so MainWindow switches view.
            self.open_at(self.project_dir / rel, line, start, end)
            self.open_location.emit(rel, line, start, end)
            # WebStorm behaviour: activating a result dismisses the floating
            # search window so the opened file is in view.
            self.search_dialog.hide()

        def open_at(self, path, line: int, start: int, end: int) -> None:
            self.open_file(path)
            tab = self.tabs.currentWidget()
            if tab is None:
                return
            ed = tab.editor
            block = ed.document().findBlockByNumber(max(0, line - 1))
            base = block.position()
            line_text = block.text()
            # review #8: start/end are code-point offsets within the line;
            # QTextCursor counts UTF-16 units. Convert so non-BMP lines
            # (e.g. an emoji before the match) select the right range.
            cur = QTextCursor(block)
            cur.setPosition(base + _utf16_offset(line_text, start))
            cur.setPosition(
                base + _utf16_offset(line_text, end),
                QTextCursor.MoveMode.KeepAnchor,
            )
            ed.setTextCursor(cur)
            ed.centerCursor()

        def stop_search(self) -> None:
            # review #3: idempotent teardown, called from closeEvent (below)
            # and MainWindow.closeEvent.
            self.search_panel.stop_session()

        def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
            # review #21: a STANDALONE FilesView (no MainWindow) must tear
            # down the SearchPanel's QThread when it closes — nothing else
            # routes through MainWindow.closeEvent in that case, so without
            # this the search panel's thread would outlive the widget.
            self.stop_search()
            self.search_dialog.close()
            super().closeEvent(event)

        def _dirty_open_paths(self) -> set:
            return {
                str(t.path) for t in self._tabs_by_path.values() if t.is_dirty()
            }

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
            # ADR 0006 tranche B: the run owns the tree while read-only, so
            # replace-in-files is gated too; search itself stays enabled.
            self.search_panel.set_replace_enabled(not self._read_only)

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
            self._last_event_sig = None

        def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
            if event.type() == QEvent.Type.KeyPress:
                if getattr(event, "isAutoRepeat", lambda: False)():
                    return False
                # A bare Shift is not consumed by the focused widget, so Qt
                # re-delivers the SAME event object up the parent chain and an
                # application-level filter sees every hop. Without dedup one
                # physical press counts as several presses → the finder fired
                # on a SINGLE Shift (live finding). Dedup by event identity.
                sig = (id(event), event.timestamp())
                if sig == self._last_event_sig:
                    return False
                self._last_event_sig = sig
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

    class _SearchWorker(QObject):
        """Runs the content search on a worker QThread. Every signal is
        generation-stamped; the loop early-exits when a newer generation
        supersedes it (``_live_generation`` is set from the GUI thread —
        a plain int assignment, atomic in CPython)."""

        # review #23: one FILE per batch — payload is
        # (list[SearchMatch], fingerprint-or-None) so the scan-time stat
        # travels with the matches it describes.
        match_batch = Signal(int, object)   # generation, (list[SearchMatch], fingerprint)
        # review #33: trailing bool = truncated (result cap hit mid-scan)
        finished = Signal(int, int, int, bool)  # generation, total_matches, total_files, truncated

        def __init__(self, project_dir, scan_file=None):
            super().__init__()
            self._project_dir = Path(project_dir)
            self._scan_file = scan_file or search_file
            self._live_generation = 0

        @Slot(int, object)
        def run_turn(self, generation: int, spec) -> None:
            # review #2: NEVER write _live_generation here — the facade owns
            # it (monotonic). The worker only READS it for early-exit. An
            # older queued turn writing its own generation back would delay
            # the newer search and repaint a cleared panel.
            try:
                pattern = compile_search_pattern(spec)
            except re.error:
                self.finished.emit(generation, 0, 0, False)
                return
            # reviews #4/#19: ripgrep ONLY for is_rg_compatible specs
            # (case-sensitive, non-whole-word, literal) on the real scan
            # path. rg runs to completion — or to the result cap (review
            # #33) — grouped per file; None means "fall back to the python
            # reference" (spawn failure, non-clean exit, or a non-UTF-8
            # parse anomaly).
            grouped = None
            if (
                self._scan_file is search_file
                and is_rg_compatible(spec)
                and ripgrep_available()
            ):
                grouped = self._ripgrep_grouped(generation, spec)
            if grouped is not None:
                groups, truncated = grouped  # review #33
                self._emit_grouped(generation, groups, truncated)
                return
            # -- python reference path (regex, injected fake, or fallback) --
            total_matches = 0
            total_files = 0
            truncated = False  # review #33: cap hit → flag, not silent stop
            for rel in build_file_index(self._project_dir):
                if generation != self._live_generation:
                    return
                # review #23: fingerprint BEFORE the read — a file
                # modified after this stat (even mid-scan) must fail the
                # replace-time verification.
                fingerprint = _stat_fingerprint(self._project_dir / rel)
                # review #35: cap BEFORE emitting — the old shape emitted
                # each file's COMPLETE matches list and only then checked
                # the cap. review #37: the remaining allowance is passed
                # DOWN as search_file's limit so the scan stops at the cap
                # instead of materializing millions of SearchMatch objects
                # from one pathological file and slicing afterwards.
                remaining = _SEARCH_MAX_RESULTS - total_matches
                matches = self._scan_file(
                    self._project_dir, rel, pattern, limit=remaining
                )
                if matches:
                    # len == remaining ⇒ allowance exhausted ⇒ truncated.
                    # The slice stays as belt-and-braces for an injected
                    # scan_file that over-returns past its limit.
                    if len(matches) >= remaining:
                        matches = matches[:remaining]
                        truncated = True  # review #33: mirrors the rg path
                    total_files += 1
                    total_matches += len(matches)
                    self.match_batch.emit(generation, (matches, fingerprint))
                if truncated:
                    break
            if generation == self._live_generation:
                self.finished.emit(
                    generation, total_matches, total_files, truncated
                )

        def _ripgrep_grouped(self, generation, spec):
            """Run rg for an is_rg_compatible *spec* (review #19) over the
            EXPLICIT file list from build_file_index (review #13),
            prefiltered by passes_search_guards and batched by _rg_batches
            (review #34: argv BYTE budget first, file count second;
            review #36: the budget counts the ABSOLUTE ``str(root / rel)``
            strings build_ripgrep_argv actually passes — see the
            builder's docstring; an E2BIG that still slips through
            surfaces as OSError from Popen and takes the fallback below);
            return ``(grouped, truncated)`` where *grouped* is a list of
            (rel, fingerprint, [SearchMatch]) grouped per file and
            *truncated* flags a result-cap stop (review #33) — or None to
            signal a python fallback (review #4).

            review #33: the _SEARCH_MAX_RESULTS cap is enforced HERE,
            while parsing — accumulating everything and letting
            _emit_grouped cap the EMISSION would still hold an unbounded
            `grouped` in memory (a common literal over a big tree can
            match millions of lines → OOM). On reaching the cap the
            worker kills rg mid-stream, skips the remaining batches, and
            returns what it has with truncated=True, mirroring the python
            path's cap-break.

            review #13: rg is handed the exact same file set the python
            reference scans (``rg -e pat -- f1 f2 …``) instead of a directory
            root, so the two engines can never diverge on filesystem
            semantics (symlinked files, .gitignore, hidden files, excludes).

            reviews #14/#22/#24: each batch runs under subprocess.Popen.
            A daemon reader thread drains stdout into a queue.Queue
            (None sentinel = EOF) so rg can never block on a full pipe;
            the worker pulls lines with queue.get(timeout=0.05) and
            re-checks self._live_generation on EVERY timeout tick —
            review #24: a large no-match batch emits NO stdout, so a
            worker blocking in `for line in proc.stdout` awaiting output
            could not see a supersede until the batch ended, stalling
            this run and every queued search. Waiting for exit before
            reading (poll loop + communicate) would DEADLOCK once the
            --json output exceeds the OS pipe capacity (~64 KB). A
            second daemon thread drains stderr. A superseding search
            kills rg mid-stream (or mid-silence) and returns None (the
            caller then takes the python path, whose first generation
            check early-exits at once). Never blocks a newer query
            behind a slow rg.

            reviews #23/#25: every file of a batch is statted BEFORE
            that batch's rg process is launched (pre-launch snapshot
            dict rel → fingerprint); parsed groups take their
            fingerprints from the snapshot. Stat-at-first-parsed-match
            ran AFTER rg had already read the file and could bless a
            write landing between rg's read and the parse."""
            import queue
            import subprocess
            import threading

            # review #19: apply the SAME size/binary guards the python
            # reference applies (shared predicate), so rg never scans a
            # file search_file would skip.
            files = [
                rel for rel in build_file_index(self._project_dir)
                if passes_search_guards(self._project_dir, rel)
            ]
            if not files:
                return ([], False)
            # Each file appears in exactly one batch and rg keeps a file's
            # matches contiguous, so per-file grouping is stable across
            # the consecutive batches (state spans the batch loop).
            grouped: list = []
            current_rel = None
            bucket: list = []
            total_parsed = 0  # review #33: cap counted DURING the parse
            fingerprints: dict = {}  # review #25: pre-launch snapshot
            # review #34: byte-budget batches, count as secondary bound;
            # review #36: budgeted on the ABSOLUTE strings rg receives.
            for batch in _rg_batches(self._project_dir, files):
                if generation != self._live_generation:
                    return None  # superseded → python path early-exits
                # review #25: stat the WHOLE batch BEFORE launching rg —
                # rg reads the files only after this snapshot, so any
                # later write (even mid-stream) fails the replace-time
                # fingerprint verification.
                for rel in batch:
                    fingerprints[rel] = _stat_fingerprint(
                        self._project_dir / rel
                    )
                argv = build_ripgrep_argv(self._project_dir, spec, batch)
                try:
                    proc = subprocess.Popen(
                        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True,
                    )
                except OSError:
                    return None  # spawn/exec failure
                # review #22: drain stderr on a side thread so a warning-
                # spewing rg can never fill THAT pipe either.
                stderr_chunks: list = []
                drain = threading.Thread(
                    target=lambda: stderr_chunks.append(proc.stderr.read()),
                    daemon=True,
                )
                drain.start()
                # review #24: a daemon reader pushes stdout lines into a
                # queue (None = EOF) — the worker never blocks on the
                # pipe itself, so a supersede is noticed within ~50 ms
                # even while rg emits nothing (large no-match batch).
                lines: "queue.Queue" = queue.Queue()

                def _pump(out=proc.stdout, q=lines):
                    for line in out:
                        q.put(line)
                    q.put(None)

                reader = threading.Thread(target=_pump, daemon=True)
                reader.start()

                def _stdout_lines():
                    # reviews #22/#24: queue.get with a 50 ms timeout —
                    # the live generation is checked on every tick, not
                    # only between lines.
                    while True:
                        if generation != self._live_generation:
                            return  # loop below sees the stale generation
                        try:
                            line = lines.get(timeout=0.05)
                        except queue.Empty:
                            continue
                        if line is None:
                            return  # EOF — rg closed stdout
                        yield line

                try:
                    for m in parse_ripgrep_stream(
                        _stdout_lines(), self._project_dir
                    ):
                        if m.path != current_rel:
                            if current_rel is not None:
                                grouped.append((
                                    current_rel,
                                    # review #25: fingerprint from the
                                    # PRE-launch snapshot, never a stat
                                    # taken after rg read the file.
                                    fingerprints[current_rel],
                                    bucket,
                                ))
                                bucket = []
                            current_rel = m.path
                        bucket.append(m)
                        total_parsed += 1
                        if total_parsed >= _SEARCH_MAX_RESULTS:
                            # review #33: cap reached WHILE parsing —
                            # kill rg mid-stream, skip every remaining
                            # batch, flush the open bucket and return the
                            # collected groups flagged as truncated.
                            proc.kill()
                            proc.wait()
                            reader.join()
                            drain.join()
                            grouped.append((
                                current_rel,
                                fingerprints[current_rel],
                                bucket,
                            ))
                            return (grouped, True)
                except _RipgrepParseError:
                    proc.kill()
                    proc.wait()
                    reader.join()
                    drain.join()
                    return None  # non-UTF-8 "bytes" member → python reference
                if generation != self._live_generation:
                    # review #14/#24: kill mid-stream OR mid-silence,
                    # never await — the queue drain guarantees this
                    # branch is reached within ~50 ms of the supersede.
                    proc.kill()
                    proc.wait()
                    reader.join()
                    drain.join()
                    return None  # cancelled mid-run → python early-exits
                proc.wait()      # stdout is exhausted — rg is exiting
                reader.join()
                drain.join()
                err = (stderr_chunks[0] if stderr_chunks else "") or ""
                # rg exit codes: 0 = matches, 1 = no matches, >=2 = error.
                if proc.returncode not in (0, 1) or err.strip():
                    return None
            if current_rel is not None:
                grouped.append(
                    (current_rel, fingerprints[current_rel], bucket)
                )
            return (grouped, False)

        def _emit_grouped(self, generation, grouped, truncated):
            # review #33: the cap is enforced at PARSE time (see
            # _ripgrep_grouped) — `grouped` is already ≤ the cap; the
            # break below is a defensive belt only.
            total_matches = 0
            total_files = 0
            for _rel, fingerprint, bucket in grouped:
                if generation != self._live_generation:
                    return
                if bucket:
                    total_files += 1
                    total_matches += len(bucket)
                    # review #23: the scan-time fingerprint travels with
                    # its file's matches.
                    self.match_batch.emit(generation, (bucket, fingerprint))
                if total_matches >= _SEARCH_MAX_RESULTS:
                    break
            if generation == self._live_generation:
                self.finished.emit(
                    generation, total_matches, total_files, truncated
                )

    # review #3: strong registry pinning every live SearchSession. The
    # QThread is owned by shiboken (no Qt parent), so its C++ object is
    # deleted when the facade's Python wrapper is garbage-collected — and
    # Qt ABORTS (or, mid-run, SEGFAULTS as it races the worker) if that
    # happens while the thread runs. A worker that finished a search sits
    # IDLE in its event loop (``isRunning() is True``), so it never
    # self-stops. pytest-qt holds only a WEAKREF to a panel, so its
    # teardown ``close()`` (hence ``closeEvent``/``stop()``) can be skipped
    # and the facade GC'd at an arbitrary later point. Pinning it here
    # keeps every facade (and its thread) alive until an explicit
    # ``stop()`` unpins it — making teardown deterministic instead of
    # racing GC finalization against Qt's C++ destruction.
    _LIVE_SEARCH_SESSIONS: "set[SearchSession]" = set()

    def _shutdown_search_thread(thread) -> None:
        """Quit and JOIN *thread* if it is still running. Registered as a
        ``weakref.finalize`` on every SearchSession so a facade that is
        never explicitly stopped still tears its thread down cleanly when
        it is finalized — for a pinned facade that is at interpreter exit
        (weakref runs pending finalizers before the modules are torn
        down). Captures only the thread, never the facade."""
        try:
            if thread.isRunning():
                thread.quit()
                thread.wait(3000)
        except RuntimeError:
            pass

    class SearchSession(QObject):
        """GUI-thread facade over _SearchWorker (mirrors ConversationSession):
        generation-token cancellation, generation-filtered delivery."""

        batch = Signal(object)        # (list[SearchMatch], fingerprint) — one file
        # review #33: trailing bool = truncated ("wyniki obcięte")
        finished = Signal(int, int, bool)  # total_matches, total_files, truncated
        _dispatch = Signal(int, object)  # generation, spec

        def __init__(self, project_dir, parent=None, scan_file=None):
            super().__init__(parent)
            self._generation = 0
            self._started = False   # review #3: lazy thread start
            self._stopped = False   # review #3: idempotent stop
            self._thread = QThread()
            self._worker = _SearchWorker(project_dir, scan_file=scan_file)
            self._worker.moveToThread(self._thread)
            self._dispatch.connect(
                self._worker.run_turn, Qt.ConnectionType.QueuedConnection
            )
            self._worker.match_batch.connect(
                self._on_batch, Qt.ConnectionType.QueuedConnection
            )
            self._worker.finished.connect(
                self._on_finished, Qt.ConnectionType.QueuedConnection
            )
            self._thread.finished.connect(self._worker.deleteLater)
            # review #3: guarantee the QThread is quit+joined before its
            # shiboken-owned C++ object is destroyed, even if the owner is
            # garbage-collected before closeEvent/stop() runs (see
            # _shutdown_search_thread). The finalizer captures only the
            # thread (never self), so it does not keep the facade alive.
            self._finalizer = weakref.finalize(
                self, _shutdown_search_thread, self._thread
            )
            # review #3: pin until stop() so GC never tears the running
            # thread down mid-run (races Qt's C++ destruction → segfault).
            _LIVE_SEARCH_SESSIONS.add(self)

        def _ensure_started(self) -> None:
            # review #3: only spawn the QThread once a search actually runs,
            # so panels that never search never leak a thread.
            if not self._started and not self._stopped:
                self._thread.start()
                self._started = True

        def search(self, spec) -> None:
            self._ensure_started()
            self._generation += 1
            # Set the worker's live generation BEFORE dispatch so an
            # in-flight older run sees the bump and bails (int write is
            # atomic in CPython; the worker only ever reads it — review #2).
            self._worker._live_generation = self._generation
            self._dispatch.emit(self._generation, spec)

        def cancel(self) -> None:
            # review #2: supersede any in-flight run WITHOUT dispatching a
            # new one (empty/invalid query). Late batches are dropped by the
            # generation filter and the worker loop early-exits.
            self._generation += 1
            self._worker._live_generation = self._generation

        def stop(self) -> None:
            if self._stopped:   # review #3: idempotent
                return
            self._stopped = True
            _LIVE_SEARCH_SESSIONS.discard(self)  # review #3: unpin
            self._generation += 1
            # review #14: bump the worker's live generation too, so an
            # in-flight python scan (or an rg poll loop) sees the supersede
            # and bails PROMPTLY instead of finishing the whole walk before
            # the thread quits.
            self._worker._live_generation = self._generation
            thread = self._thread
            try:
                if thread.isRunning():
                    _SEARCH_ABANDONED_THREADS.add(thread)

                    def _release(thread=thread) -> None:
                        thread.deleteLater()
                        _SEARCH_ABANDONED_THREADS.discard(thread)

                    thread.finished.connect(_release)
                thread.quit()
                # review #3: JOIN the (now generation-bumped) thread so a
                # caller's teardown — SearchPanel.closeEvent under
                # qtbot.addWidget has no fixture that waits — never destroys
                # a still-running QThread (Qt aborts the process on that).
                # The worker bails promptly on the supersede, so this returns
                # in ~ms; the bounded timeout keeps a genuinely stuck thread
                # abandoned (already in _SEARCH_ABANDONED_THREADS) instead of
                # blocking the GUI indefinitely.
                thread.wait(3000)
            except RuntimeError:
                pass

        def _on_batch(self, generation: int, payload) -> None:
            if generation != self._generation:
                return
            self.batch.emit(payload)

        def _on_finished(self, generation: int, total_matches: int,
                         total_files: int, truncated: bool) -> None:
            if generation != self._generation:
                return
            self.finished.emit(total_matches, total_files, truncated)

    class SearchPanel(QWidget):
        """Find/replace-in-files panel (ADR 0006 tranche B), hosted in the
        floating SearchDialog. Holds all search/replace machinery."""

        open_location = Signal(str, int, int, int)  # rel, line, start, end

        def __init__(self, project_dir, parent=None, session=None):
            super().__init__(parent)
            self.setObjectName("searchPanel")
            self.project_dir = Path(project_dir)
            self._invalid = False
            self._replace_enabled = True
            # review #5/#15: the spec whose results FULLY populate the tree.
            # Replace reuses THIS, not the live controls; if the controls
            # drift from it the replace button is disabled. It is promoted
            # from _pending_spec ONLY in _on_finished (never at dispatch), so
            # a still-running search never exposes a partial tree to replace.
            self._pending_spec = None
            self._results_spec = None
            # review #16: sticky replace summary; when set, the refresh
            # search's _on_finished appends its counts to it (not overwrite).
            self._replace_summary = None

            outer = QVBoxLayout(self)
            outer.setContentsMargins(6, 4, 6, 4)
            outer.setSpacing(4)

            # -- query row --
            query_row = QHBoxLayout()
            self.query = QLineEdit(self)
            self.query.setObjectName("searchQuery")
            self.query.setPlaceholderText("Szukaj w plikach… (Enter)")
            self.query.returnPressed.connect(self._run_search)
            # review #5: editing the query without re-running marks the
            # existing results stale (disables replace).
            self.query.textChanged.connect(self._update_replace_state)
            query_row.addWidget(self.query, stretch=1)
            self.case_toggle = QToolButton(self)
            self.case_toggle.setObjectName("searchToggle")
            self.case_toggle.setText("Aa")
            self.case_toggle.setCheckable(True)
            self.case_toggle.setToolTip("Rozróżniaj wielkość liter")
            self.regex_toggle = QToolButton(self)
            self.regex_toggle.setObjectName("searchToggle")
            self.regex_toggle.setText(".*")
            self.regex_toggle.setCheckable(True)
            self.regex_toggle.setToolTip("Wyrażenie regularne")
            self.word_toggle = QToolButton(self)
            self.word_toggle.setObjectName("searchToggle")
            self.word_toggle.setText("W")
            self.word_toggle.setCheckable(True)
            self.word_toggle.setToolTip("Całe słowa")
            for tb in (self.case_toggle, self.regex_toggle, self.word_toggle):
                tb.clicked.connect(self._run_search)
                query_row.addWidget(tb)
            outer.addLayout(query_row)

            # -- replace row --
            replace_row = QHBoxLayout()
            self.replace = QLineEdit(self)
            self.replace.setObjectName("replaceField")
            self.replace.setPlaceholderText("Zamień na…")
            replace_row.addWidget(self.replace, stretch=1)
            self.replace_button = QPushButton("Zamień zaznaczone", self)
            self.replace_button.setObjectName("replaceButton")
            self.replace_button.clicked.connect(self._apply_replace)
            replace_row.addWidget(self.replace_button)
            outer.addLayout(replace_row)

            # -- results tree --
            self.results = QTreeWidget(self)
            self.results.setObjectName("searchResults")
            self.results.setHeaderHidden(True)
            self.results.itemActivated.connect(self._on_item_activated)
            self.results.itemClicked.connect(self._on_item_activated)
            outer.addWidget(self.results, stretch=1)

            # -- status --
            self.status = QLabel("", self)
            self.status.setObjectName("searchStatus")
            outer.addWidget(self.status)

            self._session = session or SearchSession(self.project_dir, self)
            self._session.batch.connect(self._on_batch)
            self._session.finished.connect(self._on_finished)

            # default hook: no open editor is dirty (MainWindow injects the
            # real one). Set at the END of __init__ so all controls exist.
            self.dirty_open_paths = lambda: set()
            # review #42: QPushButton defaults to ENABLED, and nothing else
            # runs before the first search — sync the fresh panel now
            # (_results_spec is None → stale → button disabled).
            self._update_replace_state()

        # -- spec / validation --
        def _current_spec(self):
            return SearchSpec(
                self.query.text(),
                regex=self.regex_toggle.isChecked(),
                case_sensitive=self.case_toggle.isChecked(),
                whole_word=self.word_toggle.isChecked(),
            )

        def _mark_invalid(self, invalid: bool) -> None:
            self._invalid = invalid
            self.query.setProperty("invalid", invalid)
            # Repolish so the dynamic-property QSS re-applies.
            self.query.style().unpolish(self.query)
            self.query.style().polish(self.query)

        def focus_query(self) -> None:
            self.query.setFocus()
            self.query.selectAll()

        def set_replace_enabled(self, enabled: bool) -> None:
            self._replace_enabled = enabled
            self.replace.setEnabled(enabled)
            for i in range(self.results.topLevelItemCount()):
                item = self.results.topLevelItem(i)
                flags = item.flags()
                if enabled:
                    flags |= Qt.ItemFlag.ItemIsUserCheckable
                else:
                    flags &= ~Qt.ItemFlag.ItemIsUserCheckable
                item.setFlags(flags)
            # _update_replace_state also honours result-staleness (review #5).
            self._update_replace_state()

        def _apply_replace(self) -> None:
            if not self._replace_enabled:
                return
            # review #5: replace the STORED search spec, never the live
            # controls (which may have drifted). If the results are stale
            # the button is already disabled, but guard defensively.
            spec = self._results_spec
            if spec is None or spec != self._current_spec():
                return
            try:
                pattern = compile_search_pattern(spec)
            except re.error:
                return
            replacement = self.replace.text()
            # review #29: dirty-tab protection compares RESOLVED targets on
            # BOTH sides — a file open dirty under one path must also block
            # a replace reaching the same file via a symlink alias.
            dirty = set()
            for p in self.dirty_open_paths():
                try:
                    dirty.add(str(Path(p).resolve()))
                except (OSError, RuntimeError):
                    dirty.add(str(p))  # unresolvable → keep the raw string
            # review #20: escape check is done on RESOLVED paths, both sides.
            root_resolved = Path(self.project_dir).resolve()
            total = files = 0
            skipped_dirty = skipped_changed = skipped_nonutf8 = skipped_err = 0
            skipped_symlink = 0
            for i in range(self.results.topLevelItemCount()):
                item = self.results.topLevelItem(i)
                if item.checkState(0) != Qt.CheckState.Checked:
                    continue
                rel = item.data(0, Qt.ItemDataRole.UserRole + 1)
                fingerprint = item.data(0, Qt.ItemDataRole.UserRole + 2)
                abs_path = self.project_dir / rel
                # review #20: os.replace on a symlink path would replace the
                # LINK with a regular file. Resolve to the real target and
                # operate on THAT; a target outside the project is refused.
                # review #30: resolution runs INSIDE the per-row try — a
                # symlink loop raises (RuntimeError on older Pythons,
                # OSError/ELOOP elsewhere) and must skip THIS row only,
                # never abort the whole batch.
                try:
                    real_path = abs_path.resolve()
                except (OSError, RuntimeError):
                    skipped_err += 1
                    continue
                # review #29: check the RESOLVED target against the
                # RESOLVED dirty set, so a symlink alias of a dirty tab
                # is skipped as niezapisane zmiany.
                if str(real_path) in dirty:
                    skipped_dirty += 1
                    continue
                try:
                    real_path.relative_to(root_resolved)
                except ValueError:
                    skipped_symlink += 1
                    continue
                try:
                    st = real_path.stat()
                except OSError:
                    skipped_err += 1
                    continue
                # review #6: refuse to write if the file changed since the
                # SCAN-time fingerprint (review #23) was captured.
                if fingerprint is None or (st.st_mtime_ns, st.st_size) != fingerprint:
                    skipped_changed += 1
                    continue
                try:
                    raw = real_path.read_bytes()
                except OSError:
                    skipped_err += 1
                    continue
                try:
                    text = raw.decode("utf-8")  # STRICT — never corrupt bytes
                except UnicodeDecodeError:
                    skipped_nonutf8 += 1
                    continue
                try:
                    new_text, n = replace_in_text(
                        text, pattern, replacement, regex=spec.regex
                    )
                except re.error:
                    skipped_err += 1
                    continue
                if not n:
                    continue
                try:
                    # No newline translation: encode and write bytes
                    # atomically — to the RESOLVED target (review #20).
                    _atomic_write_bytes(real_path, new_text.encode("utf-8"))
                except OSError:
                    skipped_err += 1
                    continue
                total += n
                files += 1
            msg = f"zamieniono {total} w {files} plikach"
            skips = []
            if skipped_dirty:
                skips.append(f"{skipped_dirty} (niezapisane zmiany)")
            if skipped_symlink:
                skips.append(f"{skipped_symlink} (dowiązanie poza projektem)")
            if skipped_changed:
                skips.append(f"{skipped_changed} (plik zmienił się)")
            if skipped_nonutf8:
                skips.append(f"{skipped_nonutf8} (nie-UTF-8)")
            if skipped_err:
                skips.append(f"{skipped_err} (błąd zapisu)")
            if skips:
                msg += " · pominięto " + ", ".join(skips)
            # review #16: make the replace summary STICKY. Setting the label
            # synchronously is not enough — the refresh search below finishes
            # on a later event-loop turn and its _on_finished would clobber
            # this text. Stash it so that _on_finished APPENDS its counts to
            # the summary instead of overwriting it, and re-run the search
            # with preserve_summary so the transient "szukam…" never wins.
            self._replace_summary = msg
            self.status.setText(msg)
            self._run_search(preserve_summary=True)

        def stop_session(self) -> None:
            # review #3: idempotent teardown of the search thread. Called by
            # closeEvent, FilesView.stop_search/closeEvent (review #21),
            # and MainWindow.closeEvent.
            self._session.stop()

        def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
            self.stop_session()
            super().closeEvent(event)

        def _update_replace_state(self) -> None:
            # review #5: replace targets the spec whose results are shown.
            # If the live controls drift from it (query edited, toggle
            # changed without re-running), disable replace with a hint.
            # Defensive: no-op until Task 4 adds the replace button.
            button = getattr(self, "replace_button", None)
            if button is None:
                return
            stale = (
                self._results_spec is None
                or self._results_spec != self._current_spec()
            )
            button.setEnabled(self._replace_enabled and not stale)
            button.setToolTip(
                "wyniki nieaktualne — uruchom szukanie ponownie"
                if (stale and self._replace_enabled) else ""
            )

        # -- run --
        def _run_search(self, *_args, preserve_summary: bool = False) -> None:
            # *_args absorbs the bool that QToolButton.clicked emits (the
            # toggles connect straight to this slot). review #16: a
            # user-initiated search clears any sticky replace summary; only
            # the post-replace refresh passes preserve_summary=True.
            if not preserve_summary:
                self._replace_summary = None
            spec = self._current_spec()
            if not spec.query:
                # review #2: cancel any in-flight run so it can't repopulate
                # the panel we are about to clear.
                self._session.cancel()
                self.results.clear()
                self.status.setText("")
                self._mark_invalid(False)
                self._pending_spec = None
                self._results_spec = None
                self._update_replace_state()
                return
            try:
                compile_search_pattern(spec)
            except re.error:
                self._session.cancel()  # review #2: cancel in-flight
                # review #28: mirror the empty-query path — the cancelled
                # run may already have delivered partial batches, so clear
                # the tree and reset BOTH specs; otherwise stale partial
                # results sit under the error banner and (with specs still
                # set) could even be replaced.
                self.results.clear()
                self._pending_spec = None
                self._results_spec = None
                self._mark_invalid(True)
                self.status.setText("niepoprawne wyrażenie regularne")
                self._update_replace_state()
                return
            self._mark_invalid(False)
            self.results.clear()
            if not preserve_summary:
                self.status.setText("szukam…")
            # review #15: the tree is being (re)built for *spec* but is NOT
            # yet complete. Stash it as _pending_spec ONLY; keep _results_spec
            # None so replace stays DISABLED until _on_finished promotes it —
            # otherwise a partial (mid-scan) tree could be replaced.
            self._pending_spec = spec
            self._results_spec = None
            self._session.search(spec)
            self._update_replace_state()

        def _on_batch(self, payload) -> None:
            # Group into (or reuse) a file row, append line children.
            # review #23: the scan-time fingerprint arrives WITH the
            # matches; the GUI only stores it, never stats the file.
            matches, fingerprint = payload
            for m in matches:
                file_item = self._file_item(m.path, fingerprint)
                line_item = QTreeWidgetItem(file_item, [f"{m.line}: {m.text}"])
                line_item.setData(0, Qt.ItemDataRole.UserRole,
                                  (m.path, m.line, m.start, m.end))
                count = file_item.childCount()
                file_item.setText(0, f"{m.path}  ({count})")

        def _file_item(self, rel: str, fingerprint):
            for i in range(self.results.topLevelItemCount()):
                item = self.results.topLevelItem(i)
                if item.data(0, Qt.ItemDataRole.UserRole + 1) == rel:
                    return item
            item = QTreeWidgetItem(self.results, [f"{rel}  (0)"])
            item.setData(0, Qt.ItemDataRole.UserRole + 1, rel)
            # review #23: store the SCAN-TIME fingerprint from the payload
            # (rows are built after rg scanned a whole batch — a GUI-side
            # stat here would bless STALE matches with a NEW fingerprint).
            item.setData(0, Qt.ItemDataRole.UserRole + 2, fingerprint)
            item.setCheckState(0, Qt.CheckState.Checked)  # default checked
            if self._replace_enabled:  # review #11
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            else:
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            item.setExpanded(True)
            return item

        def _on_finished(self, total_matches: int, total_files: int,
                         truncated: bool = False) -> None:
            # review #15: the tree is now COMPLETE for _pending_spec — only
            # here (never at dispatch) is it promoted to the replace target.
            # The facade already generation-filters finished, so this fires
            # only for the current (live) search.
            self._results_spec = self._pending_spec
            counts = f"{total_matches} wyników w {total_files} plikach"
            if truncated:  # review #33: the scan stopped at the cap
                counts += (
                    f" · wyniki obcięte do {_SEARCH_MAX_RESULTS}"
                )
            # review #16: if a replace just ran, this is its refresh search —
            # append the counts to the sticky summary instead of clobbering
            # it, then consume the summary (persists until the next search).
            if self._replace_summary is not None:
                self.status.setText(f"{self._replace_summary} · {counts}")
                self._replace_summary = None
            else:
                self.status.setText(counts)
            self._update_replace_state()

        def _on_item_activated(self, item, _col) -> None:
            payload = item.data(0, Qt.ItemDataRole.UserRole)
            if payload is None:  # a file row, not a line
                return
            rel, line, start, end = payload
            self.open_location.emit(rel, line, start, end)
