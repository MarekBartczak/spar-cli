# Multi-Project GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run spar's GUI on N projects in parallel — one window = one project = one OS process — with an in-app project opener, per-project layout memory, and a second launch on the same project raising the existing window instead of opening a read-only twin.

**Architecture:** No process sharing and no tabs. A new pure-ish module `spar/gui/instances.py` owns four concerns: a stable per-project key (sha1 of the resolved path) used both as a QSettings prefix and as a QLocalServer name, the recent-projects list, the detached-spawn command for a new window (including the frozen macOS `open -n` path), and a `SingleInstanceGuard` that either claims the project's local socket or hands a `raise` request to the process that already holds it. `MainWindow` consumes the key for its layout state; `main_gui` consumes the guard before constructing any window.

**Tech Stack:** Python 3.13, PySide6 (QSettings, QLocalServer, QLocalSocket, QProcess.startDetached, QFileDialog, QToolButton/QMenu), pytest + pytest-qt.

## Global Constraints

- No cap on the number of concurrent windows/processes — no counter, no `max_instances` setting, no warning dialog.
- The engine is untouched. This tranche changes `spar/gui/*` and `packaging/macos/entrypoint.py` docs only; no change to `spar/state.py` locking, `spar/cli.py` exit codes, or adapter code.
- `RunnerState.LOCKED` behavior stays exactly as-is: it is the answer to a foreign *engine* holding `<project>/.spar/lock` (e.g. a headless CLI run), which is a different situation from a second GUI on the same project.
- QSettings organisation/application stays `QSettings("spar", "gui")` — do not introduce a second store.
- Per-project keys must be derived from `Path(project_dir).resolve()`, so `.`, `~/x/../x`, and a trailing slash all map to one window and one settings scope. Symlinks resolve too (`resolve()` already does).
- UI strings are Polish, matching the rest of the GUI (`Otwórz projekt…`, `Wybierz katalog projektu`).
- Tests run with `QT_QPA_PLATFORM=offscreen`; run the suite as `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -q`. On macOS prefix with `PATH="$PWD/.venv/bin:$PATH"`.
- Never spawn a real process in tests: `QProcess.startDetached` is always monkeypatched. Real `QLocalServer.listen` IS allowed (the name is derived from a unique `tmp_path`, so runs never collide), but every such test must `release()` in a `finally` — a leaked listener breaks later tests in the same process. The stale-socket branch is covered by injection, not by real sockets (see Task 5).
- No migration of pre-existing global layout keys (`mainSplitter/state`, `rails/*`, `files/tree_split`). After the upgrade the first launch of each project starts from defaults; layout state is cheap to re-establish and a migration would have to guess which project the old global layout belonged to. Stale global keys are left in place, unread (review #6).
- Suite baseline before this tranche: **1035 passed, 2 skipped**.

---

## File Structure

- **Create `spar/gui/instances.py`** — multi-instance plumbing: `project_key`, `scoped`, `recent_projects`/`push_recent_project`, `new_window_command`, `spawn_new_window`, `local_server_name`, `SingleInstanceGuard`. This is the only new module; it has no import dependency on `app.py` (so `app.py` can import it freely).
- **Create `tests/test_gui_instances.py`** — unit tests for the pure helpers and the guard (with a fake QLocalServer/QLocalSocket where needed).
- **Modify `spar/gui/app.py`** — `MainWindow.__init__` (settings scope + title + geometry restore), `_restore_*`/`_save_*` splitter helpers, `closeEvent` (geometry save + guard teardown), `Toolbar` (project button), `main_gui` (guard + recent-projects push).
- **Modify `spar/gui/files.py`** — `FilesView` `files/tree_split` becomes project-scoped; `SearchPanel` mask history and `SearchDialog` geometry stay global.
- **Modify `tests/test_gui_app.py`, `tests/test_gui_files.py`** — extend, keep the existing `_hermetic_qsettings` fixtures.
- **Modify `README.md`, `docs/HANDOFF.md`, create `docs/adr/0007-one-window-per-project.md`** — document the decision and the flow.

---

### Task 1: Per-project identity, settings scope, recent projects (Sonnet)

**Files:**
- Create: `spar/gui/instances.py`
- Test: `tests/test_gui_instances.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `project_key(project_dir: str | Path) -> str` — 12 hex chars, sha1 of the resolved absolute path.
  - `scoped(project_dir: str | Path, key: str) -> str` — `f"projects/{project_key(...)}/{key}"`.
  - `recent_projects() -> list[str]` — most-recent-first absolute paths, max 10, non-existent ones filtered out.
  - `push_recent_project(project_dir: str | Path) -> None` — dedups and truncates to 10.

- [ ] **Step 1: Write the failing test**

Create `tests/test_gui_instances.py`:

```python
"""Tests for spar.gui.instances — per-project identity and window plumbing."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from spar.gui import instances


@pytest.fixture(autouse=True)
def _hermetic_qsettings():
    from PySide6.QtCore import QSettings

    QSettings("spar", "gui").clear()
    yield


class TestProjectKey:
    def test_stable_for_the_same_directory(self, tmp_path):
        assert instances.project_key(tmp_path) == instances.project_key(tmp_path)

    def test_differs_between_directories(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        assert instances.project_key(a) != instances.project_key(b)

    def test_normalizes_relative_and_dotted_paths(self, tmp_path, monkeypatch):
        (tmp_path / "proj").mkdir()
        monkeypatch.chdir(tmp_path / "proj")
        # "." , "../proj" and the absolute path are ONE project.
        assert instances.project_key(".") == instances.project_key(tmp_path / "proj")
        assert instances.project_key("../proj") == instances.project_key(tmp_path / "proj")

    def test_is_short_hex(self, tmp_path):
        key = instances.project_key(tmp_path)
        assert len(key) == 12
        assert all(c in "0123456789abcdef" for c in key)


class TestScoped:
    def test_prefixes_with_project_key(self, tmp_path):
        key = instances.project_key(tmp_path)
        assert instances.scoped(tmp_path, "rails/centre_view") == (
            f"projects/{key}/rails/centre_view"
        )


class TestRecentProjects:
    def test_empty_by_default(self):
        assert instances.recent_projects() == []

    def test_push_puts_newest_first_and_dedups(self, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        instances.push_recent_project(a)
        instances.push_recent_project(b)
        instances.push_recent_project(a)
        assert instances.recent_projects() == [str(a.resolve()), str(b.resolve())]

    def test_caps_at_ten(self, tmp_path):
        for i in range(12):
            d = tmp_path / f"p{i}"
            d.mkdir()
            instances.push_recent_project(d)
        assert len(instances.recent_projects()) == 10
        assert instances.recent_projects()[0] == str((tmp_path / "p11").resolve())

    def test_drops_vanished_directories(self, tmp_path):
        gone = tmp_path / "gone"
        gone.mkdir()
        instances.push_recent_project(gone)
        gone.rmdir()
        assert instances.recent_projects() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_instances.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'spar.gui.instances'` (collection error).

- [ ] **Step 3: Write minimal implementation**

Create `spar/gui/instances.py`:

```python
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
from pathlib import Path

from PySide6.QtCore import QSettings

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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_instances.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add spar/gui/instances.py tests/test_gui_instances.py
git commit -m "feat(gui): per-project identity, settings scope, recent projects"
```

---

### Task 2: Per-project layout state in QSettings (Sonnet)

**Files:**
- Modify: `spar/gui/app.py` (`MainWindow.__init__` settings block ~line 267-300, `_restore_splitter_state`/`_save_splitter_state`/`_restore_right_split_state`/`_save_right_split_state` ~line 653-670, `_on_rail_toggled` ~line 409, `_set_centre_view` persist ~line 448, `closeEvent` ~line 674)
- Modify: `spar/gui/files.py` (`FilesView.__init__` `_settings` ~line 1188, `files/tree_split` restore/save ~line 1556-1564)
- Test: `tests/test_gui_app.py`, `tests/test_gui_files.py`

**Interfaces:**
- Consumes: `spar.gui.instances.scoped`, `spar.gui.instances.project_key` (Task 1).
- Produces: `MainWindow._skey(key: str) -> str` — the window's project-scoped QSettings key helper; `FilesView._skey(key: str) -> str` — same for the files module. Window geometry persists under the scoped key `window/geometry`.

Scoping decision (do NOT scope everything): project-scoped keys are
`mainSplitter/state`, `rails/right_split`, `rails/centre_view`,
`rails/tasks_visible`, `rails/chat_visible`, `files/tree_split`,
`window/geometry`. Global (deliberately shared across projects):
`files/mask_history`, `files/search_dialog_geometry`, `recent_projects`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gui_app.py`:

```python
class TestPerProjectSettings:
    """Layout state is remembered per project, not globally (ADR 0007)."""

    def test_centre_view_is_scoped_per_project(self, qtbot, tmp_path):
        from spar.gui.instances import scoped
        from PySide6.QtCore import QSettings

        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        win_a = MainWindow(a)
        qtbot.addWidget(win_a)
        win_a._set_centre_view("files")

        settings = QSettings("spar", "gui")
        assert settings.value(scoped(a, "rails/centre_view"), type=str) == "files"
        # Project B is untouched and still starts on the stream view.
        assert settings.value(scoped(b, "rails/centre_view")) is None
        win_b = MainWindow(b)
        qtbot.addWidget(win_b)
        assert win_b.centre_stack.currentIndex() == 0  # index 0 == Strumień

    def test_splitter_state_is_scoped_per_project(self, qtbot, tmp_path):
        from spar.gui.instances import scoped
        from PySide6.QtCore import QSettings

        a = tmp_path / "a"
        a.mkdir()
        win = MainWindow(a)
        qtbot.addWidget(win)
        win._save_splitter_state()
        settings = QSettings("spar", "gui")
        assert settings.value(scoped(a, "mainSplitter/state")) is not None
        assert settings.value("mainSplitter/state") is None  # no global leftovers

    def test_window_geometry_restored_per_project(self, qtbot, tmp_path):
        a = tmp_path / "a"
        a.mkdir()
        win = MainWindow(a)
        qtbot.addWidget(win)
        win.resize(1234, 777)
        win._save_window_geometry()
        reopened = MainWindow(a)
        qtbot.addWidget(reopened)
        assert reopened.size().width() == 1234
        assert reopened.size().height() == 777
```

Append to `tests/test_gui_files.py` (inside the existing FilesView test class
or as a new class next to it — it needs the same fixtures):

```python
class TestFilesViewPerProjectSettings:
    def test_split_state_saved_under_the_scoped_key(self, qtbot, tmp_path):
        from PySide6.QtCore import QSettings

        from spar.gui.files import FilesView
        from spar.gui.instances import scoped

        view = FilesView(tmp_path)
        qtbot.addWidget(view)
        view._save_split_state()
        settings = QSettings("spar", "gui")
        assert settings.value(scoped(tmp_path, "files/tree_split")) is not None
        assert settings.value("files/tree_split") is None

    def test_splitter_move_signal_saves_scoped(self, qtbot, tmp_path):
        """Review #9: the CONSTRUCTOR's splitterMoved wiring must hit the
        scoped key too — a directly-called helper proves nothing about the
        runtime path."""
        from PySide6.QtCore import QSettings

        from spar.gui.files import FilesView
        from spar.gui.instances import scoped

        view = FilesView(tmp_path)
        qtbot.addWidget(view)
        view.splitter.splitterMoved.emit(120, 1)
        settings = QSettings("spar", "gui")
        assert settings.value(scoped(tmp_path, "files/tree_split")) is not None
        assert settings.value("files/tree_split") is None

    def test_constructor_restores_from_the_scoped_key(self, qtbot, tmp_path):
        """The restore side is wired in the constructor (files.py:1262), so it
        must read the scoped key — otherwise layouts silently never come
        back."""
        from PySide6.QtCore import QSettings

        from spar.gui.files import FilesView
        from spar.gui.instances import scoped

        first = FilesView(tmp_path)
        qtbot.addWidget(first)
        first.splitter.setSizes([500, 100])
        first._save_split_state()
        saved = QSettings("spar", "gui").value(scoped(tmp_path, "files/tree_split"))
        assert saved is not None

        reopened = FilesView(tmp_path)
        qtbot.addWidget(reopened)
        assert reopened.splitter.saveState() == saved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_app.py -q -k PerProjectSettings tests/test_gui_files.py -k PerProjectSettings`
Expected: FAIL — `AttributeError: 'MainWindow' object has no attribute '_save_window_geometry'` and scoped-key assertions failing because values land under the global keys.

- [ ] **Step 3: Write minimal implementation**

In `spar/gui/app.py`, import the helper and add the scope method. After
`from spar.gui.files import ...` add:

```python
from spar.gui.instances import scoped
```

In `MainWindow.__init__`, right after `self._settings = QSettings("spar", "gui")`:

```python
        # ADR 0007: layout state belongs to THIS project, not to the app —
        # with several windows open, a global key means the last window
        # closed clobbers everyone else's layout.
        self._settings = QSettings("spar", "gui")
        self._restore_window_geometry()
```

and add the helpers next to the splitter ones:

```python
    def _skey(self, key: str) -> str:
        """Project-scoped QSettings key (ADR 0007)."""
        return scoped(self.project_dir, key)

    def _restore_window_geometry(self) -> None:
        geo = self._settings.value(self._skey("window/geometry"))
        if geo is not None:
            self.restoreGeometry(geo)

    def _save_window_geometry(self) -> None:
        self._settings.setValue(self._skey("window/geometry"), self.saveGeometry())
```

Then replace every scoped read/write in `MainWindow` with `self._skey(...)`:

```python
    def _restore_splitter_state(self) -> None:
        state = self._settings.value(self._skey("mainSplitter/state"))
        if state is not None:
            self.splitter.restoreState(state)

    def _save_splitter_state(self, *_args) -> None:
        self._settings.setValue(
            self._skey("mainSplitter/state"), self.splitter.saveState()
        )

    def _restore_right_split_state(self) -> None:
        state = self._settings.value(self._skey("rails/right_split"))
        if state is not None:
            self.right_column.splitter.restoreState(state)

    def _save_right_split_state(self, *_args) -> None:
        self._settings.setValue(
            self._skey("rails/right_split"), self.right_column.splitter.saveState()
        )
```

and in the three remaining call sites (`__init__` centre-view/rail restore,
`_on_rail_toggled`, `_set_centre_view`) swap the literals:

- `self._settings.value("rails/centre_view", "stream", type=str)` → `self._settings.value(self._skey("rails/centre_view"), "stream", type=str)`
- `self._settings.value("rails/tasks_visible", True, type=bool)` → `self._settings.value(self._skey("rails/tasks_visible"), True, type=bool)`
- `self._settings.value("rails/chat_visible", True, type=bool)` → `self._settings.value(self._skey("rails/chat_visible"), True, type=bool)`
- `self._settings.setValue(f"rails/{key}_visible", checked)` → `self._settings.setValue(self._skey(f"rails/{key}_visible"), checked)`
- `self._settings.setValue("rails/centre_view", key)` → `self._settings.setValue(self._skey("rails/centre_view"), key)`

In `closeEvent`, save geometry alongside the splitters:

```python
        self._save_window_geometry()
        self._save_splitter_state()
        self._save_right_split_state()
```

In `spar/gui/files.py`, add `from spar.gui.instances import scoped` to the
module imports, and in `FilesView` add the same helper plus named
restore/save methods (the current code inlines them):

Review #9: do NOT invent new method names. `FilesView` already has
`_restore_split_state()` / `_save_split_state()` (`spar/gui/files.py:1557`
and `:1562`), wired in the constructor at `:1262-1263`
(`self._restore_split_state()` and
`self.splitter.splitterMoved.connect(self._save_split_state)`). Keep both
names and that wiring untouched — only the KEY inside them changes, so the
runtime restore-on-construct and save-on-move paths are scoped too, not just
a directly-called helper:

```python
        def _skey(self, key: str) -> str:
            return scoped(self.project_dir, key)

        def _restore_split_state(self) -> None:
            state = self._settings.value(self._skey("files/tree_split"))
            if state is not None:
                self.splitter.restoreState(state)

        def _save_split_state(self, *_args) -> None:
            self._settings.setValue(
                self._skey("files/tree_split"), self.splitter.saveState()
            )
```

Leave
`files/mask_history` and `files/search_dialog_geometry` global — they are
user habits, not project layout.

- [ ] **Step 4: Update the existing tests that assert GLOBAL keys**

Review #5: four existing tests read the raw literal keys through
`window._settings` and WILL fail after scoping — they must be rewritten, not
worked around by keeping a global fallback. Each one gains
`from spar.gui.instances import scoped` and wraps the key:

- `tests/test_gui_app.py:79` — `window2._settings.value("mainSplitter/state")` → `window2._settings.value(scoped(tmp_path, "mainSplitter/state"))`
- `tests/test_gui_app.py:209` — `window._settings.value("rails/chat_visible")` → `window._settings.value(scoped(tmp_path, "rails/chat_visible"))`
- `tests/test_gui_app.py:241` — `window2._settings.value("rails/right_split")` → `window2._settings.value(scoped(tmp_path, "rails/right_split"))`
- `tests/test_gui_app.py:799` — `window._settings.value("rails/centre_view")` → `window._settings.value(scoped(tmp_path, "rails/centre_view"))`

Before editing, re-locate them (line numbers drift as tests are appended):

```bash
grep -n '_settings.value("' tests/test_gui_app.py tests/test_gui_files.py
```

Every hit on `mainSplitter/state`, `rails/*` or `files/tree_split` must be
scoped; hits on `files/mask_history` and `files/search_dialog_geometry` must
stay literal.

- [ ] **Step 5: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_app.py tests/test_gui_files.py -q`
Expected: PASS — the new scoping tests plus the four rewritten ones.

- [ ] **Step 6: Commit**

```bash
git add spar/gui/app.py spar/gui/files.py tests/test_gui_app.py tests/test_gui_files.py
git commit -m "feat(gui): scope window layout state per project"
```

---

### Task 3: Window title carries the parent path (Haiku)

**Files:**
- Modify: `spar/gui/app.py` (`MainWindow.__init__` line ~138)
- Create: helper `window_title(project_dir)` in `spar/gui/instances.py`
- Test: `tests/test_gui_instances.py`, `tests/test_gui_app.py`

**Interfaces:**
- Consumes: nothing from other tasks (uses `Path` only).
- Produces: `window_title(project_dir: str | Path) -> str` — `"spar — <name> (<parent>)"` where `<parent>` is the parent directory with `$HOME` collapsed to `~`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gui_instances.py`:

```python
class TestWindowTitle:
    def test_includes_name_and_parent(self, tmp_path):
        proj = tmp_path / "ai_fight"
        proj.mkdir()
        title = instances.window_title(proj)
        assert title.startswith("spar — ai_fight (")
        assert str(tmp_path) in title

    def test_collapses_home_to_tilde(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        proj = tmp_path / "P_PROJ" / "ai_fight"
        proj.mkdir(parents=True)
        assert instances.window_title(proj) == "spar — ai_fight (~/P_PROJ)"

    def test_root_level_project_has_no_empty_parens(self):
        assert instances.window_title("/") == "spar — /"
```

Append to `tests/test_gui_app.py`:

```python
class TestWindowTitleWiring:
    def test_main_window_uses_the_full_title(self, qtbot, tmp_path):
        from spar.gui.instances import window_title

        proj = tmp_path / "repo"
        proj.mkdir()
        win = MainWindow(proj)
        qtbot.addWidget(win)
        assert win.windowTitle() == window_title(proj)
        assert "repo" in win.windowTitle()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_instances.py -q -k WindowTitle tests/test_gui_app.py -k WindowTitleWiring`
Expected: FAIL — `AttributeError: module 'spar.gui.instances' has no attribute 'window_title'`.

- [ ] **Step 3: Write minimal implementation**

Add to `spar/gui/instances.py`:

```python
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
```

In `spar/gui/app.py`, import it (`from spar.gui.instances import scoped, window_title`)
and replace line ~138:

```python
        self.setWindowTitle(window_title(self.project_dir))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_instances.py tests/test_gui_app.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add spar/gui/instances.py spar/gui/app.py tests/test_gui_instances.py tests/test_gui_app.py
git commit -m "feat(gui): window title carries the project's parent path"
```

---

### Task 4: `Otwórz projekt…` — picker, recents, detached spawn (Sonnet)

**Files:**
- Modify: `spar/gui/instances.py` (add `new_window_command`, `spawn_new_window`)
- Modify: `spar/gui/app.py` (`Toolbar`, `MainWindow` wiring, `main_gui` recents push)
- Test: `tests/test_gui_instances.py`, `tests/test_gui_app.py`

**Interfaces:**
- Consumes: `recent_projects`, `push_recent_project` (Task 1).
- Produces:
  - `new_window_command(project_dir: str | Path) -> list[str]` — argv for a NEW detached GUI process on that project.
  - `spawn_new_window(project_dir: str | Path) -> bool` — `QProcess.startDetached` on that argv; returns success.
  - `Toolbar.project_button: QToolButton` with `objectName == "projectButton"` and a dropdown `QMenu`; `MainWindow.open_project(path)` and `MainWindow._rebuild_project_menu()`.

Spawn matrix (frozen bundles are the only tricky part — Finder/`open`
refuses a second instance of the same bundle unless `-n` is passed):

| context | argv |
|---------|------|
| normal Python install | `[sys.executable, "-m", "spar.cli", "gui", "--dir", <path>]` |
| frozen, macOS (`sys.frozen` and `sys.platform == "darwin"`) | `["open", "-n", "-a", <Spar.app>, "--args", "--dir", <path>]` |
| frozen, non-macOS | `[sys.executable, "--dir", <path>]` |

The bundle path is derived from `sys.executable`
(`…/Spar.app/Contents/MacOS/Spar` → `parents[2]`); if the executable is not
inside a `.app`, fall back to the frozen non-macOS form.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gui_instances.py`:

```python
class TestNewWindowCommand:
    def test_normal_install_spawns_module_entry_point(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instances.sys, "frozen", False, raising=False)
        cmd = instances.new_window_command(tmp_path)
        assert cmd[1:] == ["-m", "spar.cli", "gui", "--dir", str(tmp_path.resolve())]

    def test_frozen_macos_uses_open_dash_n(self, tmp_path, monkeypatch):
        bundle = tmp_path / "Spar.app"
        exe = bundle / "Contents" / "MacOS" / "Spar"
        exe.parent.mkdir(parents=True)
        exe.touch()
        monkeypatch.setattr(instances.sys, "frozen", True, raising=False)
        monkeypatch.setattr(instances.sys, "executable", str(exe))
        monkeypatch.setattr(instances.sys, "platform", "darwin")
        proj = tmp_path / "proj"
        proj.mkdir()
        assert instances.new_window_command(proj) == [
            "open", "-n", "-a", str(bundle), "--args", "--dir", str(proj.resolve()),
        ]

    def test_frozen_non_macos_reinvokes_the_executable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(instances.sys, "frozen", True, raising=False)
        monkeypatch.setattr(instances.sys, "executable", "/opt/spar/spar")
        monkeypatch.setattr(instances.sys, "platform", "linux")
        cmd = instances.new_window_command(tmp_path)
        assert cmd == ["/opt/spar/spar", "--dir", str(tmp_path.resolve())]


class TestSpawnNewWindow:
    def test_starts_detached_with_the_command(self, tmp_path, monkeypatch):
        seen = {}

        def fake_detached(program, arguments, working_dir):
            seen["program"] = program
            seen["arguments"] = arguments
            seen["cwd"] = working_dir
            return True, 4242

        from PySide6.QtCore import QProcess

        monkeypatch.setattr(QProcess, "startDetached", staticmethod(fake_detached))
        monkeypatch.setattr(instances.sys, "frozen", False, raising=False)
        assert instances.spawn_new_window(tmp_path) is True
        assert seen["arguments"][-2:] == ["--dir", str(tmp_path.resolve())]
        assert seen["cwd"] == str(tmp_path.resolve())
```

Append to `tests/test_gui_app.py`:

```python
class TestOpenProjectAction:
    def test_toolbar_has_a_project_button_with_a_menu(self, qtbot, tmp_path):
        from PySide6.QtWidgets import QToolButton

        win = MainWindow(tmp_path)
        qtbot.addWidget(win)
        button = win.findChild(QToolButton, "projectButton")
        assert button is not None
        assert button.menu() is not None
        labels = [a.text() for a in button.menu().actions()]
        assert "Otwórz projekt…" in labels

    def test_open_project_spawns_a_detached_window_and_records_recent(
        self, qtbot, tmp_path, monkeypatch
    ):
        from spar.gui import app as app_mod
        from spar.gui.instances import recent_projects

        spawned = []
        monkeypatch.setattr(
            app_mod, "spawn_new_window", lambda p: spawned.append(str(p)) or True
        )
        other = tmp_path / "other"
        other.mkdir()
        win = MainWindow(tmp_path)
        qtbot.addWidget(win)
        win.open_project(other)
        assert spawned == [str(other)]
        assert str(other.resolve()) in recent_projects()

    def test_menu_lists_recent_projects_and_opens_them(self, qtbot, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QToolButton

        from spar.gui import app as app_mod
        from spar.gui.instances import push_recent_project

        spawned = []
        monkeypatch.setattr(
            app_mod, "spawn_new_window", lambda p: spawned.append(str(p)) or True
        )
        other = tmp_path / "other"
        other.mkdir()
        push_recent_project(other)
        win = MainWindow(tmp_path)
        qtbot.addWidget(win)
        button = win.findChild(QToolButton, "projectButton")
        recent_action = [
            a for a in button.menu().actions() if a.data() == str(other.resolve())
        ]
        assert len(recent_action) == 1
        recent_action[0].trigger()
        assert spawned == [str(other.resolve())]

    def test_current_project_is_not_offered_in_recents(self, qtbot, tmp_path):
        from PySide6.QtWidgets import QToolButton

        from spar.gui.instances import push_recent_project

        push_recent_project(tmp_path)
        win = MainWindow(tmp_path)
        qtbot.addWidget(win)
        button = win.findChild(QToolButton, "projectButton")
        data = [a.data() for a in button.menu().actions()]
        assert str(tmp_path.resolve()) not in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_instances.py -q -k "NewWindowCommand or SpawnNewWindow" tests/test_gui_app.py -k OpenProjectAction`
Expected: FAIL — missing `new_window_command` / `spawn_new_window`, and `findChild(QToolButton, "projectButton")` returning `None`.

- [ ] **Step 3: Write minimal implementation**

Add to `spar/gui/instances.py` (module imports gain `import sys` and
`from PySide6.QtCore import QProcess, QSettings`):

```python
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
```

In `spar/gui/app.py`: import
`from spar.gui.instances import (push_recent_project, recent_projects, scoped, spawn_new_window, window_title)`
and add `QFileDialog`, `QMenu`, `QToolButton` to the `QtWidgets` import list.

In `Toolbar.__init__`, after the placeholder actions:

```python
        # ADR 0007: one window per project — this button opens ANOTHER
        # project in a NEW process, never swaps the project under this one.
        self.project_button = QToolButton(self)
        self.project_button.setObjectName("projectButton")
        self.project_button.setText("Projekt")
        self.project_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.project_button.setMenu(QMenu(self.project_button))
        self.addWidget(self.project_button)
```

In `MainWindow.__init__`, after `self.addToolBar(self.toolbar)`:

```python
        self._rebuild_project_menu()
```

and add the methods:

```python
    def _rebuild_project_menu(self) -> None:
        """Populate the Projekt dropdown: picker + recent projects."""
        menu = self.toolbar.project_button.menu()
        menu.clear()
        pick = menu.addAction("Otwórz projekt…")
        pick.triggered.connect(self._pick_project)
        current = str(Path(self.project_dir).resolve())
        others = [p for p in recent_projects() if p != current]
        if others:
            menu.addSeparator()
            for path in others:
                action = menu.addAction(window_title(path).removeprefix("spar — "))
                action.setData(path)
                action.triggered.connect(
                    lambda _checked=False, p=path: self.open_project(p)
                )

    def _pick_project(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Wybierz katalog projektu",
            str(Path(self.project_dir).parent),
            QFileDialog.Option.ShowDirsOnly,
        )
        if selected:
            self.open_project(selected)

    def open_project(self, project_dir: "str | Path") -> None:
        """Open ``project_dir`` in a NEW window (a new OS process)."""
        push_recent_project(project_dir)
        if not spawn_new_window(project_dir):
            QMessageBox.warning(
                self,
                "spar",
                f"Nie udało się otworzyć nowego okna dla:\n{project_dir}",
            )
            return
        self._rebuild_project_menu()
```

In `main_gui`, record the project as recent so sibling windows can offer it:

```python
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(build_qss())

    push_recent_project(project_dir)
    window = MainWindow(project_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_instances.py tests/test_gui_app.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add spar/gui/instances.py spar/gui/app.py tests/test_gui_instances.py tests/test_gui_app.py
git commit -m "feat(gui): Projekt menu opens another project in a new window"
```

---

### Task 5: Single instance per project — raise the existing window (Opus)

**Files:**
- Modify: `spar/gui/instances.py` (add `local_server_name`, `SingleInstanceGuard`)
- Modify: `spar/gui/app.py` (`main_gui` guard flow, `MainWindow.closeEvent` teardown, `MainWindow.raise_to_front`)
- Test: `tests/test_gui_instances.py`, `tests/test_gui_app.py`

**Interfaces:**
- Consumes: `project_key` (Task 1), `MainWindow` (existing).
- Produces:
  - `local_server_name(project_dir) -> str` — `f"spar-gui-{project_key(...)}"` (short, so the AF_UNIX 104-byte path limit is never in play).
  - `class SingleInstanceGuard(QObject)` with `raise_requested = Signal()`, `try_claim() -> bool`, `attach(receiver) -> None`, `request_raise(timeout_ms: int = 1000) -> bool`, `release() -> None`.

**Contract (review #7 — exactly one raise per launch):** `try_claim()` owns
the whole decision INCLUDING delivering the raise request. `try_claim()
== False` means "an owner exists and has already been asked to raise its
window" — the caller must then just exit 0 and MUST NOT call
`request_raise()` again. `request_raise()` stays public only for the guard's
own internal use and for the no-owner unit test; nothing in `app.py` calls
it directly.

Claim protocol, in this order:

1. `listen(name)`. Success → we own the slot, `try_claim()` True. (Listen
   first, not probe first: on the common single-instance launch this is one
   syscall and no connect attempt at all.)
2. `listen` failing with anything OTHER than `AddressInUseError` → warn on
   stderr and return True anyway; a broken socket layer must never make the
   GUI unlaunchable.
3. `AddressInUseError` means either a live owner or a socket file left by a
   crashed process. Probe with `request_raise()`: delivered → a live owner
   now has the raise request, return False (caller exits). Refused → the
   name is stale, so `QLocalServer.removeServer(name)` and `listen` again.
4. If that second `listen` also fails, warn on stderr and return True (open
   the window without single-instance protection).

**Buffered delivery (review #8):** the guard starts listening BEFORE
`MainWindow` exists, and window construction is not event-loop-quiet —
`FilesView._wait_dir_loaded()` pumps `AllEvents` (`spar/gui/files.py:1343`),
so an incoming raise can be processed while no slot is connected yet. Since
the contract forbids a second request, that raise would be silently lost.
Therefore the guard buffers: before anything is attached, an arriving raise
only sets `_pending_raise`; `attach(receiver)` connects the receiver and
replays a buffered raise exactly once. `main_gui` must call
`guard.attach(window.raise_to_front)` instead of connecting the signal
directly.

Remaining race after that: only the probe timeout — a live-but-hung owner
that cannot answer within it gets its name taken and the user ends up with
two windows. That is the deliberate trade; the alternative is refusing to
launch while a hung process holds the name.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_gui_instances.py`:

```python
class TestLocalServerName:
    def test_derives_from_project_key(self, tmp_path):
        assert instances.local_server_name(tmp_path) == (
            f"spar-gui-{instances.project_key(tmp_path)}"
        )

    def test_short_enough_for_af_unix(self, tmp_path):
        assert len(instances.local_server_name(tmp_path)) <= 40


class TestSingleInstanceGuard:
    def test_first_claim_wins(self, qtbot, tmp_path):
        guard = instances.SingleInstanceGuard(tmp_path)
        try:
            assert guard.try_claim() is True
        finally:
            guard.release()

    def test_second_claim_fails_and_delivers_exactly_one_raise(self, qtbot, tmp_path):
        """Review #7: try_claim() itself delivers the raise — the caller must
        NOT send a second one, or the owner's window bounces twice."""
        owner = instances.SingleInstanceGuard(tmp_path)
        second = instances.SingleInstanceGuard(tmp_path)
        raised = []
        owner.attach(lambda: raised.append(True))
        try:
            assert owner.try_claim() is True
            assert second.try_claim() is False
            qtbot.waitUntil(lambda: raised == [True], timeout=2000)
            # Give a stray second delivery a chance to show up, then prove
            # there was none.
            qtbot.wait(100)
            assert raised == [True]
        finally:
            second.release()
            owner.release()

    def test_raise_arriving_before_attach_is_replayed_once(self, qtbot, tmp_path):
        """Review #8: the guard listens before MainWindow exists, and window
        construction pumps the event loop (FilesView._wait_dir_loaded), so a
        raise can land with no receiver attached. It must be buffered and
        replayed by attach(), exactly once."""
        owner = instances.SingleInstanceGuard(tmp_path)
        second = instances.SingleInstanceGuard(tmp_path)
        try:
            assert owner.try_claim() is True
            assert second.try_claim() is False  # delivers the raise now
            # Simulate "still constructing the window": pump events with NO
            # receiver attached, exactly what FilesView does.
            qtbot.wait(200)
            raised = []
            owner.attach(lambda: raised.append(True))
            assert raised == [True]  # replayed, not lost
            qtbot.wait(100)
            assert raised == [True]  # and not replayed twice
        finally:
            second.release()
            owner.release()

    def test_different_projects_both_claim(self, qtbot, tmp_path):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()
        ga, gb = instances.SingleInstanceGuard(a), instances.SingleInstanceGuard(b)
        try:
            assert ga.try_claim() is True
            assert gb.try_claim() is True
        finally:
            ga.release()
            gb.release()

    def test_release_frees_the_name_for_a_later_claim(self, qtbot, tmp_path):
        first = instances.SingleInstanceGuard(tmp_path)
        assert first.try_claim() is True
        first.release()
        second = instances.SingleInstanceGuard(tmp_path)
        try:
            assert second.try_claim() is True
        finally:
            second.release()

    def test_release_by_a_non_owner_does_not_free_the_owner(self, qtbot, tmp_path):
        """Review #4: release() must be a no-op for a guard that never
        claimed — otherwise the loser of the race removes the WINNER's
        socket name and the next launch opens a duplicate window."""
        owner = instances.SingleInstanceGuard(tmp_path)
        loser = instances.SingleInstanceGuard(tmp_path)
        try:
            assert owner.try_claim() is True
            assert loser.try_claim() is False
            loser.release()  # must not touch the owner's name
            third = instances.SingleInstanceGuard(tmp_path)
            assert third.try_claim() is False  # owner still owns it
        finally:
            loser.release()
            owner.release()

    def test_request_raise_without_owner_is_false(self, qtbot, tmp_path):
        guard = instances.SingleInstanceGuard(tmp_path)
        assert guard.request_raise(timeout_ms=200) is False


class TestSingleInstanceGuardBranches:
    """Deterministic coverage of try_claim()'s error branches via injected
    factories — review #2/#3: the stale-socket path CANNOT be simulated with
    a real QLocalServer, because `removeServer()` succeeds against a LIVE
    server on Linux and both processes then listen on one name (verified
    empirically). Only injection proves the branch order."""

    class FakeServer:
        """Minimal QLocalServer stand-in: `listen` outcomes are scripted."""

        def __init__(self, outcomes, error, log):
            self._outcomes = list(outcomes)
            self._error = error
            self._log = log
            self.newConnection = _FakeSignal()
            self.closed = False

        def listen(self, name):
            self._log.append(("listen", name))
            return self._outcomes.pop(0)

        def serverError(self):
            return self._error

        def close(self):
            self.closed = True

    def _guard(self, tmp_path, outcomes, error, log, raise_delivered):
        from PySide6.QtNetwork import QAbstractSocket

        server = TestSingleInstanceGuardBranches.FakeServer(outcomes, error, log)
        guard = instances.SingleInstanceGuard(
            tmp_path, server_factory=lambda _parent: server
        )
        guard.request_raise = lambda timeout_ms=1000: (
            log.append(("raise", timeout_ms)) or raise_delivered
        )
        return guard, server

    def test_first_listen_success_claims_without_probing(self, tmp_path):
        from PySide6.QtNetwork import QAbstractSocket

        log = []
        guard, _ = self._guard(
            tmp_path, [True], QAbstractSocket.SocketError.UnknownSocketError,
            log, raise_delivered=False,
        )
        assert guard.try_claim() is True
        assert [entry[0] for entry in log] == ["listen"]

    def test_address_in_use_with_live_owner_yields_the_slot(self, tmp_path):
        from PySide6.QtNetwork import QAbstractSocket

        log = []
        guard, _ = self._guard(
            tmp_path, [False], QAbstractSocket.SocketError.AddressInUseError,
            log, raise_delivered=True,
        )
        assert guard.try_claim() is False
        # Probed the owner, did NOT remove the name, did NOT listen again.
        assert [entry[0] for entry in log] == ["listen", "raise"]

    def test_address_in_use_with_dead_owner_removes_and_relistens(
        self, tmp_path, monkeypatch
    ):
        from PySide6.QtNetwork import QAbstractSocket, QLocalServer

        log = []
        removed = []
        monkeypatch.setattr(
            QLocalServer, "removeServer",
            staticmethod(lambda name: removed.append(name) or True),
        )
        guard, _ = self._guard(
            tmp_path, [False, True], QAbstractSocket.SocketError.AddressInUseError,
            log, raise_delivered=False,
        )
        assert guard.try_claim() is True
        assert removed == [instances.local_server_name(tmp_path)]
        assert [entry[0] for entry in log] == ["listen", "raise", "listen"]

    def test_unknown_listen_error_still_opens_the_window(self, tmp_path):
        """A broken socket layer must never make the gui unlaunchable."""
        from PySide6.QtNetwork import QAbstractSocket

        log = []
        guard, _ = self._guard(
            tmp_path, [False], QAbstractSocket.SocketError.SocketAccessError,
            log, raise_delivered=False,
        )
        assert guard.try_claim() is True
        assert [entry[0] for entry in log] == ["listen"]  # no probe, no remove
```

`_FakeSignal` is a two-line helper at module level in the same test file:

```python
class _FakeSignal:
    """Stand-in for a Qt signal on a fake object: records connections."""

    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)
