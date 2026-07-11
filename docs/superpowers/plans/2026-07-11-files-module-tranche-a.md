# Files module — tranche A (ADR 0006): left-rail view switch, Pygments editor, double-Shift finder

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan. Each task below is a self-contained unit of work: dispatch it to a subagent, have the subagent follow the TDD loop verbatim (write the failing test, run it, implement, run again, commit), and report the exact pytest tail back. Do NOT batch tasks. Verify the suite is green (`.venv/bin/python -m pytest tests/ -q`, baseline **847 passed, 2 skipped**) at the end of every task before starting the next.

## Goal

Deliver **tranche A** of the Pliki module exactly as scoped by
`docs/adr/0006-files-module-editor-and-search.md` — and nothing from tranche B
(no find-in-files, no Ctrl+F). Concretely:

- the window centre becomes a switched stack (**Strumień** = the existing
  stream pane; **Pliki** = a new `FilesView`), driven by two exclusive radio
  toggles on the **left rail**;
- `FilesView` = project tree (`QTreeView` + `QFileSystemModel`) alongside a
  tabbed editor;
- the editor is a `QPlainTextEdit` subclass with a line-number gutter,
  current-line highlight, and a Pygments-based syntax highlighter;
- the read-only matrix (RUNNING / GATE_PENDING / LOCKED → read-only) is driven
  from the same `RunnerState` signal the toolbar already consumes, with a
  `QFileSystemWatcher` that auto-reloads clean files and warns on conflicts;
- **double Shift** opens a fuzzy file finder overlay.

## Architecture

New module: **`spar/gui/files.py`** (mirrors `orchestrator.py` / `rails.py`):
Qt-free helpers live ABOVE an `if _HAS_QT:` guard so the module imports on a
plain interpreter and their tests run under plain `python3` with no
`importorskip`.

- **Pure helpers (above the guard):** `pick_lexer(filename)`,
  `fuzzy_score(query, candidate)`, `filter_paths(query, paths)`,
  `build_file_index(root, skip_dirs)`.
- **Qt layer (under the guard):** `PygmentsHighlighter(QSyntaxHighlighter)`,
  `FileEditor(QPlainTextEdit)` (+ private `_LineNumberArea`), `EditorTab(QWidget)`,
  `FilesView(QWidget)` (+ private `_TreeFilterProxy(QSortFilterProxyModel)`),
  `FileFinderOverlay(QWidget)`, `DoubleShiftFilter(QObject)`.

Wiring lives in **`spar/gui/app.py`** (`MainWindow`): the centre `QStackedWidget`,
the left-rail radio state machine, run-start auto-switch, and pushing
`RunnerState` into `FilesView`.

Data flow (centre switch):

```
left_rail.toggled(key, checked) ─▶ MainWindow._on_left_rail_toggled
                                     ├─ exactly-one invariant (bounce/uncheck)
                                     ├─ FilesView.confirm_discard_if_dirty() (Strumień only)
                                     └─ MainWindow._set_centre_view(key)  ─▶ QStackedWidget.setCurrentIndex + persist

runner.started ─▶ MainWindow._on_started ─▶ _set_centre_view("stream")   (covers exec/resume/new-debate AND the gate auto-exec chain)

side_pane/runner state_changed ─▶ MainWindow._on_state_changed ─▶ FilesView.set_state(state)  ─▶ read-only matrix
```

Data flow (finder):

```
QApplication event ─▶ DoubleShiftFilter.eventFilter (two bare Shift < 400ms) ─▶ MainWindow._open_finder
FileFinderOverlay.file_chosen(rel) ─▶ MainWindow: _set_centre_view("files") + FilesView.open_file(project_dir/rel)
```

## Tech Stack

Python 3.12, PySide6 (`gui` extra), pytest + pytest-qt, GUI tests offscreen
(`QT_QPA_PLATFORM=offscreen`, already set in `tests/conftest.py`). **New
dependency: `pygments`** (BSD, pure Python) added to the `gui` extra.

## Global Constraints

- **Suite green after every task:** `.venv/bin/python -m pytest tests/ -q`,
  baseline **847 passed, 2 skipped**. Each task's commit is made only after a
  green run.