```

Append to `tests/test_gui_app.py`:

```python
class TestSingleInstanceEntryPoint:
    def test_second_launch_raises_and_exits_without_a_window(self, tmp_path, monkeypatch):
        """A second `spar gui` on the SAME project must not build a window."""
        from spar.gui import app as app_mod

        class FakeGuard:
            instances_made = []

            def __init__(self, project_dir):
                self.project_dir = project_dir
                FakeGuard.instances_made.append(self)
                self.claims = 0
                self.extra_raises = 0

            def try_claim(self):
                # Review #7: the raise is delivered HERE, inside the claim.
                self.claims += 1
                return False

            def request_raise(self, timeout_ms=1000):
                self.extra_raises += 1  # main_gui must never reach this
                return True

            def release(self):
                pass

        built = []
        monkeypatch.setattr(app_mod, "SingleInstanceGuard", FakeGuard)
        monkeypatch.setattr(
            app_mod, "MainWindow", lambda *a, **kw: built.append(a) or pytest.fail(
                "second launch must not construct a MainWindow"
            )
        )
        rc = app_mod.main_gui(["--dir", str(tmp_path)])
        assert rc == 0
        assert built == []
        guard = FakeGuard.instances_made[-1]
        assert guard.claims == 1
        assert guard.extra_raises == 0  # no double raise from main_gui

    def test_raise_to_front_shows_and_activates(self, qtbot, tmp_path):
        win = MainWindow(tmp_path)
        qtbot.addWidget(win)
        win.showMinimized()
        win.raise_to_front()
        assert win.isMinimized() is False
        assert win.isVisible() is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_instances.py -q -k "LocalServerName or SingleInstanceGuard" tests/test_gui_app.py -k SingleInstanceEntryPoint`
Expected: FAIL — `AttributeError: module 'spar.gui.instances' has no attribute 'SingleInstanceGuard'` / no `raise_to_front`.

- [ ] **Step 3: Write minimal implementation**

Add to `spar/gui/instances.py` (imports gain
`from PySide6.QtCore import QObject, QProcess, QSettings, Signal` and
`from PySide6.QtNetwork import QAbstractSocket, QLocalServer, QLocalSocket`).

Two API facts verified against this venv's PySide6 — do NOT "fix" them to
the shapes that look more natural:

- `QLocalServer.serverError()` returns a **`QAbstractSocket.SocketError`**.
  There is no `QLocalServer.LocalSocketError`, and
  `QLocalSocket.LocalSocketError` has **no** `AddressInUseError` member. The
  only correct comparison is
  `server.serverError() == QAbstractSocket.SocketError.AddressInUseError`.
- `QLocalSocket.waitForBytesWritten()` returns **False** after a successful
  `flush()` — there is nothing left pending — even though the peer received
  the bytes (verified: `write()` → 6, `flush()` → True,
  `waitForBytesWritten()` → False, owner read `b"raise\n"`). Never use it as
  the delivery test; use `bytesToWrite() == 0` plus the byte count.
- `QLocalServer.removeServer(name)` succeeds even against a **live**
  listener, after which a second `listen(name)` also succeeds and BOTH
  servers report `isListening() == True`. That is why the protocol must
  probe with `request_raise()` *before* removing, and why the probe timeout
  is the safety margin: a live-but-hung owner that fails to answer within
  it gets its name stolen and the user ends up with two windows. 1000 ms is
  the chosen margin (a responsive Qt event loop answers in single-digit
  ms); do not lower it to make tests faster — the branch tests inject
  instead of waiting.

```python
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
        # Review #8: raises can arrive while MainWindow is still being built
        # (FilesView pumps the event loop), i.e. before any receiver exists.
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
        during window construction is delivered here instead of being lost
        (review #8).
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
        duplicate window (review #4).
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
        # Review #10: do NOT use waitForBytesWritten() as delivery proof.
        # Verified in this venv: after a successful flush() it returns False
        # (nothing left pending) even though the owner DID receive the
        # message. Taking that as "no owner" would remove a live owner's
        # socket name and open a duplicate window. The real signal is: the
        # connect succeeded, the whole message was written, and the send
        # queue is empty.
        if sock.bytesToWrite() > 0:
            sock.waitForBytesWritten(timeout_ms)
        delivered = written == len(_RAISE_MESSAGE) and sock.bytesToWrite() == 0
        sock.disconnectFromServer()
        return bool(delivered)
```

In `spar/gui/app.py`, extend the instances import with `SingleInstanceGuard`
and add to `MainWindow`:

```python
    def raise_to_front(self) -> None:
        """Bring this project's window forward (second-launch request)."""
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        self.raise_()
        self.activateWindow()
```

In `closeEvent`, before `super().closeEvent(event)`:

```python
        guard = getattr(self, "_instance_guard", None)
        if guard is not None:
            guard.release()
```

Rewrite `main_gui`:

```python
def main_gui(argv: list[str]) -> int:
    """Entry point for the ``spar gui`` subcommand."""
    args = _parse_args(argv)
    project_dir = Path(args.project_dir).resolve() if args.project_dir else Path.cwd()

    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setStyleSheet(build_qss())

    # ADR 0007: one window per project. A second launch on the same
    # directory raises the running window and exits instead of opening a
    # read-only twin.
    # try_claim() has already asked the owner to raise its window when it
    # returns False (review #7) — do NOT send a second request here.
    guard = SingleInstanceGuard(project_dir)
    if not guard.try_claim():
        return 0

    push_recent_project(project_dir)
    window = MainWindow(project_dir)
    window._instance_guard = guard
    # attach(), not a bare connect(): window construction pumps the event
    # loop, so a raise may already be buffered (review #8).
    guard.attach(window.raise_to_front)
    window.show()

    return app.exec()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/test_gui_instances.py tests/test_gui_app.py -q`
Expected: PASS.

- [ ] **Step 5: Run the whole suite**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -q`
Expected: PASS — at least 1035 passed, 2 skipped plus the new tests.

- [ ] **Step 6: Commit**

```bash
git add spar/gui/instances.py spar/gui/app.py tests/test_gui_instances.py tests/test_gui_app.py
git commit -m "feat(gui): one window per project, second launch raises it"
```

---

### Task 6: Docs — ADR 0007, README, HANDOFF (Sonnet)

**Files:**
- Create: `docs/adr/0007-one-window-per-project.md`
- Modify: `README.md` (the `spar gui` section)
- Modify: `docs/HANDOFF.md` (turn the "Next up" section into a landed entry)

**Interfaces:**
- Consumes: the shipped behavior from Tasks 1-5.
- Produces: nothing code-facing.

- [ ] **Step 1: Write the ADR**

Create `docs/adr/0007-one-window-per-project.md` following the shape of
`docs/adr/0006-files-module-editor-and-search.md` (Status / Context /
Decision / Consequences). Content requirements:

- **Status:** Accepted 2026-07-28, implemented.
- **Context:** parallel work on 2-3+ projects; the engine is already a
  per-project subprocess with a per-project `flock`.
- **Decision, numbered:** (1) one window = one project = one OS process, no
  instance cap; (2) tabs/single-process rejected — N engines and N live.log
  tailers on one Qt event loop, plus a rewrite of all singleton-per-window
  state; note that IntelliJ hosts multiple project frames in one JVM and we
  copy its UX, not its implementation; (3) layout state is QSettings-scoped
  per project (`projects/<sha1[:12]>/…`), while search/mask history and the
  recent-projects list stay global; (4) a second launch on the same
  directory raises the running window via a per-project QLocalServer and
  exits 0; (5) `RunnerState.LOCKED` is unchanged and still covers a foreign
  *engine* holding `.spar/lock`; (6) new windows are spawned detached, with
  `open -n` for the frozen macOS bundle.
- **Consequences:** no cross-project aggregate view (a status "hub" is a
  possible later addition, deliberately out of scope); a stale socket file
  after a crash is reclaimed on the next launch; each window keeps its own
  geometry and splitter layout.

- [ ] **Step 2: Update README**

In the `spar gui` section document, with a short example block: running the
GUI on several projects at once (`spar gui --dir ~/a` and `spar gui --dir ~/b`
in parallel), the `Projekt` toolbar menu (picker + recent projects, opens a
NEW window), that a second launch on the same project raises the existing
window, and that per-project layout is remembered separately.

- [ ] **Step 3: Update HANDOFF**

Replace the `## Next up: multi-project GUI (decided 2026-07-28)` section with
a landed entry: commit range, what shipped per task, the ADR reference, the
new suite total from Task 5 Step 5, and any remaining manual-smoke item
(multi-window smoke on the real GUI is user-driven).

- [ ] **Step 4: Verify docs match the code**

Run: `QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest tests/ -q`
Expected: PASS. Then re-read the ADR against `spar/gui/instances.py` and
confirm every claim (key format, socket name, spawn matrix) is literally
what the code does.

- [ ] **Step 5: Commit**

```bash
git add docs/adr/0007-one-window-per-project.md README.md docs/HANDOFF.md
git commit -m "docs: ADR 0007 one window per project"
```

---

## Manual smoke (user-driven, after Task 6)

Not automatable here — Qt window activation and `open -n` need a real
desktop session:

1. `spar gui --dir ~/P_PROJ/ai_fight` and `spar gui --dir ~/P_PROJ/spar_tests`
   → two windows, distinct titles, both interactive; start a run in one and
   confirm the other stays responsive.
2. Resize/rearrange splitters differently in each, close both, reopen →
   each window comes back with its own layout.
3. `spar gui --dir ~/P_PROJ/ai_fight` a third time while both are open →
   no new window; the existing ai_fight window comes to the front.
4. `Projekt → Otwórz projekt…` → picker opens a THIRD window; the menu then
   lists the other open projects as recents.
5. macOS bundle only: `Projekt → Otwórz projekt…` from the frozen app opens
   a second `Spar.app` instance (this is what `open -n` buys).

## Review history

### Round 1 (codex gpt-5.5) — Verdict: CONTINUE

Accepted:

- **#1 wrong enum path** — fixed in Task 5, but NOT with the reviewer's
  suggested `QLocalSocket.LocalSocketError.AddressInUseError`: verified in
  this venv that `QLocalSocket.LocalSocketError` has no `AddressInUseError`
  member and `QLocalServer.serverError()` returns a
  `QAbstractSocket.SocketError`. The plan now uses
  `QAbstractSocket.SocketError.AddressInUseError` and documents both facts
  so a future editor does not "correct" it back.
- **#2 tests violated the plan's own no-real-sockets constraint** — the
  constraint was the wrong half to keep. Real `listen` on a `tmp_path`-derived
  name is hermetic and worth having; real *processes* are not. The constraint
  now says exactly that, and requires `release()` in `finally`.
- **#3 stale-socket test proved nothing** — confirmed empirically that
  `QLocalServer.removeServer(name)` succeeds against a LIVE listener and both
  servers then report `isListening() == True`. The fake-stale test is gone,
  replaced by `TestSingleInstanceGuardBranches`, which scripts `listen`
  outcomes through an injected `server_factory` and asserts the branch ORDER
  (listen → probe → remove → listen). The live-owner-not-stolen case is
  covered by the real-socket test.
- **#4 `release()` by a non-owner freed the owner's name** — real bug given
  #3's finding. `SingleInstanceGuard` now tracks `_owned` and `release()`
  returns early for a guard that never claimed; new test
  `test_release_by_a_non_owner_does_not_free_the_owner`.
- **#5 existing global-key assertions would fail** — confirmed at
  `tests/test_gui_app.py:79,209,241,799`. Task 2 gained an explicit step that
  rewrites all four with `scoped(...)`, plus a `grep` to re-locate them after
  line drift, and a rule for which keys stay literal.
- **#6 (NICE) migration of pre-existing global layout keys** — accepted as a
  documented decision: no migration, defaults on first launch per project,
  old global keys left unread. Recorded in Global Constraints.

Rejected: none.

### Round 2 (codex gpt-5.5) — Verdict: CONTINUE

Accepted:

- **#7 double raise / contradictory claim order** — real bug. The protocol
  text said probe-then-listen while the code listened first, and both the
  real-socket test and `main_gui()` sent a SECOND `request_raise()` after
  `try_claim()` had already delivered one, so the owner's window would bounce
  twice and the test's `raised == [True]` was flaky. Resolved by fixing the
  contract in one direction: `try_claim()` owns the decision AND the raise
  delivery; `try_claim() == False` means "owner already asked, just exit 0".
  Updated in the Interfaces block, the protocol steps (now listen-first, with
  the probe-timeout race documented as a deliberate trade), the real-socket
  test (now asserts exactly one delivery), the `FakeGuard` in the entry-point
  test (counts `extra_raises`, must be 0), and `main_gui()`.

Rejected: none.

### Round 3 (codex gpt-5.5) — Verdict: CONTINUE

Accepted:

- **#8 raise lost when it arrives during window construction** — verified in
  the real code: `FilesView._wait_dir_loaded()` pumps
  `app.processEvents(AllEvents, 10)` (`spar/gui/files.py:1343`) inside
  `MainWindow.__init__`, while the guard is already listening and nothing is
  connected to `raise_requested` yet. Combined with #7 (no second request
  allowed) that raise would vanish, so the duplicate launch would appear to
  do nothing. Fixed by buffering in the guard: `_on_ready` sets
  `_pending_raise` while unattached, and the new `attach(receiver)` connects
  and replays a buffered raise exactly once. `main_gui` now calls
  `guard.attach(window.raise_to_front)` instead of connecting the signal, the
  interfaces block lists `attach`, the protocol section documents the
  buffering, and a new test
  (`test_raise_arriving_before_attach_is_replayed_once`) pumps events with no
  receiver attached and asserts one replay.

Rejected: none.

### Round 4 (codex gpt-5.5) — Verdict: CONTINUE

Accepted (both verified empirically, not taken on trust):

- **#9 `files/tree_split` scoping missed the runtime path** — confirmed: the
  plan invented `_restore_tree_split`/`_save_tree_split`, while the real
  `FilesView` has `_restore_split_state()`/`_save_split_state()`
  (`spar/gui/files.py:1557`, `:1562`) wired in the constructor at
  `:1262-1263`. As written, an implementer could add scoped helpers, leave the
  wired methods on the global key, and still pass the test. Task 2 now keeps
  the existing names and wiring and changes only the key inside them, plus two
  new tests that exercise the RUNTIME paths: `splitterMoved.emit(...)` for the
  save side and a second `FilesView` construction for the restore side.
- **#10 `waitForBytesWritten()` is not delivery proof** — real bug, reproduced
  in this venv: `write()` → 6 bytes, `flush()` → True,
  `waitForBytesWritten()` → **False**, and the owner still read `b"raise\n"`.
  With the old code every live owner would look stale, so `removeServer()`
  would steal its name and the user would get two windows on one project —
  the exact failure this tranche exists to prevent. `request_raise()` now
  proves delivery with `written == len(_RAISE_MESSAGE) and
  bytesToWrite() == 0` (waiting only if something is actually pending), and
  the API-facts block documents the trap. The real-socket test already asserts
  `second.try_claim() is False`, which is the end-to-end guard against this
  regressing.

Rejected: none.

**Status: round limit (4) reached with the last round's fixes UNVERIFIED by
the reviewer.** Open question for the user: run a verification round 5, or
start executing the plan.

### Round 5 (codex gpt-5.5, verification) — Verdict: AGREE

"No new or unresolved findings." Rounds 1-4 produced 9 MUSTs and 1 NICE, all
accepted and applied; none rejected. The plan is cleared for execution.