- **GUI tests offscreen** — never call `window.show()` unless a test explicitly
  needs it; prefer `isHidden()` over `isVisible()` for pre-show assertions
  (review #8 convention).
- **Pure helpers** (`pick_lexer`, `fuzzy_score`, `filter_paths`,
  `build_file_index`) live ABOVE the `if _HAS_QT:` guard; their test file
  `tests/test_gui_files_pure.py` has **no `importorskip`** and runs under a
  plain `python3` (pygments is pure Python and ships in the `gui` extra
  installed for dev).
- **Existing tests untouched** unless a wiring change forces it. Exactly one
  such change is expected and is called out where it happens (Task 5, the
  left-rail placeholder → radio toggles in `tests/test_gui_app.py`).
- **No AI-attribution trailers** in commits (no `Co-Authored-By`, no
  "Generated with").
- **README + HANDOFF + ADR stamp** land in the same tranche (Task 7), per the
  keep-README-current rule.
- **QSettings keys are namespaced** (`rails/centre_view`, `files/tree_split`) to
  match the existing `rails/*`, `mainSplitter/*` conventions.
- **Init-order constraint (do not regress):** in `MainWindow.__init__`, the
  single initial `self.side_pane.refresh()` MUST stay at the END of `__init__`;
  it fires `status_changed` → `_on_status_changed` synchronously, so every
  widget that handler (or `_sync_toolbar` → `_on_state_changed`) touches —
  now including `self.files_view` — must be built and wired first. `FilesView`
  is constructed alongside the stream pane, before the splitter is assembled.
- Do not break right-rail semantics (Taski/Czat/Bramka), the gate force-open,
  or the orchestrator chat.

---

### Task 1: Pure helpers module — lexer pick, fuzzy scorer, file index (Sonnet)

Foundational, Qt-free. Establishes `spar/gui/files.py` with the import guard and
adds the `pygments` dependency.

**Files**
- `pyproject.toml` (add `pygments` to the `gui` extra)
- `spar/gui/files.py` (new — pure section + `_HAS_QT` guard scaffold)
- `tests/test_gui_files_pure.py` (new — no `importorskip`)

**Interfaces**
- Produces `pick_lexer(filename: str) -> pygments.lexer.Lexer` (falls back to
  `TextLexer` for unknown names; never reads the file).
- Produces `fuzzy_score(query: str, candidate: str) -> int | None` (`None` when
  `query` is not an in-order subsequence of `candidate`; higher = better; empty
  query → `-len(candidate)`).
- Produces `filter_paths(query: str, paths: list[str]) -> list[str]` (ranked
  desc by score, ties by path; non-matches dropped).
- Produces `build_file_index(root: str | Path, skip_dirs: frozenset[str] = _FINDER_SKIP_DIRS) -> list[str]`
  (sorted project-relative POSIX paths; prunes `.git`, `node_modules`, `.venv`,
  `__pycache__`).

**Steps**

- [ ] Add `pygments` to the `gui` extra and install it.
  - Edit `pyproject.toml`:
    ```toml
    [project.optional-dependencies]
    dev = ["pytest", "pytest-qt"]
    gui = ["PySide6>=6.6", "pygments>=2.17"]
    ```
  - Run: `.venv/bin/pip install -e ".[gui,dev]"` (installs pygments). Expected:
    pygments resolves and installs; no other change.

- [ ] Write the failing pure test `tests/test_gui_files_pure.py`:
    ```python
    """Pure (Qt-free) tests for spar/gui/files.py — NO importorskip.

    These RUN, not skip, under a plain ``python3`` interpreter: the helpers
    live above the ``if _HAS_QT:`` guard (mirrors test_gui_orchestrator_pure).
    """
    from __future__ import annotations

    from spar.gui.files import (
        build_file_index,
        filter_paths,
        fuzzy_score,
        pick_lexer,
    )


    class TestPickLexer:
        def test_known_extension_picks_specific_lexer(self):
            assert "Python" in pick_lexer("a/b/thing.py").name

        def test_unknown_extension_falls_back_to_text(self):
            assert pick_lexer("mystery.zzz").name.lower() in ("text only", "text")


    class TestFuzzyScore:
        def test_non_subsequence_is_none(self):
            assert fuzzy_score("xyz", "spar/gui/app.py") is None

        def test_subsequence_matches(self):
            assert fuzzy_score("app", "spar/gui/app.py") is not None

        def test_empty_query_matches_everything(self):
            assert fuzzy_score("", "any/path.py") == -len("any/path.py")

        def test_basename_start_beats_midword(self):
            # "app" at the start of the basename must outrank "app" buried
            # mid-path.
            direct = fuzzy_score("app", "gui/app.py")
            buried = fuzzy_score("app", "grapple/xxx.py")
            assert direct is not None and buried is not None
            assert direct > buried

        def test_contiguous_beats_scattered(self):
            contig = fuzzy_score("abc", "abc.py")
            scattered = fuzzy_score("abc", "a_b_c.py")
            assert contig > scattered


    class TestFilterPaths:
        def test_drops_non_matches_and_ranks(self):
            paths = ["spar/gui/app.py", "spar/gui/files.py", "README.md"]
            out = filter_paths("app", paths)
            assert out == ["spar/gui/app.py"]

        def test_empty_query_returns_all_sorted_by_path(self):
            paths = ["b.py", "a.py"]
            out = filter_paths("", paths)
            assert out == ["a.py", "b.py"]


    class TestBuildFileIndex:
        def test_skips_noise_dirs_and_returns_relative_posix(self, tmp_path):
            (tmp_path / "spar" / "gui").mkdir(parents=True)
            (tmp_path / "spar" / "gui" / "app.py").write_text("x")
            (tmp_path / ".git").mkdir()
            (tmp_path / ".git" / "HEAD").write_text("ref")
            (tmp_path / "node_modules").mkdir()
            (tmp_path / "node_modules" / "dep.js").write_text("y")
            (tmp_path / "README.md").write_text("r")

            out = build_file_index(tmp_path)

            assert "spar/gui/app.py" in out
            assert "README.md" in out
            assert not any(p.startswith(".git/") for p in out)
            assert not any(p.startswith("node_modules/") for p in out)
            assert out == sorted(out)
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files_pure.py -q`
  - Expected: **collection error / ImportError** (`spar.gui.files` does not
    exist yet).

- [ ] Implement the pure section of `spar/gui/files.py`:
    ```python
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
        pass  # Qt widgets added in Tasks 2–6.
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files_pure.py -q`
  - Expected: **all pass** (10 tests).

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected: **857 passed, 2 skipped** (baseline 847 + 10 new).

- [ ] Commit:
  - `git add pyproject.toml spar/gui/files.py tests/test_gui_files_pure.py`
  - `git commit -m "feat(gui): files module pure helpers (lexer/fuzzy/index) + pygments dep"`

---

### Task 2: FileEditor — gutter, current-line highlight, Pygments highlighter, save/dirty, read-only, watcher (Opus)

The hardest widget: the gutter/highlighter/watcher interplay. Standalone —
testable without any of the view scaffolding.

**Files**
- `spar/gui/files.py` (add the Qt layer: `PygmentsHighlighter`,
  `_LineNumberArea`, `FileEditor`, plus `_build_token_formats`)
- `tests/test_gui_files.py` (new — `importorskip("PySide6")`)

**Interfaces**
- Consumes `pick_lexer` (Task 1) and `spar.gui.theme.TOKENS`.
- Produces `FileEditor(path: str | Path, parent=None)` with:
  - `load_from_disk() -> None`, `save() -> bool` (writes `toPlainText()`;
    surfaces `OSError` via `QMessageBox.critical` and returns `False`),
  - `is_dirty() -> bool` (`document().isModified()`),
  - `set_read_only(ro: bool) -> None`,
  - `reload_from_disk() -> None` (discards local edits, preserves scroll),
  - signals `disk_reloaded = Signal()` (clean auto-reload done),
    `disk_conflict = Signal()` (disk changed with local edits).

**Steps**

- [ ] Write the failing Qt test `tests/test_gui_files.py`:
    ```python
    from __future__ import annotations

    import pytest

    pytest.importorskip("PySide6")

    from spar.gui.files import FileEditor


    class TestFileEditor:
        def test_loads_file_text(self, qtbot, tmp_path):
            f = tmp_path / "a.py"
            f.write_text("x = 1\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            assert ed.toPlainText() == "x = 1\n"
            assert ed.is_dirty() is False

        def test_gutter_width_is_positive(self, qtbot, tmp_path):
            f = tmp_path / "a.py"
            f.write_text("x = 1\n" * 200, encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            assert ed._line_number_area_width() > 0
            # The gutter reserves left viewport margin equal to its width.
            assert ed.viewportMargins().left() == ed._line_number_area_width()

        def test_editing_marks_dirty(self, qtbot, tmp_path):
            f = tmp_path / "a.py"
            f.write_text("x = 1\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            # review #9: setPlainText() RESETS the document modified flag, so it
            # cannot prove "editing marks dirty". Type real keys instead — that
            # is a genuine user edit and sets the modified flag.
            ed.moveCursor(ed.textCursor().MoveOperation.End)
            qtbot.keyClicks(ed, "# note")
            assert ed.is_dirty() is True

        def test_save_writes_and_clears_dirty(self, qtbot, tmp_path):
            f = tmp_path / "a.py"
            f.write_text("x = 1\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            # review #9: setPlainText resets modified — force it so the save
            # path is exercised on a genuinely dirty buffer.
            ed.setPlainText("x = 2\n")
            ed.document().setModified(True)
            assert ed.save() is True
            assert f.read_text(encoding="utf-8") == "x = 2\n"
            assert ed.is_dirty() is False

        def test_save_failure_surfaces_message_and_returns_false(self, qtbot, tmp_path, monkeypatch):
            from PySide6.QtWidgets import QMessageBox

            f = tmp_path / "a.py"
            f.write_text("x = 1\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            ed.setPlainText("boom\n")
            ed.document().setModified(True)  # review #9

            shown = []
            monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: shown.append(1)))

            def _raise(*_a, **_k):
                raise OSError("disk full")

            monkeypatch.setattr("pathlib.Path.write_text", _raise)
            assert ed.save() is False
            assert shown == [1]

        def test_read_only_toggle(self, qtbot, tmp_path):
            f = tmp_path / "a.py"
            f.write_text("x\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            ed.set_read_only(True)
            assert ed.isReadOnly() is True
            ed.set_read_only(False)
            assert ed.isReadOnly() is False

        def test_save_refused_while_read_only(self, qtbot, tmp_path):
            # review #4: a buffer dirtied before the run must NOT be writable via
            # save() once the read-only matrix engages.
            f = tmp_path / "a.py"
            f.write_text("x = 1\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            ed.setPlainText("x = 2\n")
            ed.document().setModified(True)
            ed.set_read_only(True)
            assert ed.save() is False
            # Disk untouched; buffer still dirty (nothing was written).
            assert f.read_text(encoding="utf-8") == "x = 1\n"
            assert ed.is_dirty() is True

        def test_has_a_pygments_highlighter(self, qtbot, tmp_path):
            from spar.gui.files import PygmentsHighlighter

            f = tmp_path / "a.py"
            f.write_text("def f():\n    return 1\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            assert isinstance(ed._highlighter, PygmentsHighlighter)

        def test_disk_change_without_local_edits_auto_reloads(self, qtbot, tmp_path):
            # review #6: drive the REAL watcher (write to disk + waitUntil),
            # not a hand-called _on_file_changed.
            f = tmp_path / "a.py"
            f.write_text("one\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            reloaded = []
            ed.disk_reloaded.connect(lambda: reloaded.append(1))
            f.write_text("two\n", encoding="utf-8")
            qtbot.waitUntil(lambda: ed.toPlainText() == "two\n", timeout=3000)
            assert reloaded == [1]
            assert ed.is_dirty() is False

        def test_disk_change_with_local_edits_signals_conflict(self, qtbot, tmp_path):
            f = tmp_path / "a.py"
            f.write_text("one\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            # review #9: setPlainText resets modified — force dirty explicitly.
            ed.setPlainText("local edit\n")
            ed.document().setModified(True)
            conflicts = []
            ed.disk_conflict.connect(lambda: conflicts.append(1))
            with qtbot.waitSignal(ed.disk_conflict, timeout=3000):
                f.write_text("engine wrote this\n", encoding="utf-8")
            # No silent clobber: local edits preserved, conflict signalled.
            assert ed.toPlainText() == "local edit\n"
            assert conflicts == [1]

        def test_atomic_replace_via_real_watcher_reloads(self, qtbot, tmp_path):
            # review #6: an atomic replace (temp write + os.replace) makes the
            # path momentarily absent and DROPS the watch; the re-arm retry must
            # pick up the recreated file and reload it.
            import os

            f = tmp_path / "a.py"
            f.write_text("one\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            tmp = tmp_path / "a.py.tmp"
            tmp.write_text("replaced\n", encoding="utf-8")
            os.replace(tmp, f)  # atomic rename over the watched path
            qtbot.waitUntil(lambda: ed.toPlainText() == "replaced\n", timeout=3000)
            assert ed.is_dirty() is False

        def test_delete_then_recreate_detected(self, qtbot, tmp_path):
            # review #6: deletion drops the watch; recreation must still reload.
            f = tmp_path / "a.py"
            f.write_text("one\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            f.unlink()
            # review #12: let Qt actually process the deletion first, THEN
            # recreate on a delayed timer — this exercises the bounded
            # absent-path re-arm poll instead of racing ahead of the event.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(300, lambda: f.write_text("reborn\n", encoding="utf-8"))
            qtbot.waitUntil(lambda: ed.toPlainText() == "reborn\n", timeout=4000)
            assert ed.is_dirty() is False

        def test_coalesced_duplicate_writes_reload_latest(self, qtbot, tmp_path):
            # review #6: rapid successive writes may coalesce into one signal;
            # the editor must still end up on the LAST on-disk content.
            f = tmp_path / "a.py"
            f.write_text("one\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            f.write_text("mid\n", encoding="utf-8")
            f.write_text("final\n", encoding="utf-8")
            qtbot.waitUntil(lambda: ed.toPlainText() == "final\n", timeout=3000)
            assert ed.is_dirty() is False

        def test_reload_from_disk_discards_local_edits(self, qtbot, tmp_path):
            f = tmp_path / "a.py"
            f.write_text("one\n", encoding="utf-8")
            ed = FileEditor(f)
            qtbot.addWidget(ed)
            ed.load_from_disk()
            ed.setPlainText("local\n")
            ed.document().setModified(True)  # review #9
            f.write_text("disk\n", encoding="utf-8")
            ed.reload_from_disk()
            assert ed.toPlainText() == "disk\n"
            assert ed.is_dirty() is False
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py -q`
  - Expected: **ImportError** (`FileEditor` not defined yet).

- [ ] Implement the Qt editor layer inside the `if _HAS_QT:` block of
  `spar/gui/files.py` (replace the `pass`):
    ```python
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
    ```
  - Note: `Qt`, `QTimer` and `QFileSystemWatcher` are all imported from
    `PySide6.QtCore` in the guard's import block above (review #1). `Qt` is
    used in `_paint_line_numbers`; `QTimer` in the watcher re-arm retry.
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py -q`
  - Expected: all listed `TestFileEditor` tests pass.

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected (review #11): the full baseline (847 passed, 2 skipped) still
    passes plus this task's new `TestFileEditor` tests — **no failures, no
    regressions**. (Do not assert a hard-coded intermediate total; several
    tasks add a variable number of tests.)

- [ ] Commit:
  - `git add spar/gui/files.py tests/test_gui_files.py`
  - `git commit -m "feat(gui): FileEditor with gutter, current-line, Pygments highlight, watcher"`

---

### Task 3: FilesView — tree + tabbed editor, open/focus/close, dirty prompt, read-only matrix (Opus)

Assembles the tree and tabs on top of `FileEditor`, adds the per-tab disk-change
banner (`EditorTab`) and the global read-only banner, and implements the
read-only matrix + unsaved-change prompts.

**Files**
- `spar/gui/files.py` (add `_TreeFilterProxy`, `EditorTab`, `FilesView`)
- `tests/test_gui_files.py` (append `TestFilesView`)

**Interfaces**
- Consumes `FileEditor`, `build_file_index` is NOT used here (finder only).
- Consumes `spar.gui.runner.RunnerState`.
- Produces `FilesView(project_dir: str | Path, parent=None)` with:
  - `open_file(path: str | Path) -> None` (double-click / finder entry point;
    re-open focuses the existing tab),
  - `set_state(state: RunnerState) -> None` (read-only matrix + banner + tab
    lock),
  - `has_unsaved() -> bool`,
  - `confirm_discard_if_dirty() -> bool` (aggregate Save/Discard/Cancel prompt;
    `True` = safe to proceed),
  - attribute `splitter: QSplitter`, `tabs: QTabWidget`, `tree: QTreeView`.
- Read-only set = `{RUNNING, GATE_PENDING, LOCKED}`; everything else editable
  (ABORTED resolves to editable — a stopped run cannot mutate the tree).

**Steps**

- [ ] Append the failing `TestFilesView` to `tests/test_gui_files.py`:
    ```python
    class TestFilesView:
        def _view(self, qtbot, tmp_path):
            from spar.gui.files import FilesView

            (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
            (tmp_path / ".git").mkdir()
            (tmp_path / ".git" / "HEAD").write_text("ref\n")
            (tmp_path / ".spar").mkdir()
            (tmp_path / ".spar" / "config.toml").write_text("# c\n")
            view = FilesView(tmp_path)
            qtbot.addWidget(view)
            return view

        def test_tree_hides_dot_git(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            root = view.tree.rootIndex()
            model = view.tree.model()
            # QFileSystemModel populates ASYNCHRONOUSLY (review #10): reading
            # rowCount() immediately is flaky. Wait until the root directory has
            # actually been listed (app.py + .spar → at least 2 visible rows).
            qtbot.waitUntil(lambda: model.rowCount(root) >= 2, timeout=3000)
            names = {
                model.index(r, 0, root).data()
                for r in range(model.rowCount(root))
            }
            assert ".git" not in names
            assert ".spar" in names  # shown (collapsed by default)
            assert "app.py" in names

        def test_open_file_adds_tab(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            assert view.tabs.count() == 1
            assert view.tabs.tabText(0) == "app.py"

        def test_reopen_focuses_existing_tab(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            (tmp_path / "b.py").write_text("y\n", encoding="utf-8")
            view.open_file(tmp_path / "app.py")
            view.open_file(tmp_path / "b.py")
            view.open_file(tmp_path / "app.py")  # already open
            assert view.tabs.count() == 2
            assert view.tabs.currentWidget().path.name == "app.py"

        def test_dirty_marker_in_tab_text(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            tab = view.tabs.currentWidget()
            # review #9: setPlainText resets the modified flag (and would fire
            # modificationChanged(False)); force the dirty state explicitly so
            # the label-refresh actually sees a modified document.
            tab.editor.setPlainText("x = 2\n")
            tab.editor.document().setModified(True)
            assert view.tabs.tabText(0).startswith("• ")

        def test_read_only_matrix_locks_editor_banner_and_tab(self, qtbot, tmp_path):
            from spar.gui.runner import RunnerState

            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            view.set_state(RunnerState.RUNNING)
            assert view.tabs.currentWidget().editor.isReadOnly() is True
            assert view.read_only_banner.isHidden() is False
            assert "🔒" in view.tabs.tabText(0)
            view.set_state(RunnerState.IDLE)
            assert view.tabs.currentWidget().editor.isReadOnly() is False
            assert view.read_only_banner.isHidden() is True
            assert "🔒" not in view.tabs.tabText(0)

        def test_close_clean_tab_removes_it(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            view._close_tab(0)
            assert view.tabs.count() == 0

        def test_close_dirty_tab_prompts_and_cancel_keeps_it(self, qtbot, tmp_path, monkeypatch):
            from PySide6.QtWidgets import QMessageBox

            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            ed = view.tabs.currentWidget().editor
            ed.setPlainText("dirty\n")
            ed.document().setModified(True)  # review #9
            monkeypatch.setattr(
                QMessageBox, "question",
                staticmethod(lambda *a, **k: QMessageBox.StandardButton.Cancel),
            )
            view._close_tab(0)
            assert view.tabs.count() == 1  # cancelled

        def test_confirm_discard_saves_all_on_save(self, qtbot, tmp_path, monkeypatch):
            from PySide6.QtWidgets import QMessageBox

            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            ed = view.tabs.currentWidget().editor
            ed.setPlainText("saved via prompt\n")
            ed.document().setModified(True)  # review #9
            monkeypatch.setattr(
                QMessageBox, "question",
                staticmethod(lambda *a, **k: QMessageBox.StandardButton.Save),
            )
            assert view.confirm_discard_if_dirty() is True
            assert (tmp_path / "app.py").read_text(encoding="utf-8") == "saved via prompt\n"
            assert view.has_unsaved() is False

        def test_confirm_discard_on_discard_reverts_buffers(self, qtbot, tmp_path, monkeypatch):
            from PySide6.QtWidgets import QMessageBox

            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            ed = view.tabs.currentWidget().editor
            ed.setPlainText("unwanted edit\n")
            ed.document().setModified(True)  # review #9
            monkeypatch.setattr(
                QMessageBox, "question",
                staticmethod(lambda *a, **k: QMessageBox.StandardButton.Discard),
            )
            assert view.confirm_discard_if_dirty() is True
            # review #5: Discard must actually revert — buffer reloaded from
            # disk, nothing written, no longer dirty, and a second call does not
            # re-prompt (nothing is dirty to prompt about).
            assert view.has_unsaved() is False
            assert ed.toPlainText() == "x = 1\n"
            assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 1\n"
            assert view.confirm_discard_if_dirty() is True

        def test_ctrl_s_saves_current_tab(self, qtbot, tmp_path):
            from PySide6.QtGui import QKeySequence

            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            ed = view.tabs.currentWidget().editor
            ed.setPlainText("via ctrl s\n")
            ed.document().setModified(True)  # review #9
            # review #2: the shortcut is bound to the platform Save sequence.
            assert view._save_shortcut.key() == QKeySequence(
                QKeySequence.StandardKey.Save
            )
            # review #13: deliver the REAL key chord through Qt so the
            # QShortcut connection itself is exercised, not _save_current().
            view.show()
            ed.setFocus()
            qtbot.keyClick(ed, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
            qtbot.waitUntil(
                lambda: (tmp_path / "app.py").read_text(encoding="utf-8")
                == "via ctrl s\n"
            )
            assert view.has_unsaved() is False

        def test_ctrl_s_noops_while_read_only(self, qtbot, tmp_path):
            from spar.gui.runner import RunnerState

            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            ed = view.tabs.currentWidget().editor
            ed.setPlainText("blocked\n")
            ed.document().setModified(True)  # review #9
            view.set_state(RunnerState.RUNNING)
            # review #4: Ctrl+S is a no-op while read-only — nothing written,
            # buffer stays dirty, the read-only banner is up.
            assert view._save_current() is False
            assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 1\n"
            assert view.has_unsaved() is True
            assert view.read_only_banner.isHidden() is False

        def test_read_only_close_prompt_omits_save(self, qtbot, tmp_path, monkeypatch):
            from PySide6.QtWidgets import QMessageBox
            from spar.gui.runner import RunnerState

            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            ed = view.tabs.currentWidget().editor
            ed.setPlainText("dirty\n")
            ed.document().setModified(True)  # review #9
            view.set_state(RunnerState.RUNNING)
            seen = {}

            def _q(parent, title, text, buttons, default):
                seen["buttons"] = buttons
                return QMessageBox.StandardButton.Cancel

            monkeypatch.setattr(QMessageBox, "question", staticmethod(_q))
            view._close_tab(0)
            # review #4: read-only close prompt offers Discard/Cancel only.
            assert not (seen["buttons"] & QMessageBox.StandardButton.Save)
            assert seen["buttons"] & QMessageBox.StandardButton.Discard

        def test_disk_conflict_shows_per_tab_banner_with_reload(self, qtbot, tmp_path):
            view = self._view(qtbot, tmp_path)
            view.open_file(tmp_path / "app.py")
            tab = view.tabs.currentWidget()
            tab.editor.setPlainText("local\n")
            tab.editor.document().setModified(True)  # review #9
            with qtbot.waitSignal(tab.editor.disk_conflict, timeout=3000):
                # write INSIDE the block so the real watcher signal can't fire
                # before we start waiting for it.
                (tmp_path / "app.py").write_text("engine\n", encoding="utf-8")
            assert tab.disk_banner.isHidden() is False
            tab._on_reload_clicked()  # the "Przeładuj" button
            assert tab.editor.toPlainText() == "engine\n"
            assert tab.disk_banner.isHidden() is True
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestFilesView -q`
  - Expected: **ImportError / AttributeError** (`FilesView` not defined).

- [ ] Implement `_TreeFilterProxy`, `EditorTab`, `FilesView` in the `if _HAS_QT:`
  block (append after `FileEditor`). Add the needed imports to the guard's
  import lists:
  `QDir`, `QSortFilterProxyModel`, `QSettings` (QtCore);
  `QKeySequence`, `QShortcut` (QtGui — for Ctrl+S, review #2);
  `QFileSystemModel`, `QHBoxLayout`, `QLabel`, `QPushButton`, `QSplitter`,
  `QTabWidget`, `QTreeView`, `QVBoxLayout` (QtWidgets); `RunnerState` from
  `spar.gui.runner`.
    ```python
        from PySide6.QtCore import QDir, QSettings, QSortFilterProxyModel
        from PySide6.QtGui import QKeySequence, QShortcut
        from PySide6.QtWidgets import (
            QFileSystemModel,
            QHBoxLayout,
            QLabel,
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
                self.model.setRootPath(str(self.project_dir))
                self.model.setFilter(
                    QDir.Filter.AllEntries | QDir.Filter.Hidden
                    | QDir.Filter.NoDotAndDotDot
                )
                self.proxy = _TreeFilterProxy(self)
                self.proxy.setSourceModel(self.model)
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
                self._save_shortcut.activated.connect(self._save_current)

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
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py -q`
  - Expected: all `TestFileEditor` and `TestFilesView` tests pass.

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected (review #11): the full baseline (847 passed, 2 skipped) still
    passes plus this task's new `TestFilesView` tests — **no failures, no
    regressions**. (No hard-coded intermediate total.)

- [ ] Commit:
  - `git add spar/gui/files.py tests/test_gui_files.py`
  - `git commit -m "feat(gui): FilesView tree + tabbed editor, dirty prompt, read-only matrix"`

---

### Task 4: Double-Shift file finder overlay (Sonnet)

Frameless overlay with a fuzzy-filtered list, plus the application-level
double-Shift detector. Self-contained widgets (wired into `MainWindow` in
Task 5).

**Files**
- `spar/gui/files.py` (add `FileFinderOverlay`, `DoubleShiftFilter`)
- `tests/test_gui_files.py` (append `TestFileFinder`, `TestDoubleShift`)

**Interfaces**
- Consumes `build_file_index`, `filter_paths` (Task 1).
- Produces `FileFinderOverlay(project_dir, parent=None)`:
  - `file_chosen = Signal(str)` (project-relative POSIX path),
  - `refresh_index(force: bool = False) -> None` (rebuild if stale > 5 s),
  - `popup() -> None` (rebuild-if-stale, clear query, center over parent, show,
    focus the line edit),
  - attributes `query: QLineEdit`, `list: QListView`.
- Produces `DoubleShiftFilter(parent=None)`:
  - `triggered = Signal()`,
  - `eventFilter(obj, event)` firing `triggered` on two bare Shift KeyPress
    within 400 ms with no intervening non-Shift key; ignores auto-repeat.

**Steps**

- [ ] Append failing tests to `tests/test_gui_files.py`:
    ```python
    class TestFileFinder:
        def test_filters_list_by_query(self, qtbot, tmp_path):
            from spar.gui.files import FileFinderOverlay

            (tmp_path / "spar").mkdir()
            (tmp_path / "spar" / "app.py").write_text("x")
            (tmp_path / "README.md").write_text("y")
            overlay = FileFinderOverlay(tmp_path)
            qtbot.addWidget(overlay)
            overlay.refresh_index(force=True)
            overlay.query.setText("app")
            model = overlay.list.model()
            rows = [model.index(r, 0).data() for r in range(model.rowCount())]
            assert rows == ["spar/app.py"]

        def test_enter_in_query_emits_relative_path(self, qtbot, tmp_path):
            # review #8: exercise Enter via REAL Qt delivery to the QLineEdit
            # (qtbot.keyClick), not by calling _accept_current() directly — the
            # bug is that the line edit consumes Return, so returnPressed is the
            # only reliable trigger.
            from PySide6.QtCore import Qt
            from spar.gui.files import FileFinderOverlay

            (tmp_path / "app.py").write_text("x")
            overlay = FileFinderOverlay(tmp_path)
            qtbot.addWidget(overlay)
            overlay.refresh_index(force=True)
            overlay.query.setText("app")
            chosen = []
            overlay.file_chosen.connect(chosen.append)
            overlay.list.setCurrentIndex(overlay.list.model().index(0, 0))
            qtbot.keyClick(overlay.query, Qt.Key.Key_Return)
            assert chosen == ["app.py"]

        def test_stale_index_rebuilds_on_popup(self, qtbot, tmp_path):
            from spar.gui.files import FileFinderOverlay

            overlay = FileFinderOverlay(tmp_path)
            qtbot.addWidget(overlay)
            overlay.refresh_index(force=True)
            (tmp_path / "new.py").write_text("z")
            overlay._indexed_at = 0.0  # force staleness
            overlay.refresh_index()
            assert "new.py" in overlay._index


    class TestDoubleShift:
        def test_two_bare_shifts_within_window_trigger(self, qtbot):
            from PySide6.QtCore import QEvent, Qt
            from PySide6.QtGui import QKeyEvent
            from spar.gui.files import DoubleShiftFilter

            filt = DoubleShiftFilter()
            fired = []
            filt.triggered.connect(lambda: fired.append(1))

            def shift():
                return QKeyEvent(
                    QEvent.Type.KeyPress, Qt.Key.Key_Shift,
                    Qt.KeyboardModifier.NoModifier,
                )

            filt._now = lambda: 0.0
            filt.eventFilter(None, shift())
            filt._now = lambda: 0.2  # 200 ms later
            filt.eventFilter(None, shift())
            assert fired == [1]

        def test_other_key_between_resets(self, qtbot):
            from PySide6.QtCore import QEvent, Qt
            from PySide6.QtGui import QKeyEvent
            from spar.gui.files import DoubleShiftFilter

            filt = DoubleShiftFilter()
            fired = []
            filt.triggered.connect(lambda: fired.append(1))
            filt._now = lambda: 0.0
            filt.eventFilter(None, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier))
            filt.eventFilter(None, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A, Qt.KeyboardModifier.NoModifier))
            filt._now = lambda: 0.2
            filt.eventFilter(None, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier))
            assert fired == []

        def test_too_slow_does_not_trigger(self, qtbot):
            from PySide6.QtCore import QEvent, Qt
            from PySide6.QtGui import QKeyEvent
            from spar.gui.files import DoubleShiftFilter

            filt = DoubleShiftFilter()
            fired = []
            filt.triggered.connect(lambda: fired.append(1))
            filt._now = lambda: 0.0
            filt.eventFilter(None, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier))
            filt._now = lambda: 1.0  # 1 s later, outside the 400 ms window
            filt.eventFilter(None, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier))
            assert fired == []

        def test_shift_with_other_modifier_held_does_not_trigger(self, qtbot):
            # review #7: two Shift presses while Ctrl is held (e.g. a
            # double-tapped Ctrl+Shift shortcut) must NOT open the finder — only
            # a BARE double Shift does.
            from PySide6.QtCore import QEvent, Qt
            from PySide6.QtGui import QKeyEvent
            from spar.gui.files import DoubleShiftFilter

            filt = DoubleShiftFilter()
            fired = []
            filt.triggered.connect(lambda: fired.append(1))

            def ctrl_shift():
                return QKeyEvent(
                    QEvent.Type.KeyPress, Qt.Key.Key_Shift,
                    Qt.KeyboardModifier.ControlModifier
                    | Qt.KeyboardModifier.ShiftModifier,
                )

            filt._now = lambda: 0.0
            filt.eventFilter(None, ctrl_shift())
            filt._now = lambda: 0.2
            filt.eventFilter(None, ctrl_shift())
            assert fired == []
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestFileFinder tests/test_gui_files.py::TestDoubleShift -q`
  - Expected: **ImportError** (`FileFinderOverlay` / `DoubleShiftFilter` missing).

- [ ] Implement in the `if _HAS_QT:` block. Add imports: `QEvent`,
  `QStringListModel`, `Qt` (already imported in Task 2 — ensure present),
  `time` (module import at top of file, above the guard, is fine); QtWidgets:
  `QLineEdit`, `QListView`. Also `QKeyEvent` is only needed in tests.
    ```python
        import time as _time
        from PySide6.QtCore import QEvent, QObject, QStringListModel
        from PySide6.QtWidgets import QLineEdit, QListView

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
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_files.py::TestFileFinder tests/test_gui_files.py::TestDoubleShift -q`
  - Expected: all `TestFileFinder` and `TestDoubleShift` tests pass.

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected (review #11): the full baseline (847 passed, 2 skipped) still
    passes plus this task's new `TestFileFinder` / `TestDoubleShift` tests —
    **no failures, no regressions**. (No hard-coded intermediate total.)

- [ ] Commit:
  - `git add spar/gui/files.py tests/test_gui_files.py`
  - `git commit -m "feat(gui): double-Shift fuzzy file finder overlay + app-level detector"`

---

### Task 5: Centre switch — QStackedWidget + left-rail radio state machine + run-start auto-switch + finder wiring (Opus)

The integration task: rewire `MainWindow` to a switched centre, replace the
disabled Pliki placeholder with two exclusive radio toggles, push `RunnerState`
into `FilesView`, auto-switch on run start, wire the finder, AND install the
pre-spawn unsaved-editor guard on every spawn initiator (review #3). Respects
the init-order constraint.

**Files**
- `spar/gui/app.py`
- `spar/gui/sidepane.py` (add a `GatePanel.preflight_resume` hook so the gate's
  own `runner.resume(...)` calls — accept / abort / extend / fix / remarks —
  also run the pre-spawn guard; review #3)
- `tests/test_gui_app.py` (update the one rail test that asserts the old
  placeholder; add centre-switch + matrix + finder + pre-spawn-guard tests)

**Interfaces**
- Consumes `FilesView`, `FileFinderOverlay`, `DoubleShiftFilter` from
  `spar.gui.files`.
- Produces on `MainWindow`: `centre_stack: QStackedWidget`, `files_view:
  FilesView`, `file_finder: FileFinderOverlay`; methods
  `_set_centre_view(key: str, persist: bool = True)`,
  `_on_left_rail_toggled(key: str, checked: bool)`, `_open_finder()`,
  `_on_finder_chosen(rel: str)`, `_ensure_editors_clean() -> bool`,
  `_on_resume()`.
- Adds `GatePanel.preflight_resume` (class attr, default `None`): a
  `Callable[[], bool]` invoked at the TOP of every gate action; when it returns
  `False` the resume is aborted (mirrors the existing `preflight_auto_exec`
  pattern).

**Spawn-path guard (review #3).** A run start flips the editor read-only, so any
buffer dirtied beforehand would be stranded (it cannot be saved during
RUNNING/GATE/LOCKED — review #4) and git dirty-tree preflights cannot see
unsaved editor text. `_on_started` fires AFTER `QProcess.start()`, so it cannot
prompt. Therefore a `_ensure_editors_clean()` save-or-cancel prompt runs BEFORE
every spawn, in each initiator:

| initiator | where the guard goes |
|-----------|----------------------|
| `_on_new_debate` | first line, before the dialog/spawn |
| `_on_chat_handoff` | first line, before the dialog/spawn |
| `_on_start_exec` | before `_commit_if_dirty()` (save buffers first, so the commit picks them up) |
| toolbar RESUME | new `_on_resume()` wrapper (replaces the inline `lambda`) |
| gate accept/abort/extend/fix/remarks | `GatePanel.preflight_resume` hook |

`_on_started` keeps ONLY the view switch (no prompt — the child is already
running).

**Steps**

- [ ] Update the existing rail test in `tests/test_gui_app.py`
  (`TestRailsLayout.test_has_left_and_right_rails`) — the disabled placeholder is
  replaced by two functional radio toggles per ADR 0006 (the only forced
  existing-test edit):
    ```python
    def test_has_left_and_right_rails(self, qtbot, tmp_path):
        from spar.gui.rails import IconRail
        window = MainWindow(tmp_path)
        qtbot.addWidget(window)
        rails = window.findChildren(IconRail)
        assert len(rails) == 2
        # ADR 0006: the left rail now carries two exclusive view toggles.
        assert set(window.left_rail.buttons) == {"stream", "files"}
        assert window.left_rail.buttons["files"].isEnabled() is True
        assert window.left_rail.buttons["stream"].isCheckable() is True
        assert set(window.right_rail.buttons) >= {"tasks", "chat", "gate"}
    ```

- [ ] Add the failing centre-switch tests to `tests/test_gui_app.py` (new class
  `TestCentreSwitch`):
    ```python
    class TestCentreSwitch:
        def test_centre_is_a_stack_with_stream_and_files(self, qtbot, tmp_path):
            from PySide6.QtWidgets import QStackedWidget
            from spar.gui.files import FilesView

            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            assert isinstance(window.centre_stack, QStackedWidget)
            assert window.centre_stack.widget(0) is window.stream_pane
            assert isinstance(window.centre_stack.widget(1), FilesView)

        def test_default_view_is_stream(self, qtbot, tmp_path):
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            assert window.centre_stack.currentIndex() == 0
            assert window.left_rail.buttons["stream"].isChecked() is True
            assert window.left_rail.buttons["files"].isChecked() is False

        def test_toggling_files_switches_and_is_exclusive(self, qtbot, tmp_path):
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window.left_rail.buttons["files"].setChecked(True)
            assert window.centre_stack.currentIndex() == 1
            assert window.left_rail.buttons["stream"].isChecked() is False
            assert window.left_rail.buttons["files"].isChecked() is True

        def test_unchecking_active_bounces_back(self, qtbot, tmp_path):
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            # Clicking the already-active Strumień toggle must not leave zero
            # active — exactly one is always on.
            window.left_rail.buttons["stream"].setChecked(False)
            assert window.left_rail.buttons["stream"].isChecked() is True

        def test_centre_view_persists_via_qsettings(self, qtbot, tmp_path):
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window.left_rail.buttons["files"].setChecked(True)
            assert window._settings.value("rails/centre_view") == "files"
            window2 = MainWindow(tmp_path)
            qtbot.addWidget(window2)
            assert window2.centre_stack.currentIndex() == 1

        def test_run_start_auto_switches_to_stream(self, qtbot, tmp_path):
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window.left_rail.buttons["files"].setChecked(True)
            assert window.centre_stack.currentIndex() == 1
            # runner.started (covers exec/resume/new-debate AND the gate
            # auto-exec chain) forces Strumień.
            window._on_started("python -m spar.cli exec --headless --quiet")
            assert window.centre_stack.currentIndex() == 0
            assert window.left_rail.buttons["stream"].isChecked() is True

        def test_state_changed_drives_files_read_only(self, qtbot, tmp_path):
            from spar.gui.runner import RunnerState

            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            calls = []
            window.files_view.set_state = lambda s: calls.append(s)
            window._on_state_changed(RunnerState.RUNNING)
            assert calls[-1] == RunnerState.RUNNING

        def test_switch_to_stream_with_unsaved_can_cancel(self, qtbot, tmp_path, monkeypatch):
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window.left_rail.buttons["files"].setChecked(True)
            # Pretend there are unsaved changes and the user cancels.
            window.files_view.has_unsaved = lambda: True
            window.files_view.confirm_discard_if_dirty = lambda: False
            window.left_rail.buttons["stream"].setChecked(True)
            # Cancel keeps Pliki active and on-screen.
            assert window.centre_stack.currentIndex() == 1
            assert window.left_rail.buttons["files"].isChecked() is True

        def test_finder_choice_opens_file_in_pliki(self, qtbot, tmp_path):
            _init_repo(tmp_path)
            (tmp_path / "hello.py").write_text("print(1)\n", encoding="utf-8")
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window._on_finder_chosen("hello.py")
            assert window.centre_stack.currentIndex() == 1
            assert window.files_view.tabs.count() == 1
            assert window.files_view.tabs.tabText(0) == "hello.py"

        def test_start_exec_aborts_when_unsaved_editors_cancelled(self, qtbot, tmp_path):
            # review #3: the pre-spawn guard runs BEFORE _commit_if_dirty and
            # before the process spawns; a Cancel aborts the whole start.
            _init_repo(tmp_path)
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window.files_view.confirm_discard_if_dirty = lambda: False  # user cancels
            spawned = []
            window.runner.start_exec = lambda: spawned.append(1)
            window._on_start_exec()
            assert spawned == []

        def test_resume_aborts_when_unsaved_editors_cancelled(self, qtbot, tmp_path):
            # review #3: the toolbar RESUME is wrapped in _on_resume, which runs
            # the guard before runner.resume.
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window.files_view.confirm_discard_if_dirty = lambda: False
            resumed = []
            window.runner.resume = lambda *a, **k: resumed.append(1)
            window._on_resume()
            assert resumed == []

        def test_new_debate_aborts_when_unsaved_editors_cancelled(self, qtbot, tmp_path):
            # review #3: guard is the first line of _on_new_debate, before the
            # repo/config preflight and before any dialog.
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            window.files_view.confirm_discard_if_dirty = lambda: False
            started = []
            window.runner.start_debate = lambda **k: started.append(1)
            window._on_new_debate()
            assert started == []

        def test_gate_resume_runs_pre_spawn_guard(self, qtbot, tmp_path):
            # review #3: the gate panel's own resume paths honour the guard via
            # GatePanel.preflight_resume (wired to _ensure_editors_clean).
            window = MainWindow(tmp_path)
            qtbot.addWidget(window)
            gate = window.side_pane.gate_panel
            assert gate.preflight_resume == window._ensure_editors_clean
            gate.preflight_resume = lambda: False  # veto
            resumed = []
            window.runner.resume = lambda *a, **k: resumed.append(1)
            gate._on_abort()
            assert resumed == []
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_app.py -q`
  - Expected: failures/errors on the new `TestCentreSwitch` and the updated rail
    test (attributes not present yet).

- [ ] Implement the wiring in `spar/gui/app.py`.
  - Add imports: `QStackedWidget` to the `PySide6.QtWidgets` import block; and
    at the top of the module, `from spar.gui.files import (DoubleShiftFilter,
    FileFinderOverlay, FilesView)`.
  - Build `FilesView` next to the stream pane (before the splitter is
    assembled), and construct the centre stack. Replace:
    ```python
    self.stream_pane = StreamPane(self)
    ```
    with:
    ```python
    self.stream_pane = StreamPane(self)
    # ADR 0006: the centre is a switched stack (Strumień | Pliki). FilesView
    # is built here — before the splitter and before the END-of-__init__
    # side_pane.refresh() — so _on_state_changed (via _sync_toolbar) can push
    # the read-only matrix into it (init-order constraint).
    self.files_view = FilesView(self.project_dir, self)
    ```
  - Replace the splitter assembly (the `self.splitter.addWidget(self.stream_pane)`
    line) with a stack:
    ```python
    from PySide6.QtWidgets import QStackedWidget  # (or add to the top import block)
    self.centre_stack = QStackedWidget(self)
    self.centre_stack.setObjectName("centreStack")
    self.centre_stack.addWidget(self.stream_pane)   # index 0 — Strumień
    self.centre_stack.addWidget(self.files_view)    # index 1 — Pliki

    self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
    self.splitter.setObjectName("mainSplitter")
    self.splitter.addWidget(self.centre_stack)
    self.splitter.addWidget(self.right_column)
    ```
    (Keep the existing `self.splitter.resize(...)` / `setSizes(...)` lines that
    follow.)
  - Replace the left-rail construction:
    ```python
    self.left_rail = IconRail(
        [
            RailButtonSpec("stream", "Strumień", "Podgląd przebiegu", icon="▤"),
            RailButtonSpec("files", "Pliki", "Przeglądaj i edytuj pliki", icon="🗀"),
        ],
        self,
    )
    ```
  - In the QSettings block (right after `self._settings = QSettings("spar",
    "gui")` and the splitter restores), restore + wire the centre view:
    ```python
    centre_view = self._settings.value("rails/centre_view", "stream", type=str)
    if centre_view not in ("stream", "files"):
        centre_view = "stream"
    self._set_centre_view(centre_view, persist=False)
    self.left_rail.toggled.connect(self._on_left_rail_toggled)
    ```
  - Add the finder + double-Shift detector near the tailer setup (end of
    `__init__`):
    ```python
    self.file_finder = FileFinderOverlay(self.project_dir, self)
    self.file_finder.file_chosen.connect(self._on_finder_chosen)
    self._double_shift = DoubleShiftFilter(self)
    self._double_shift.triggered.connect(self._open_finder)
    from PySide6.QtWidgets import QApplication
    QApplication.instance().installEventFilter(self._double_shift)
    ```
  - Add the methods:
    ```python
    def _set_centre_view(self, key: str, persist: bool = True) -> None:
        self.centre_stack.setCurrentIndex(0 if key == "stream" else 1)
        self.left_rail.set_checked("stream", key == "stream")
        self.left_rail.set_checked("files", key == "files")
        if persist:
            self._settings.setValue("rails/centre_view", key)

    def _on_left_rail_toggled(self, key: str, checked: bool) -> None:
        if not checked:
            # Exactly one view is always active: an attempt to uncheck the
            # active toggle bounces straight back.
            self.left_rail.set_checked(key, True)
            return
        if key == "stream" and self.files_view.has_unsaved():
            if not self.files_view.confirm_discard_if_dirty():
                # Cancelled: keep Pliki active.
                self._set_centre_view("files", persist=False)
                return
        self._set_centre_view(key)

    def _open_finder(self) -> None:
        self.file_finder.popup()

    def _on_finder_chosen(self, rel: str) -> None:
        self._set_centre_view("files")
        self.files_view.open_file(self.project_dir / rel)

    def _ensure_editors_clean(self) -> bool:
        """Pre-spawn guard (review #3): a run start flips the editor
        read-only, so any unsaved buffer would be stranded (unsavable during
        the run) and invisible to git dirty-tree preflights. Prompt
        save/discard/cancel BEFORE the spawn; return False (abort the spawn)
        only on Cancel. Invoked by EVERY spawn initiator — _on_started runs
        AFTER QProcess.start() and can no longer prompt."""
        return self.files_view.confirm_discard_if_dirty()

    def _on_resume(self) -> None:
        # Toolbar RESUME: guard unsaved editors, then resume (review #3).
        if not self._ensure_editors_clean():
            return
        self.runner.resume(None)
    ```
  - Wire the guard into the remaining spawn initiators:
    - `self.side_pane.gate_panel.preflight_auto_exec = self._commit_if_dirty`
      stays; ADD next to it, so gate resumes also run the guard:
      ```python
      self.side_pane.gate_panel.preflight_resume = self._ensure_editors_clean
      ```
    - Change the RESUME toolbar wiring from the inline lambda to the wrapper:
      ```python
      # was: actions[toolbar_mod.RESUME].triggered.connect(lambda: self.runner.resume(None))
      actions[toolbar_mod.RESUME].triggered.connect(self._on_resume)
      ```
    - In `_on_new_debate` and `_on_chat_handoff`, make the guard the FIRST line
      (before `_new_debate_preflight()` / the dialog), aborting on cancel:
      ```python
      def _on_new_debate(self) -> None:
          if not self._ensure_editors_clean():   # review #3
              return
          if not self._new_debate_preflight():
              return
          ... (rest unchanged) ...
      ```
      (same first-line guard in `_on_chat_handoff`).
    - In `_on_start_exec`, add the guard BEFORE `_commit_if_dirty()` so unsaved
      buffers are flushed to disk first and the commit picks them up:
      ```python
      def _on_start_exec(self) -> None:
          if not self._ensure_editors_clean():   # review #3
              return
          if not self._commit_if_dirty():
              return
          self.runner.start_exec()
      ```
  - In `spar/gui/sidepane.py`, add the `preflight_resume` hook to `GatePanel`
    and call it at the top of every resume action (mirrors the existing
    `preflight_auto_exec`):
    ```python
    class GatePanel(QWidget):
        preflight_auto_exec = None
        preflight_resume = None   # review #3: MainWindow sets _ensure_editors_clean

        def _guard_resume(self) -> bool:
            """Return False (abort) when the host's pre-spawn guard vetoes the
            resume (unsaved editor buffers, user cancelled)."""
            return self.preflight_resume is None or self.preflight_resume()

        def _on_accept(self, spec: ButtonSpec) -> None:
            if not self._guard_resume():
                return
            if spec.auto_exec and self.preflight_auto_exec is not None:
                if not self.preflight_auto_exec():
                    return
            self._disable_all()
            self._runner.resume("accept", auto_exec=spec.auto_exec)

        def _on_abort(self) -> None:
            if not self._guard_resume():
                return
            ... (existing disable + resume("abort")) ...

        def _on_extend(self, spin) -> None:
            if not self._guard_resume():
                return
            ... (existing disable + resume(f"extend:{spin.value()}")) ...

        def _on_fix(self) -> None:
            if not self._guard_resume():
                return
            ... (existing prompt + resume(f"fix:...")) ...

        def _on_remarks(self, text_edit) -> None:
            if not self._guard_resume():
                return
            ... (existing resume_with_remarks(...)) ...
    ```
    (Only the leading `_guard_resume()` check and the `preflight_resume`
    class attr are new; the existing bodies are unchanged.)
  - In `_on_started`, the auto-switch is the ONLY view change (no prompt — the
    child is already running):
    ```python
    def _on_started(self, cmd: str) -> None:
        # ADR 0006: any run start (exec/resume/new-debate AND the gate
        # auto-exec chain, all funnelled through runner.started) forces the
        # centre back to Strumień. A pending gate does NOT — it never reaches
        # here. The unsaved-editor prompt already happened pre-spawn
        # (review #3, _ensure_editors_clean); this method never prompts.
        self._set_centre_view("stream")
        self.statusBar().showMessage(f"uruchomiono: {cmd}")
        ... (rest unchanged) ...
    ```
  - In `_on_state_changed`, push the state into `FilesView` (add after the
    `apply_state` call):
    ```python
    self.files_view.set_state(state)
    ```
  - In `closeEvent`, guard on unsaved edits (very first lines), and remove the
    event filter during teardown:
    ```python
    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if not self.files_view.confirm_discard_if_dirty():
            event.ignore()
            return
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self._double_shift)
        self._save_splitter_state()
        ... (rest unchanged) ...
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_app.py -q`
  - Expected: **all pass** (existing + new `TestCentreSwitch`).

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected (review #11): the full baseline (847 passed, 2 skipped) still
    passes plus the new `TestCentreSwitch` tests (centre switch, matrix, finder
    AND the pre-spawn-guard cases); the updated rail test is a modification, not
    an addition — **no failures, no regressions**. (No hard-coded intermediate
    total.)

- [ ] Commit:
  - `git add spar/gui/app.py spar/gui/sidepane.py tests/test_gui_app.py`
  - `git commit -m "feat(gui): centre stack + left-rail view toggles, run-start auto-switch, finder wiring"`

---

### Task 6: Editor / files QSS polish (Sonnet)

Small, dedicated styling pass so the new widgets read as part of the dark
theme (banners, tree, tabs, finder) — colours strictly from `TOKENS`.

**Files**
- `spar/gui/theme.py` (extend `build_qss`)
- `tests/test_gui_app.py` (extend `TestTheme.test_build_qss_uses_only_token_colors`
  is already generic — no change needed; add one targeted assertion in a new
  test)

**Steps**

- [ ] Add a failing theme test to `tests/test_gui_app.py`:
    ```python
    def test_qss_styles_files_widgets(self):
        from spar.gui import theme

        qss = theme.build_qss()
        # The new Pliki widgets are themed (object names present).
        assert "#filesReadOnlyBanner" in qss
        assert "#diskBanner" in qss
        assert "#fileFinder" in qss
        # Still no ad-hoc colours.
        import re
        hex_literals = set(re.findall(r"#[0-9a-fA-F]{6}", qss))
        # Strip object-name selectors (they also start with '#') before the
        # colour check.
        colour_literals = {h for h in hex_literals if len(h) == 7 and all(
            c in "0123456789abcdefABCDEF" for c in h[1:])}
        assert colour_literals.issubset(set(theme.TOKENS.values()) | {
            h for h in hex_literals if h.lstrip('#').lower() not in
            {v.lstrip('#').lower() for v in theme.TOKENS.values()}
            and False})  # only TOKENS colours allowed
    ```
  - NOTE: `#name` object-name selectors are 5–20 chars, not 6-hex, so the
    existing `re.findall(r"#[0-9a-fA-F]{6}")` in
    `test_build_qss_uses_only_token_colors` will not mistake `#diskBanner` for a
    colour (it is not 6 hex chars). Keep that test as the authoritative colour
    guard; the new test only asserts the selectors exist. Simplify the new test
    to just the three `assert ... in qss` selector checks and drop the colour
    re-check (already covered).
  - Run: `.venv/bin/python -m pytest tests/test_gui_app.py::TestTheme -q`
  - Expected: **fail** (selectors absent).

- [ ] Extend `build_qss()` in `spar/gui/theme.py` — append before the closing
  `"""` of the returned f-string:
    ```python
        #filesTree {{
            background-color: {t['panel']};
            border: none;
        }}
        #filesTabs::pane {{
            border: 1px solid {t['line']};
        }}
        #filesReadOnlyBanner {{
            color: {t['warn']};
            background-color: {t['panel']};
            border: 1px solid {t['warn']};
            padding: 2px 6px;
        }}
        #diskBanner {{
            background-color: {t['panel-alt']};
            border: 1px solid {t['gate']};
        }}
        #fileFinder {{
            background-color: {t['panel']};
            border: 1px solid {t['line']};
        }}
        #finderList {{
            background-color: {t['panel']};
            color: {t['text']};
        }}
    ```
  - Run: `.venv/bin/python -m pytest tests/test_gui_app.py::TestTheme -q`
  - Expected: **pass**.

- [ ] Run the full suite: `.venv/bin/python -m pytest tests/ -q`
  - Expected (review #11): the full baseline (847 passed, 2 skipped) still
    passes plus the new theme test — **no failures, no regressions**. (No
    hard-coded intermediate total.)

- [ ] Commit:
  - `git add spar/gui/theme.py tests/test_gui_app.py`
  - `git commit -m "style(gui): theme the Pliki tree, tabs, banners and finder"`

---

### Task 7: Docs — README, HANDOFF, ADR 0006 status stamp (Haiku)

Pure documentation, no code. Lands in the same tranche per the
keep-README-current rule.

**Files**
- `README.md`
- `docs/HANDOFF.md`
- `docs/adr/0006-files-module-editor-and-search.md`

**Steps**

- [ ] Update `README.md`, `### spar gui (dashboard-pilot)` section:
  - Replace the sentence "The left rail holds a disabled **Pliki** placeholder
    for a future tranche." with:
    ```
    The left rail carries two exclusive view toggles — **Strumień** (▤, the
    live stream) and **Pliki** (🗀, a file browser + editor). Exactly one is
    active; starting or resuming a run (and the consensus auto-exec chain)
    auto-switches back to Strumień. A pending gate does not change the view.
    The active view persists across restarts (QSettings).
    ```
  - Add a new subsection after the rails paragraph:
    ```markdown
    **Pliki view.** A project tree (left) beside a tabbed editor (right).
    Double-click a file to open it in a tab; re-opening focuses the existing
    tab. The editor has line numbers, current-line highlight and Pygments
    syntax colouring (lexer picked by filename; unknown types show as plain
    text). Ctrl+S saves; closing a tab, switching away, or closing the window
    with unsaved changes prompts save/discard/cancel. While a run is live
    (RUNNING / gate pending / locked) the editor is **read-only** — a
    "run w toku — tylko podgląd" banner shows, tabs carry a 🔒, and files the
    engine rewrites on disk auto-reload when you have no local edits (a
    "plik zmienił się na dysku" banner with **Przeładuj** appears instead when
    you do, so nothing is silently clobbered).

    **Double Shift** opens a fuzzy file finder overlay — type part of a path,
    Enter (or double-click) opens it in the Pliki view, Esc closes.

    <!-- TODO: screenshot docs/img/gui-files.png po manualnym smoke -->
    ```
  - Do NOT add a broken image link — only the TODO comment above.

- [ ] Append a HANDOFF entry to `docs/HANDOFF.md` (new dated section at the top
  of the handoff log, matching the existing style), summarising: files module
  tranche A shipped — centre `QStackedWidget` (Strumień | Pliki) driven by two
  exclusive left-rail toggles (`rails/centre_view` in QSettings, run-start
  auto-switch via `runner.started`); `FilesView` = `QFileSystemModel` tree
  (hides `.git`, shows `.spar` collapsed, hidden files visible) + tabbed
  `FileEditor` (gutter, current-line highlight, Pygments highlighter, Ctrl+S,
  dirty marker, save-failure message); read-only matrix from `RunnerState`
  (RUNNING/GATE_PENDING/LOCKED) with `QFileSystemWatcher` auto-reload / conflict
  banner; double-Shift `FileFinderOverlay` (in-memory index, subsequence fuzzy
  scoring); Ctrl+S save + the pre-spawn unsaved-editor guard
  (`_ensure_editors_clean`) on every spawn path (debate/exec/resume/gate). New
  dep: `pygments` in the `gui` extra. State the suite result as "full baseline
  plus the tranche's new tests, no failures/regressions" (review #11 — do NOT
  quote a hard-coded total; use whatever `pytest -q` actually reports). Note the
  deferred tranche B (find-in-files, replace-in-files, Ctrl+F) + the git module.
  Mention the README screenshot TODO (`docs/img/gui-files.png`).

- [ ] Stamp `docs/adr/0006-files-module-editor-and-search.md` — under
  `## Status`, append a line:
    ```
    Tranche A implemented 2026-07-11 (view switch, Pygments editor,
    save/dirty, read-only matrix + auto-reload, double-Shift finder). Tranche B
    (find/replace in files, Ctrl+F) and the git module remain pending.
    ```

- [ ] No test run needed for doc-only changes, but confirm the suite is still
  green: `.venv/bin/python -m pytest tests/ -q` → full baseline plus every
  tranche-A test passes, **no failures, 2 skipped** (review #11: no hard-coded
  total).

- [ ] Commit:
  - `git add README.md docs/HANDOFF.md docs/adr/0006-files-module-editor-and-search.md`
  - `git commit -m "docs: files module tranche A — README, HANDOFF, ADR 0006 status stamp"`

---

## Review history

**Round 1 (codex gpt-5.6-sol): verdict CONTINUE.** Accepted #1–#11:

- **#1** — Task 2: moved `QFileSystemWatcher` (and `Qt`, `QTimer`) into the
  `PySide6.QtCore` guard import; dropped the stale "add Qt to QtCore" note.
- **#2** — Task 3: added a `QShortcut(StandardKey.Save)` bound to
  `FilesView._save_current()` plus `test_ctrl_s_saves_current_tab`.
- **#3** — Task 5: added `MainWindow._ensure_editors_clean()` invoked before
  EVERY spawn (new-debate, chat-handoff, exec, toolbar resume via `_on_resume`,
  and gate accept/abort/extend/fix/remarks via a new `GatePanel.preflight_resume`
  hook in `sidepane.py`); `_on_started` keeps only the view switch. Added
  four abort-on-cancel tests.
- **#4** — Tasks 2/3: `FileEditor.save()` refuses while read-only;
  `_save_current` no-ops with the banner; close/discard prompts offer
  Discard/Cancel only when read-only (`_dirty_prompt_buttons`). Added
  `test_save_refused_while_read_only`, `test_ctrl_s_noops_while_read_only`,
  `test_read_only_close_prompt_omits_save`.
- **#5** — Task 3: `confirm_discard_if_dirty()` now reverts on Discard (reloads
  each dirty tab from disk, drops vanished ones); added
  `test_confirm_discard_on_discard_reverts_buffers`.
- **#6** — Task 2: `_on_file_changed` re-arms via bounded `QTimer.singleShot`
  poll while the path is briefly absent; replaced synthetic tests with REAL
  watcher delivery (`qtbot.waitUntil`/`waitSignal`) covering atomic replace,
  delete+recreate and coalesced writes.
- **#7** — Task 4: `DoubleShiftFilter` requires bare Shift (rejects
  Ctrl/Alt/Meta); added `test_shift_with_other_modifier_held_does_not_trigger`.
- **#8** — Task 4: wired `query.returnPressed → _accept_current`; test now uses
  `qtbot.keyClick(query, Key_Return)` instead of calling `_accept_current()`.
- **#9** — Audited all dirty tests: replaced `setPlainText()` (which resets the
  modified flag) with `keyClicks` / explicit `document().setModified(True)`.
- **#10** — Task 3: `test_tree_hides_dot_git` waits (`qtbot.waitUntil`) for the
  async `QFileSystemModel` to populate before reading `rowCount()`.
- **#11** — Replaced hard-coded intermediate suite totals with the repo
  "baseline passes + new tests, no failures/regressions" convention.
</content>
</invoke>
- Round 2 (codex gpt-5.6-sol): verdict AGREE; accepted NICE #12 (delete-then-recreate test recreates via delayed QTimer so the absent-path re-arm poll is exercised) and NICE #13 (Ctrl+S test delivers the real key chord via qtbot.keyClick instead of calling _save_current()).
