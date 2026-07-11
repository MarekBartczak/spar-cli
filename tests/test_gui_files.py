from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

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

    def test_save_shortcut_activated_is_wired_to_save_current(self, qtbot, tmp_path):
        # Offscreen CI never routes the real chord through the QShortcut (the
        # eventFilter bridge handles it there), so the
        # activated -> _save_current connection itself would otherwise be
        # untested — deleting it would stay green here while breaking Ctrl+S
        # on a real display. Emit the signal directly to pin the wiring.
        view = self._view(qtbot, tmp_path)
        view.open_file(tmp_path / "app.py")
        ed = view.tabs.currentWidget().editor
        ed.setPlainText("via activated\n")
        ed.document().setModified(True)  # review #9
        view._save_shortcut.activated.emit()
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == "via activated\n"
        assert view.has_unsaved() is False


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

    def test_propagated_same_event_counts_as_one_press(self, qtbot):
        # Live finding: a bare Shift is not consumed by the focused widget,
        # so Qt re-delivers the SAME QKeyEvent up the parent chain and the
        # application-level filter sees each hop — without identity dedup a
        # SINGLE physical press fired the finder.
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from spar.gui.files import DoubleShiftFilter

        filt = DoubleShiftFilter()
        fired = []
        filt.triggered.connect(lambda: fired.append(1))
        filt._now = lambda: 0.0
        ev = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Shift,
            Qt.KeyboardModifier.NoModifier,
        )
        # One physical press delivered to several objects in the chain.
        for target in (None, None, None):
            filt.eventFilter(target, ev)
        assert fired == []  # single press must NOT trigger
        # A genuinely new second press (new event object) still triggers.
        filt._now = lambda: 0.2
        ev2 = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Shift,
            Qt.KeyboardModifier.NoModifier,
        )
        filt.eventFilter(None, ev2)
        assert fired == [1]

    def test_window_and_widget_copies_count_as_one_press(self, qtbot):
        # Live finding #2: one physical press yields a QWindow-level event
        # AND a distinct widget-level QKeyEvent instance — identity dedup
        # missed the pair. Copies share the input timestamp; a genuinely new
        # press carries a new timestamp.
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from spar.gui.files import DoubleShiftFilter

        filt = DoubleShiftFilter()
        fired = []
        filt.triggered.connect(lambda: fired.append(1))

        def shift(ts):
            ev = QKeyEvent(
                QEvent.Type.KeyPress, Qt.Key.Key_Shift,
                Qt.KeyboardModifier.NoModifier,
            )
            ev.setTimestamp(ts)
            return ev

        filt._now = lambda: 0.0
        # One physical press: two DISTINCT instances, same timestamp.
        filt.eventFilter(None, shift(1000))
        filt.eventFilter(None, shift(1000))
        assert fired == []  # single press must NOT trigger
        # Second physical press (new timestamp, again two copies) → trigger.
        filt._now = lambda: 0.2
        filt.eventFilter(None, shift(1180))
        filt.eventFilter(None, shift(1180))
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


class TestSearchSession:
    def test_delivers_matches(self, qtbot, tmp_path):
        from spar.gui.files import SearchSession, SearchSpec

        (tmp_path / "a.py").write_text("todo one\ntodo two\n", encoding="utf-8")
        session = SearchSession(tmp_path)
        got = []
        # review #23: payload is (matches, fingerprint) — one file per batch
        session.batch.connect(lambda payload: got.extend(payload[0]))
        done = []
        session.finished.connect(lambda n, m: done.append((n, m)))
        session.search(SearchSpec("todo"))
        qtbot.waitUntil(lambda: bool(done), timeout=5000)
        assert done[0][0] == 2  # two matches
        assert {m.line for m in got} == {1, 2}
        session.stop()

    def test_superseded_search_delivers_no_stale_results(self, qtbot, tmp_path):
        # Slow fake scan: first query is still "scanning" when the second
        # supersedes it; only the second query's results may arrive.
        import time
        from spar.gui.files import SearchSession, SearchSpec, SearchMatch

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

        def slow_scan(root, rel, pattern, limit=None):
            time.sleep(0.2)
            # Tag the match text with the pattern so we can tell runs apart.
            return [SearchMatch(rel, 1, pattern.pattern, 0, 1)]

        session = SearchSession(tmp_path, scan_file=slow_scan)
        got = []
        # review #23: payload is (matches, fingerprint) — one file per batch
        session.batch.connect(lambda payload: got.extend(payload[0]))
        session.search(SearchSpec("first"))
        session.search(SearchSpec("second"))  # supersedes immediately
        qtbot.wait(800)
        texts = {m.text for m in got}
        assert "first" not in texts  # stale run dropped
        session.stop()

    def test_regex_search_uses_python_not_ripgrep(self, qtbot, tmp_path, monkeypatch):
        # review #4: regex specs must never shell out to rg (no parity).
        from spar.gui import files as fmod

        (tmp_path / "a.py").write_text("v1 v2\n", encoding="utf-8")

        def _boom(*_a, **_k):
            raise AssertionError("ripgrep used for a regex search")

        monkeypatch.setattr(fmod, "build_ripgrep_argv", _boom)
        session = fmod.SearchSession(tmp_path)
        done, got = [], []
        session.finished.connect(lambda n, m: done.append((n, m)))
        # review #23: payload is (matches, fingerprint) — one file per batch
        session.batch.connect(lambda payload: got.extend(payload[0]))
        session.search(fmod.SearchSpec(r"v\d", regex=True))
        qtbot.waitUntil(lambda: bool(done), timeout=5000)
        assert done[0][0] == 2  # two matches, via the python path
        session.stop()

    def test_case_insensitive_search_uses_python_not_ripgrep(self, qtbot, tmp_path, monkeypatch):
        # review #19: rg's -i unicode case-folding diverges from python re,
        # so a case-insensitive spec (is_rg_compatible == False) must take
        # the python path, never rg.
        from spar.gui import files as fmod

        (tmp_path / "a.py").write_text("TODO todo\n", encoding="utf-8")

        def _boom(*_a, **_k):
            raise AssertionError("ripgrep used for a case-insensitive search")

        monkeypatch.setattr(fmod, "build_ripgrep_argv", _boom)
        session = fmod.SearchSession(tmp_path)
        done = []
        session.finished.connect(lambda n, m: done.append((n, m)))
        session.search(fmod.SearchSpec("todo"))  # default: case-insensitive
        qtbot.waitUntil(lambda: bool(done), timeout=5000)
        assert done[0][0] == 2  # both cases matched, via the python path
        session.stop()

    def test_cancel_bumps_generation_and_drops_stale(self, qtbot, tmp_path):
        # review #2 (empty/invalid clause): cancel() must supersede an
        # in-flight run so its late batches never reach the facade.
        import time
        from spar.gui.files import SearchSession, SearchSpec, SearchMatch

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

        def slow_scan(root, rel, pattern, limit=None):
            time.sleep(0.2)
            return [SearchMatch(rel, 1, "x", 0, 1)]

        session = SearchSession(tmp_path, scan_file=slow_scan)
        got = []
        # review #23: payload is (matches, fingerprint) — one file per batch
        session.batch.connect(lambda payload: got.extend(payload[0]))
        session.search(SearchSpec("first"))
        session.cancel()  # supersede without dispatching a new search
        qtbot.wait(600)
        assert got == []  # cancelled run delivered nothing
        session.stop()

    def test_stop_bumps_worker_live_generation(self, qtbot, tmp_path):
        # review #14: stop() must bump the WORKER's live generation (not just
        # the facade's), so an in-flight python scan sees the supersede and
        # bails promptly instead of finishing the whole walk.
        import time
        from spar.gui.files import SearchSession, SearchSpec, SearchMatch

        (tmp_path / "a.py").write_text("x\n", encoding="utf-8")

        def slow_scan(root, rel, pattern, limit=None):
            time.sleep(0.2)
            return [SearchMatch(rel, 1, "x", 0, 1)]

        session = SearchSession(tmp_path, scan_file=slow_scan)
        session.search(SearchSpec("first"))
        searched_gen = session._generation
        session.stop()
        assert session._worker._live_generation == session._generation
        assert session._worker._live_generation > searched_gen

    def test_ripgrep_batch_killed_when_superseded_with_no_output_yet(self, tmp_path, monkeypatch):
        # reviews #14/#22/#24: a large NO-MATCH rg batch emits no stdout
        # at all. A worker blocking in `for line in proc.stdout` could
        # only notice a supersede on the next line — which never comes —
        # stalling this run AND every queued search. The queue-based
        # drain re-checks _live_generation on every queue.get timeout
        # tick (~50 ms), so rg is killed even while completely silent.
        import io
        import subprocess
        import threading

        from spar.gui import files as fmod

        (tmp_path / "a.py").write_text("todo\n", encoding="utf-8")
        worker = fmod._SearchWorker(tmp_path)
        worker._live_generation = 1
        killed = threading.Event()

        class FakeStdout:
            def __iter__(self):
                return self

            def __next__(self):
                # the daemon reader thread blocks HERE: rg has produced
                # NO output yet. Supersede now — only a queue.get
                # timeout tick can notice it (there is no line to read).
                worker._live_generation = 2
                if not killed.wait(timeout=5):
                    raise AssertionError(
                        "worker never killed the silent rg batch"
                    )
                raise StopIteration  # kill() → stream ends

        class FakePopen:
            def __init__(self, *a, **k):
                self.stdout = FakeStdout()
                self.stderr = io.StringIO("")
                self.returncode = 0

            def kill(self):
                killed.set()

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen())
        result = worker._ripgrep_grouped(
            1, fmod.SearchSpec("todo", case_sensitive=True)
        )
        assert result is None      # cancelled → caller takes python early-exit
        assert killed.is_set()     # rg killed while it had emitted NOTHING

    def test_ripgrep_output_exceeding_pipe_capacity_completes(self, tmp_path):
        # review #22: stdout must be drained WHILE rg runs. The old shape
        # (wait for exit, then communicate) deadlocked once the --json
        # output exceeded the OS pipe capacity (~64 KB): rg blocked
        # writing the full pipe while the worker blocked waiting for
        # exit. 3000 match events ≈ several hundred KB of JSON.
        import shutil

        if shutil.which("rg") is None:
            pytest.skip("ripgrep not installed")
        from spar.gui import files as fmod

        body = "".join(f"todo padding line {i:05d}\n" for i in range(3000))
        (tmp_path / "big.py").write_text(body, encoding="utf-8")
        worker = fmod._SearchWorker(tmp_path)
        worker._live_generation = 1
        result = worker._ripgrep_grouped(
            1, fmod.SearchSpec("todo", case_sensitive=True)
        )
        assert result is not None           # completed — no deadlock
        grouped, truncated = result         # review #33: (groups, truncated)
        assert truncated is False           # 3000 < _SEARCH_MAX_RESULTS
        (rel, fingerprint, bucket), = grouped
        assert rel == "big.py" and len(bucket) == 3000
        assert fingerprint is not None      # review #25: pre-launch snapshot

    def test_batch_payload_carries_scan_time_fingerprint(self, qtbot, tmp_path):
        # review #23: the (mtime_ns, size) fingerprint is captured by the
        # WORKER (before the file is read) and travels with the payload —
        # the GUI never stats the file itself.
        from spar.gui.files import SearchSession, SearchSpec

        f = tmp_path / "a.py"
        f.write_text("todo\n", encoding="utf-8")
        st = f.stat()
        session = SearchSession(tmp_path)
        payloads = []
        session.batch.connect(payloads.append)
        done = []
        session.finished.connect(lambda n, m: done.append(n))
        session.search(SearchSpec("todo"))
        qtbot.waitUntil(lambda: bool(done), timeout=5000)
        (matches, fingerprint), = payloads
        assert [m.line for m in matches] == [1]
        assert fingerprint == (st.st_mtime_ns, st.st_size)
        session.stop()

    def test_rg_fingerprint_snapshot_taken_before_launch(self, tmp_path, monkeypatch):
        # review #25: the rg path must stat the WHOLE batch BEFORE
        # launching rg. Stat-at-first-parsed-match (the old shape) ran
        # AFTER rg had already read the file: a write landing between
        # rg's read and the parse got blessed with the POST-write
        # fingerprint, and replace would clobber the newer content.
        # Here a fake rg mutates the file mid-stream — the emitted
        # fingerprint must be the PRE-launch one, so the replace-time
        # verification (review #6) refuses with "plik zmienił się".
        import io
        import json
        import subprocess

        from spar.gui import files as fmod

        f = tmp_path / "a.py"
        f.write_text("todo\n", encoding="utf-8")
        st = f.stat()
        pre = (st.st_mtime_ns, st.st_size)

        row = json.dumps({
            "type": "match",
            "data": {
                "path": {"text": str(f)},
                "line_number": 1,
                "lines": {"text": "todo\n"},
                "submatches": [{"start": 0, "end": 4}],
            },
        }) + "\n"

        class FakeStdout:
            def __init__(self):
                self._lines = iter([row])

            def __iter__(self):
                return self

            def __next__(self):
                # rg is streaming: the file changes AFTER launch (and
                # after rg's read), BEFORE its match row is parsed.
                f.write_text("todo but changed on disk\n", encoding="utf-8")
                return next(self._lines)

        class FakePopen:
            def __init__(self, *a, **k):
                self.stdout = FakeStdout()
                self.stderr = io.StringIO("")
                self.returncode = 0

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen())
        worker = fmod._SearchWorker(tmp_path)
        worker._live_generation = 1
        grouped, _truncated = worker._ripgrep_grouped(
            1, fmod.SearchSpec("todo", case_sensitive=True)
        )  # review #33: returns (groups, truncated)
        (rel, fingerprint, bucket), = grouped
        assert rel == "a.py" and len(bucket) == 1
        assert fingerprint == pre  # snapshot from BEFORE the launch
        st2 = f.stat()
        # the mid-stream write no longer matches the payload fingerprint,
        # so _apply_replace's check (review #6) skips it: replace refuses.
        assert (st2.st_mtime_ns, st2.st_size) != fingerprint

    def test_rg_result_cap_enforced_during_parse_kills_rg(self, tmp_path, monkeypatch):
        # review #33: the old shape accumulated EVERY parsed match in
        # `grouped` and only capped the EMISSION in _emit_grouped — a
        # common literal over a big tree could parse millions of match
        # rows into memory (OOM). The cap must stop the PARSE: a fake rg
        # streaming far more than _SEARCH_MAX_RESULTS rows is killed the
        # moment the cap is reached, the stream is not drained further,
        # and the collected groups come back flagged truncated.
        import io
        import json
        import subprocess
        import threading

        from spar.gui import files as fmod

        f = tmp_path / "a.py"
        f.write_text("todo\n", encoding="utf-8")
        cap = fmod._SEARCH_MAX_RESULTS
        killed = threading.Event()

        def _rows():
            # an "endless" rg: serves comfortably past the cap, then
            # STALLS — only a kill (which in real life closes stdout)
            # lets the stream end. If the worker kept accumulating
            # instead of capping the parse, it would hang here and the
            # wait below would fail the test.
            for i in range(cap + 100):
                yield json.dumps({
                    "type": "match",
                    "data": {
                        "path": {"text": str(f)},
                        "line_number": i + 1,
                        "lines": {"text": "todo\n"},
                        "submatches": [{"start": 0, "end": 4}],
                    },
                }) + "\n"
            if not killed.wait(timeout=5):
                raise AssertionError(
                    "worker never killed rg at the result cap"
                )

        class FakePopen:
            def __init__(self, *a, **k):
                self.stdout = _rows()
                self.stderr = io.StringIO("")
                self.returncode = 0

            def kill(self):
                killed.set()

            def wait(self, timeout=None):
                return 0

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakePopen())
        worker = fmod._SearchWorker(tmp_path)
        worker._live_generation = 1
        grouped, truncated = worker._ripgrep_grouped(
            1, fmod.SearchSpec("todo", case_sensitive=True)
        )
        assert truncated is True
        assert killed.is_set()           # rg killed mid-stream at the cap
        total = sum(len(bucket) for _rel, _fp, bucket in grouped)
        assert total == cap              # collected == cap, nothing beyond

    def test_rg_batches_bounded_by_argv_byte_budget(self, tmp_path):
        # review #34: _RIPGREP_BATCH alone bounds the file COUNT, not the
        # argv BYTES — long paths could trip ARG_MAX. Batches split when
        # the estimated encoded size exceeds _RIPGREP_ARGV_BUDGET, with
        # the count as the secondary bound. review #36: the budget must
        # count the ABSOLUTE strings build_ripgrep_argv passes
        # (str(root / rel)), not the bare relatives — the assertion below
        # measures the absolute forms.
        from spar.gui import files as fmod

        long_rel = "d/" + "x" * 4094  # 4 KB per path
        files = [f"{long_rel}{i:04d}" for i in range(100)]  # ~400 KB total
        batches = list(fmod._rg_batches(tmp_path, files))
        assert len(batches) > 1                      # split by BYTES
        assert [p for b in batches for p in b] == files  # order + coverage
        import os as _os
        for b in batches:
            # review #36: measure what rg's argv actually carries.
            size = sum(
                len(_os.fsencode(str(tmp_path / p))) + 1 for p in b
            )
            assert size <= fmod._RIPGREP_ARGV_BUDGET
            assert len(b) <= fmod._RIPGREP_BATCH     # secondary bound

    def test_python_path_result_cap_slices_single_huge_file(self, qtbot, tmp_path):
        # review #35: the python path emitted each file's COMPLETE matches
        # list BEFORE the cap check — one file with more matches than
        # _SEARCH_MAX_RESULTS blew straight past the cap. The per-file
        # list must be sliced to the remaining allowance before the emit:
        # exactly cap results arrive, flagged truncated.
        from spar.gui import files as fmod

        cap = fmod._SEARCH_MAX_RESULTS
        (tmp_path / "huge.py").write_text("x\n", encoding="utf-8")
        seen_limits = []

        def fat_scan(root, rel, pattern, limit=None):
            seen_limits.append(limit)   # review #37: spy on the allowance
            return [
                fmod.SearchMatch(rel, i + 1, "x", 0, 1)
                for i in range(cap + 100)   # over-returns past its limit —
                # exercises the worker's belt-and-braces slice
            ]

        session = fmod.SearchSession(tmp_path, scan_file=fat_scan)
        got, done = [], []
        # review #23: payload is (matches, fingerprint) — one file per batch
        session.batch.connect(lambda payload: got.extend(payload[0]))
        session.finished.connect(lambda n, m, t: done.append((n, m, t)))
        session.search(fmod.SearchSpec("x"))
        qtbot.waitUntil(lambda: bool(done), timeout=5000)
        assert len(got) == cap              # sliced to EXACTLY the cap
        assert done[0] == (cap, 1, True)    # truncated flagged
        # review #37: the worker passed the REMAINING allowance down as
        # search_file's limit (first file ⇒ the full cap).
        assert seen_limits == [cap]
        session.stop()


class TestSearchPanel:
    def _panel(self, qtbot, tmp_path):
        from spar.gui.files import SearchPanel

        (tmp_path / "a.py").write_text("todo one\nplain\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("todo two\n", encoding="utf-8")
        panel = SearchPanel(tmp_path)
        # qtbot.addWidget closes the panel at teardown; SearchPanel.closeEvent
        # calls stop_session() so the search thread never leaks (review #3).
        qtbot.addWidget(panel)
        return panel

    def test_search_populates_results_tree(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(lambda: panel.results.topLevelItemCount() == 2, timeout=5000)
        files = {
            panel.results.topLevelItem(i).text(0).split(" ")[0]
            for i in range(panel.results.topLevelItemCount())
        }
        assert files == {"a.py", "b.py"}

    def test_status_reports_counts(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(lambda: "wyników" in panel.status.text(), timeout=5000)
        assert "2" in panel.status.text()  # 2 matches
        assert "2 plik" in panel.status.text()  # in 2 files

    def test_invalid_regex_marks_query_and_disables(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        panel.regex_toggle.setChecked(True)
        panel.query.setText("(")
        panel._run_search()
        assert panel._invalid is True
        assert panel.query.property("invalid") is True

    def test_empty_query_cancels_and_clears(self, qtbot, tmp_path):
        # review #2: an empty query must bump the generation (cancel any
        # in-flight run) and clear, not silently leave a search running.
        panel = self._panel(qtbot, tmp_path)
        cancelled = []
        panel._session.cancel = lambda: cancelled.append(True)
        panel.query.setText("")
        panel._run_search()
        assert cancelled == [True]
        assert panel.results.topLevelItemCount() == 0

    def test_invalid_query_cancels_in_flight(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        cancelled = []
        panel._session.cancel = lambda: cancelled.append(True)
        panel.regex_toggle.setChecked(True)
        panel.query.setText("(")
        panel._run_search()
        assert cancelled == [True]

    def test_invalid_query_clears_results_and_resets_specs(self, qtbot, tmp_path):
        # review #28: the invalid-regex branch mirrors the empty-query
        # path — already-delivered (possibly partial) results are cleared
        # and both specs reset, so replace stays disabled and no stale
        # tree sits under the error banner.
        panel = self._panel(qtbot, tmp_path)
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(lambda: panel._results_spec is not None, timeout=5000)
        assert panel.results.topLevelItemCount() == 2
        panel.regex_toggle.setChecked(True)  # re-runs; "todo" still valid
        panel.query.setText("todo(")         # now an invalid regex
        panel._run_search()
        assert panel._invalid is True
        assert panel.results.topLevelItemCount() == 0  # partials cleared
        assert panel._pending_spec is None
        assert panel._results_spec is None

    def test_clicking_line_emits_open_location(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(lambda: panel.results.topLevelItemCount() == 2, timeout=5000)
        emitted = []
        panel.open_location.connect(lambda *a: emitted.append(a))
        # first file's first (and only) line child
        file_item = panel.results.topLevelItem(0)
        line_item = file_item.child(0)
        panel._on_item_activated(line_item, 0)
        assert emitted and emitted[0][0] in ("a.py", "b.py")
        assert emitted[0][1] == 1  # line number


class TestReplaceInFiles:
    def _panel(self, qtbot, tmp_path):
        from spar.gui.files import SearchPanel

        (tmp_path / "a.py").write_text("cat here\ncat again\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("cat only\n", encoding="utf-8")
        panel = SearchPanel(tmp_path)
        qtbot.addWidget(panel)
        return panel

    def _search(self, qtbot, panel, text="cat"):
        panel.query.setText(text)
        panel._run_search()
        qtbot.waitUntil(lambda: panel.results.topLevelItemCount() == 2, timeout=5000)

    def test_fresh_panel_starts_with_replace_disabled(self, qtbot, tmp_path):
        # review #42: __init__ must end with _update_replace_state() —
        # a fresh panel has _results_spec None (stale), so the replace
        # button starts DISABLED (Qt buttons default to enabled), and a
        # click is a guarded no-op. Rows created later while the panel is
        # read-only stay uncheckable (covered by
        # test_rows_created_while_read_only_not_checkable).
        panel = self._panel(qtbot, tmp_path)
        assert panel.replace_button.isEnabled() is False
        panel.replace.setText("dog")
        panel._apply_replace()  # no results → must not touch any file
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "cat here\ncat again\n"

    def test_replace_writes_checked_files(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)
        panel.replace.setText("dog")
        panel._apply_replace()
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "dog here\ndog again\n"
        assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dog only\n"

    def test_unchecked_file_is_left_alone(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)
        # Uncheck the b.py row.
        for i in range(panel.results.topLevelItemCount()):
            item = panel.results.topLevelItem(i)
            if item.data(0, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.ItemDataRole.UserRole + 1) == "b.py":
                item.setCheckState(0, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.CheckState.Unchecked)
        panel.replace.setText("dog")
        panel._apply_replace()
        assert (tmp_path / "b.py").read_text(encoding="utf-8") == "cat only\n"

    def test_skips_files_with_unsaved_edits_and_reports(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        # a.py is "open dirty".
        panel.dirty_open_paths = lambda: {str(tmp_path / "a.py")}
        self._search(qtbot, panel)
        panel.replace.setText("dog")
        panel._apply_replace()
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "cat here\ncat again\n"
        assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dog only\n"
        assert "pominięto 1" in panel.status.text()

    def test_regex_replace_uses_same_toggle_pattern(self, qtbot, tmp_path):
        (tmp_path / "c.py").write_text("v1 v2\n", encoding="utf-8")
        from spar.gui.files import SearchPanel
        panel = SearchPanel(tmp_path)
        qtbot.addWidget(panel)
        panel.regex_toggle.setChecked(True)
        panel.query.setText(r"v(\d)")
        panel._run_search()
        qtbot.waitUntil(lambda: panel.results.topLevelItemCount() >= 1, timeout=5000)
        panel.replace.setText(r"w\1")
        panel._apply_replace()
        assert (tmp_path / "c.py").read_text(encoding="utf-8") == "w1 w2\n"

    def test_read_only_disables_replace_not_search(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)  # give it results so the button can enable
        panel.set_replace_enabled(False)
        assert panel.replace.isEnabled() is False
        assert panel.replace_button.isEnabled() is False
        assert panel.query.isEnabled() is True  # search stays live

    def test_replace_disabled_when_query_edited_after_search(self, qtbot, tmp_path):
        # review #5: editing the query without re-running makes the results
        # stale → replace disabled with an explanatory hint.
        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)
        assert panel.replace_button.isEnabled() is True
        panel.query.setText("dog")  # drift from the searched spec
        assert panel.replace_button.isEnabled() is False
        assert "nieaktualne" in panel.replace_button.toolTip()

    def test_replace_disabled_while_search_in_flight(self, qtbot, tmp_path):
        # review #15: replace must stay DISABLED until the search completes —
        # _results_spec is promoted only in _on_finished, never at dispatch,
        # so a partial (mid-scan) tree can never be replaced.
        import time
        from spar.gui.files import SearchMatch, SearchPanel, SearchSession

        (tmp_path / "a.py").write_text("cat\n", encoding="utf-8")

        def slow_scan(root, rel, pattern, limit=None):
            time.sleep(0.2)
            return [SearchMatch(rel, 1, "cat", 0, 3)]

        session = SearchSession(tmp_path, scan_file=slow_scan)
        panel = SearchPanel(tmp_path, session=session)
        qtbot.addWidget(panel)
        panel.query.setText("cat")
        panel._run_search()
        # dispatch happened but finished has NOT fired yet.
        assert panel._results_spec is None
        assert panel.replace_button.isEnabled() is False
        qtbot.waitUntil(lambda: panel._results_spec is not None, timeout=5000)
        assert panel.replace_button.isEnabled() is True  # enabled on finish

    def test_apply_replace_uses_stored_spec_not_current_controls(self, qtbot, tmp_path):
        # review #5: even if forced, replace must not run against a drifted
        # spec — it targets the stored one and no-ops when they differ.
        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)
        panel.replace.setText("dog")
        panel.query.setText("zzz")  # current spec now differs from stored
        panel._apply_replace()
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "cat here\ncat again\n"

    def test_skips_file_changed_since_search_and_reports(self, qtbot, tmp_path):
        # review #6: a file whose size/mtime changed after the search must
        # be refused (its content may no longer match the results).
        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)
        (tmp_path / "a.py").write_text("cat cat cat\n", encoding="utf-8")  # differs
        panel.replace.setText("dog")
        panel._apply_replace()
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "cat cat cat\n"
        assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dog only\n"
        assert "plik zmienił się" in panel.status.text()

    def test_file_modified_between_scan_and_row_creation_is_skipped(self, qtbot, tmp_path):
        # review #23: the fingerprint is captured AT SCAN TIME (before the
        # file's bytes are read), not when the GUI row is built — rg scans
        # a whole batch before any row lands, so a GUI-side stat would
        # bless STALE matches with the NEW fingerprint. A file modified
        # after the scan's read but before its row exists must be refused.
        from spar.gui.files import SearchPanel, SearchSession, search_file

        target = tmp_path / "a.py"
        target.write_text("cat here\n", encoding="utf-8")

        def mutating_scan(root, rel, pattern, limit=None):
            matches = search_file(root, rel, pattern, limit=limit)
            # the file changes AFTER the scan read, BEFORE the GUI row.
            (tmp_path / rel).write_text(
                "cat mutated after scan\n", encoding="utf-8"
            )
            return matches

        session = SearchSession(tmp_path, scan_file=mutating_scan)
        panel = SearchPanel(tmp_path, session=session)
        qtbot.addWidget(panel)
        panel.query.setText("cat")
        panel._run_search()
        qtbot.waitUntil(
            lambda: panel.results.topLevelItemCount() == 1, timeout=5000
        )
        panel.replace.setText("dog")
        panel._apply_replace()
        # untouched: the scan-time fingerprint no longer matches disk.
        assert target.read_text(encoding="utf-8") == "cat mutated after scan\n"
        assert "plik zmienił się" in panel.status.text()

    def test_skips_non_utf8_file_and_reports(self, qtbot, tmp_path):
        # review #6: strict decode — never corrupt non-UTF-8 bytes.
        from spar.gui.files import SearchPanel

        (tmp_path / "x.txt").write_bytes(b"caf\xe9 cat\n")  # invalid UTF-8
        panel = SearchPanel(tmp_path)
        qtbot.addWidget(panel)
        panel.query.setText("cat")
        panel._run_search()
        qtbot.waitUntil(lambda: panel.results.topLevelItemCount() == 1, timeout=5000)
        panel.replace.setText("dog")
        panel._apply_replace()
        assert (tmp_path / "x.txt").read_bytes() == b"caf\xe9 cat\n"  # untouched
        assert "nie-UTF-8" in panel.status.text()

    def test_replace_leaves_no_temp_file(self, qtbot, tmp_path):
        # review #6: the atomic temp file must not linger after a write.
        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)
        panel.replace.setText("dog")
        panel._apply_replace()
        assert list(tmp_path.glob("*.spar-tmp")) == []

    def test_replace_never_clobbers_existing_spar_tmp_sibling(self, qtbot, tmp_path):
        # review #32: the old predictable temp name `<file>.spar-tmp`
        # would OVERWRITE a legitimate user file of exactly that name
        # (write_bytes truncates) and then rename it away or unlink it.
        # mkstemp's unique, exclusively-created name can never collide:
        # the pre-existing sibling must survive byte-identical.
        sibling_a = tmp_path / "a.py.spar-tmp"
        sibling_b = tmp_path / "b.py.spar-tmp"
        sibling_a.write_text("legit user file, hands off\n", encoding="utf-8")
        sibling_b.write_text("also legit\n", encoding="utf-8")
        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)
        panel.replace.setText("dog")
        panel._apply_replace()
        # the replace itself worked…
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "dog here\ndog again\n"
        # …and the pre-existing *.spar-tmp files are untouched, in place.
        assert sibling_a.read_text(encoding="utf-8") == "legit user file, hands off\n"
        assert sibling_b.read_text(encoding="utf-8") == "also legit\n"

    def test_replace_preserves_executable_mode(self, qtbot, tmp_path):
        # review #17: os.replace swaps the inode, so the temp file's perms
        # would clobber the original's — a 0o755 script would lose +x. The
        # atomic writer chmods the temp to the original mode before rename.
        import os
        import stat as _stat

        script = tmp_path / "run.sh"
        script.write_text("cat here\ncat again\n", encoding="utf-8")
        os.chmod(script, 0o755)
        from spar.gui.files import SearchPanel

        panel = SearchPanel(tmp_path)
        qtbot.addWidget(panel)
        panel.query.setText("cat")
        panel._run_search()
        qtbot.waitUntil(
            lambda: panel.results.topLevelItemCount() == 1, timeout=5000
        )
        panel.replace.setText("dog")
        panel._apply_replace()
        assert script.read_text(encoding="utf-8") == "dog here\ndog again\n"
        assert _stat.S_IMODE(script.stat().st_mode) == 0o755  # +x survived

    def test_replace_through_symlink_keeps_link_writes_target(self, qtbot, tmp_path):
        # review #20: os.replace on the symlink path would swap the LINK
        # itself for a regular file. The writer resolves first, so the
        # link SURVIVES as a symlink and the TARGET's content changes.
        # The target lives in a skip-dir (node_modules) so only the LINK
        # row appears in the results.
        import os

        (tmp_path / "node_modules").mkdir()
        real = tmp_path / "node_modules" / "real.txt"
        real.write_text("cat here\n", encoding="utf-8")
        link = tmp_path / "link.txt"
        os.symlink(real, link)
        from spar.gui.files import SearchPanel

        panel = SearchPanel(tmp_path)
        qtbot.addWidget(panel)
        panel.query.setText("cat")
        panel._run_search()
        qtbot.waitUntil(
            lambda: panel.results.topLevelItemCount() == 1, timeout=5000
        )
        panel.replace.setText("dog")
        panel._apply_replace()
        assert link.is_symlink()  # the link is STILL a symlink
        assert real.read_text(encoding="utf-8") == "dog here\n"  # target rewritten

    def test_replace_skips_symlink_escaping_project(self, qtbot, tmp_path):
        # review #20: a symlink resolving OUTSIDE the project root is never
        # written — skipped and reported.
        import os

        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "real.txt"
        target.write_text("cat here\n", encoding="utf-8")
        proj = tmp_path / "proj"
        proj.mkdir()
        link = proj / "link.txt"
        os.symlink(target, link)
        from spar.gui.files import SearchPanel

        panel = SearchPanel(proj)
        qtbot.addWidget(panel)
        panel.query.setText("cat")
        panel._run_search()
        qtbot.waitUntil(
            lambda: panel.results.topLevelItemCount() == 1, timeout=5000
        )
        panel.replace.setText("dog")
        panel._apply_replace()
        assert target.read_text(encoding="utf-8") == "cat here\n"  # untouched
        assert link.is_symlink()
        assert "pominięto 1 (dowiązanie poza projektem)" in panel.status.text()

    def test_replace_via_symlink_alias_of_dirty_tab_is_skipped(self, qtbot, tmp_path):
        # review #29: the target is open DIRTY under its real path while
        # the results row reaches it through a symlink alias. Comparing
        # unresolved strings would miss it and overwrite the dirty file —
        # resolved-vs-resolved comparison skips it as niezapisane zmiany.
        # The target lives in a skip-dir so only the LINK row appears.
        import os

        (tmp_path / "node_modules").mkdir()
        real = tmp_path / "node_modules" / "real.txt"
        real.write_text("cat here\n", encoding="utf-8")
        link = tmp_path / "link.txt"
        os.symlink(real, link)
        from spar.gui.files import SearchPanel

        panel = SearchPanel(tmp_path)
        qtbot.addWidget(panel)
        panel.dirty_open_paths = lambda: {str(real)}  # dirty under REAL path
        panel.query.setText("cat")
        panel._run_search()
        qtbot.waitUntil(
            lambda: panel.results.topLevelItemCount() == 1, timeout=5000
        )
        panel.replace.setText("dog")
        panel._apply_replace()
        assert real.read_text(encoding="utf-8") == "cat here\n"  # untouched
        assert "pominięto 1 (niezapisane zmiany)" in panel.status.text()

    def test_symlink_loop_row_skipped_without_aborting_batch(self, qtbot, tmp_path):
        # review #30: resolving a symlink loop raises (RuntimeError on
        # older Pythons, OSError/ELOOP via stat elsewhere). It must be
        # caught PER ROW — counted as błąd zapisu — while the rest of the
        # batch is still replaced, never abort _apply_replace wholesale.
        import os

        panel = self._panel(qtbot, tmp_path)
        self._search(qtbot, panel)  # rows for a.py and b.py
        # a.py becomes a self-referential symlink AFTER the scan.
        (tmp_path / "a.py").unlink()
        os.symlink(tmp_path / "a.py", tmp_path / "a.py")
        panel.replace.setText("dog")
        panel._apply_replace()  # must NOT raise
        assert (tmp_path / "b.py").read_text(encoding="utf-8") == "dog only\n"
        assert "błąd zapisu" in panel.status.text()

    def test_skip_warning_survives_refresh(self, qtbot, tmp_path):
        # review #16: the replace summary (with skip warnings) must persist
        # through the async refresh search — its _on_finished appends counts
        # to the summary instead of overwriting it.
        panel = self._panel(qtbot, tmp_path)
        panel.dirty_open_paths = lambda: {str(tmp_path / "a.py")}
        self._search(qtbot, panel)
        panel.replace.setText("dog")
        panel._apply_replace()
        # let the refresh search complete (its finished fires on a later turn)
        qtbot.waitUntil(lambda: "wyników" in panel.status.text(), timeout=5000)
        assert "pominięto 1" in panel.status.text()  # skip warning survived

    def test_rows_created_while_read_only_not_checkable(self, qtbot, tmp_path):
        # review #11: rows built while replace is disabled must not be
        # user-checkable.
        from PySide6.QtCore import Qt

        panel = self._panel(qtbot, tmp_path)
        panel.set_replace_enabled(False)
        self._search(qtbot, panel)
        item = panel.results.topLevelItem(0)
        assert not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable)


class TestEditorFindBar:
    def _tab(self, qtbot, tmp_path, text="alpha beta alpha gamma alpha\n"):
        from spar.gui.files import EditorTab

        f = tmp_path / "a.py"
        f.write_text(text, encoding="utf-8")
        tab = EditorTab(f)
        qtbot.addWidget(tab)
        return tab

    def test_open_prefills_and_shows(self, qtbot, tmp_path):
        tab = self._tab(qtbot, tmp_path)
        tab.open_find(prefill="alpha")
        assert tab.find_bar.isHidden() is False
        assert tab.find_bar.find_field.text() == "alpha"

    def test_find_next_wraps(self, qtbot, tmp_path):
        tab = self._tab(qtbot, tmp_path)
        bar = tab.find_bar
        bar.open("alpha")
        assert bar.find_next() is True
        first = tab.editor.textCursor().selectedText()
        assert first == "alpha"
        bar.find_next()
        bar.find_next()
        # a 4th next wraps back to the first occurrence (only 3 exist)
        assert bar.find_next() is True

    def test_highlight_all_marks_every_match(self, qtbot, tmp_path):
        from PySide6.QtGui import QColor, QTextFormat

        from spar.gui.theme import TOKENS

        tab = self._tab(qtbot, tmp_path)
        bar = tab.find_bar
        bar.open("alpha")
        bar.find_next()  # triggers highlight-all
        sels = tab.editor.extraSelections()
        # review #10: current-line FIRST, 3 match selections AFTER.
        assert len(sels) == 4
        warn = QColor(TOKENS["warn"])
        match_bgs = [s.format.background().color() for s in sels[1:]]
        assert all(c == warn for c in match_bgs)  # assert formats, not count
        # the current-line band is full-width; the matches are not
        assert sels[0].format.property(
            QTextFormat.Property.FullWidthSelection
        )

    def test_f3_and_shift_f3_wired(self, qtbot, tmp_path):
        # review #7: F3 / Shift+F3 map to next/prev (emit-pin the shortcuts).
        tab = self._tab(qtbot, tmp_path)
        bar = tab.find_bar
        bar.open("alpha")
        bar._f3.activated.emit()
        assert tab.editor.textCursor().selectedText() == "alpha"
        first = tab.editor.textCursor().selectionStart()
        bar._f3.activated.emit()
        assert tab.editor.textCursor().selectionStart() > first  # moved on
        bar._shift_f3.activated.emit()
        assert tab.editor.textCursor().selectionStart() == first  # back

    def test_real_f3_keypress_navigates_next_and_prev(self, qtbot, tmp_path):
        # review #26 (both-halves rule): the emit-pin above proves the
        # signal→slot wiring; this proves the REAL key half — a physical
        # F3 / Shift+F3 pressed while a QLineEdit child holds focus must
        # reach the bar (WidgetWithChildrenShortcut) instead of being
        # swallowed by the line edit.
        from PySide6.QtCore import Qt

        tab = self._tab(qtbot, tmp_path)
        tab.show()
        qtbot.waitExposed(tab)
        bar = tab.find_bar
        bar.open("alpha")  # focuses find_field (a child QLineEdit)
        assert bar.find_field.hasFocus()
        qtbot.keyClick(bar.find_field, Qt.Key.Key_F3)
        assert tab.editor.textCursor().selectedText() == "alpha"
        first = tab.editor.textCursor().selectionStart()
        qtbot.keyClick(bar.find_field, Qt.Key.Key_F3)
        assert tab.editor.textCursor().selectionStart() > first  # next
        qtbot.keyClick(
            bar.find_field,
            Qt.Key.Key_F3,
            Qt.KeyboardModifier.ShiftModifier,
        )
        assert tab.editor.textCursor().selectionStart() == first  # prev

    def test_case_insensitive_uses_regex_not_lower(self, qtbot, tmp_path):
        # review #8: "İ".lower() is two code points; a lower()-based scan
        # would desync offsets. A regex IGNORECASE search stays aligned.
        tab = self._tab(qtbot, tmp_path, text="x İ y İ\n")
        bar = tab.find_bar
        bar.open("i̇")  # combining form should NOT match; İ literal should
        bar.find_field.setText("İ")
        assert bar.find_next() is True
        assert tab.editor.textCursor().selectedText() == "İ"

    def test_non_bmp_span_selects_correct_range(self, qtbot, tmp_path):
        # review #8: a match after a non-BMP char (😀) must select the right
        # UTF-16 range, not a code-point-shifted one.
        tab = self._tab(qtbot, tmp_path, text="😀 alpha\n")
        bar = tab.find_bar
        bar.open("alpha")
        assert bar.find_next() is True
        assert tab.editor.textCursor().selectedText() == "alpha"

    def test_replace_one(self, qtbot, tmp_path):
        tab = self._tab(qtbot, tmp_path)
        bar = tab.find_bar
        bar.open("alpha")
        bar.replace_field.setText("X")
        bar.find_next()
        bar.replace_one()
        assert tab.editor.toPlainText().startswith("X beta alpha")

    def test_replace_all(self, qtbot, tmp_path):
        tab = self._tab(qtbot, tmp_path)
        bar = tab.find_bar
        bar.open("alpha")
        bar.replace_field.setText("X")
        assert bar.replace_all() == 3
        assert "alpha" not in tab.editor.toPlainText()

    def test_replace_disabled_when_read_only(self, qtbot, tmp_path):
        tab = self._tab(qtbot, tmp_path)
        tab.set_read_only(True)
        tab.open_find("alpha")
        assert tab.find_bar.replace_field.isEnabled() is False
        assert tab.find_bar.replace_all_button.isEnabled() is False

    def test_read_only_toggled_while_bar_open_updates_controls(self, qtbot, tmp_path):
        # review #7: locking the editor while the find bar is already open
        # must disable its replace controls (not just on next open).
        tab = self._tab(qtbot, tmp_path)
        tab.open_find("alpha")
        assert tab.find_bar.replace_field.isEnabled() is True
        tab.set_read_only(True)
        assert tab.find_bar.replace_field.isEnabled() is False
        assert tab.find_bar.replace_button.isEnabled() is False
        tab.set_read_only(False)
        assert tab.find_bar.replace_field.isEnabled() is True

    def test_current_line_highlight_survives(self, qtbot, tmp_path):
        # Tranche-A invariant: opening the find bar must not drop the
        # current-line highlight.
        tab = self._tab(qtbot, tmp_path)
        tab.editor.set_match_selections([])
        assert len(tab.editor.extraSelections()) == 1  # current line only


class TestFilesViewSearchWiring:
    def _view(self, qtbot, tmp_path):
        from spar.gui.files import FilesView

        (tmp_path / "app.py").write_text("todo one\nplain\n", encoding="utf-8")
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "HEAD").write_text("ref\n")
        view = FilesView(tmp_path)
        qtbot.addWidget(view)
        return view

    def test_open_search_shows_dialog_and_focuses(self, qtbot, tmp_path):
        view = self._view(qtbot, tmp_path)
        assert view.search_dialog.isVisible() is False  # hidden by default
        view.open_search()
        assert view.search_dialog.isVisible() is True
        assert view.search_dialog.windowTitle() == "Szukaj w plikach"
        assert view.search_dialog.isModal() is False
        # the query field gets focus (with any existing text preselected)
        assert view.search_dialog.focusWidget() is view.search_panel.query

    def test_open_search_preselects_existing_query_text(self, qtbot, tmp_path):
        view = self._view(qtbot, tmp_path)
        view.search_panel.query.setText("todo")
        view.open_search()
        assert view.search_panel.query.selectedText() == "todo"

    def test_second_open_search_is_not_a_toggle(self, qtbot, tmp_path):
        # a second Ctrl+Shift+F raises/focuses the dialog; it must NOT close
        # it (Esc closes).
        view = self._view(qtbot, tmp_path)
        view.open_search()
        view.open_search()
        assert view.search_dialog.isVisible() is True

    def test_esc_hides_dialog_without_stopping_session(self, qtbot, tmp_path):
        from PySide6.QtCore import Qt

        view = self._view(qtbot, tmp_path)
        view.open_search()
        view.search_panel.query.setText("todo")
        view.search_panel._run_search()
        qtbot.waitUntil(
            lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
        )
        session = view.search_panel._session
        assert session._started is True
        qtbot.keyClick(view.search_dialog, Qt.Key.Key_Escape)
        assert view.search_dialog.isVisible() is False
        # the session/thread stays alive for reopen — teardown belongs to
        # the owning FilesView, not the dialog.
        assert session._stopped is False
        assert session._thread.isRunning() is True
        # reopen + a NEW search still works on the same session
        view.open_search()
        assert view.search_dialog.isVisible() is True
        view.search_panel.query.setText("plain")
        view.search_panel._run_search()
        qtbot.waitUntil(
            lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
        )
        item = view.search_panel.results.topLevelItem(0)
        assert item.childCount() == 1

    def test_result_activation_hides_dialog(self, qtbot, tmp_path):
        view = self._view(qtbot, tmp_path)
        view.open_search()
        view.search_panel.query.setText("plain")
        view.search_panel._run_search()
        qtbot.waitUntil(
            lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
        )
        line_item = view.search_panel.results.topLevelItem(0).child(0)
        view.search_panel._on_item_activated(line_item, 0)
        assert view.tabs.currentWidget().path.name == "app.py"
        assert view.search_dialog.isVisible() is False

    def test_dialog_geometry_round_trips_via_settings(self, qtbot, tmp_path):
        from spar.gui.files import FilesView

        view = self._view(qtbot, tmp_path)
        view.open_search()
        view.search_dialog.resize(640, 420)
        view.search_dialog.hide()  # hide persists geometry
        other = FilesView(tmp_path)
        qtbot.addWidget(other)
        other.open_search()
        assert other.search_dialog.width() == 640
        assert other.search_dialog.height() == 420

    def test_open_at_positions_cursor_and_selects_span(self, qtbot, tmp_path):
        view = self._view(qtbot, tmp_path)
        view.open_at(tmp_path / "app.py", 1, 0, 4)  # "todo"
        ed = view.tabs.currentWidget().editor
        assert ed.textCursor().selectedText() == "todo"

    def test_search_open_location_opens_tab_at_line(self, qtbot, tmp_path):
        view = self._view(qtbot, tmp_path)
        view.open_search()
        view.search_panel.query.setText("plain")
        view.search_panel._run_search()
        qtbot.waitUntil(
            lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
        )
        line_item = view.search_panel.results.topLevelItem(0).child(0)
        view.search_panel._on_item_activated(line_item, 0)
        assert view.tabs.currentWidget().path.name == "app.py"
        assert view.tabs.currentWidget().editor.textCursor().selectedText() == "plain"

    def test_read_only_disables_replace_keeps_search(self, qtbot, tmp_path):
        from spar.gui.runner import RunnerState

        view = self._view(qtbot, tmp_path)
        # Give the panel live results so the replace button is not disabled
        # purely on staleness grounds (review #5).
        view.open_search()
        view.search_panel.query.setText("todo")
        view.search_panel._run_search()
        qtbot.waitUntil(
            lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
        )
        view.set_state(RunnerState.RUNNING)
        assert view.search_panel.replace_button.isEnabled() is False
        assert view.search_panel.query.isEnabled() is True
        view.set_state(RunnerState.IDLE)
        assert view.search_panel.replace_button.isEnabled() is True

    def test_dirty_open_paths_reports_unsaved_tabs(self, qtbot, tmp_path):
        view = self._view(qtbot, tmp_path)
        view.open_file(tmp_path / "app.py")
        ed = view.tabs.currentWidget().editor
        ed.setPlainText("dirty\n")
        ed.document().setModified(True)  # #9
        assert str(tmp_path / "app.py") in view.search_panel.dirty_open_paths()

    def test_find_in_files_shortcut_wired(self, qtbot, tmp_path):
        # emit-based pin (the Ctrl+S lesson): the QShortcut→open_search
        # connection must be exercised even though offscreen never routes
        # the real chord through the shortcut map.
        view = self._view(qtbot, tmp_path)
        view._find_in_files_shortcut.activated.emit()
        assert view.search_dialog.isVisible() is True

    def test_ctrl_shift_f_real_chord_opens_search(self, qtbot, tmp_path):
        # real-chord half: deliver Ctrl+Shift+F to an editor; the
        # eventFilter bridge must open the dialog offscreen.
        view = self._view(qtbot, tmp_path)
        view.open_file(tmp_path / "app.py")
        view.show()
        ed = view.tabs.currentWidget().editor
        ed.setFocus()
        qtbot.keyClick(
            ed, Qt.Key.Key_F,
            Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
        )
        assert view.search_dialog.isVisible() is True

    def test_find_in_editor_shortcut_wired(self, qtbot, tmp_path):
        view = self._view(qtbot, tmp_path)
        view.open_file(tmp_path / "app.py")
        view._find_in_editor_shortcut.activated.emit()
        assert view.tabs.currentWidget().find_bar.isHidden() is False

    def test_ctrl_f_real_chord_opens_editor_find_bar(self, qtbot, tmp_path):
        # review #7: the real-chord Ctrl+F half — the eventFilter bridge must
        # open the editor find bar offscreen (mirrors the Ctrl+S lesson).
        view = self._view(qtbot, tmp_path)
        view.open_file(tmp_path / "app.py")
        view.show()
        ed = view.tabs.currentWidget().editor
        ed.setFocus()
        qtbot.keyClick(ed, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        assert view.tabs.currentWidget().find_bar.isHidden() is False

    def test_replace_reloads_open_clean_tab(self, qtbot, tmp_path):
        # review #11: replace-in-files rewrites disk; the open CLEAN tab must
        # auto-reload via the watcher (real disk write, mirroring tranche A).
        view = self._view(qtbot, tmp_path)
        view.open_file(tmp_path / "app.py")   # open + clean (not dirty)
        ed = view.tabs.currentWidget().editor
        view.open_search()
        view.search_panel.query.setText("todo")
        view.search_panel._run_search()
        qtbot.waitUntil(
            lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
        )
        view.search_panel.replace.setText("DONE")
        view.search_panel._apply_replace()
        # the watcher reload lands on a later event-loop turn
        qtbot.waitUntil(lambda: "DONE" in ed.toPlainText(), timeout=5000)
        assert "todo" not in ed.toPlainText()

    def test_close_standalone_view_stops_search_thread(self, qtbot, tmp_path):
        # review #21: closing a STANDALONE FilesView (no MainWindow) must
        # stop the SearchPanel's QThread via closeEvent → stop_search();
        # without it the started thread outlives the closed widget.
        view = self._view(qtbot, tmp_path)
        view.open_search()
        view.search_panel.query.setText("todo")
        view.search_panel._run_search()
        qtbot.waitUntil(
            lambda: view.search_panel.results.topLevelItemCount() == 1, timeout=5000
        )
        session = view.search_panel._session
        assert session._started is True   # the thread actually ran
        view.close()
        assert session._stopped is True   # closeEvent tore it down
        assert view.search_dialog.isVisible() is False  # dialog closed too
        qtbot.waitUntil(lambda: not session._thread.isRunning(), timeout=5000)


class TestSearchFileMask:
    """WebStorm-style file mask: checkbox + editable combo restricting the
    searched file set (comma-separated basename globs, e.g. *.ts, *.tsx)."""

    @pytest.fixture(autouse=True)
    def _hermetic_qsettings(self):
        # Same convention as test_gui_app.py: QSettings caches its config
        # path at first use, so the conftest HOME isolation is not enough —
        # clear the shared spar/gui store so mask history is hermetic.
        from PySide6.QtCore import QSettings

        QSettings("spar", "gui").clear()
        yield

    def _write_fixture(self, tmp_path):
        (tmp_path / "a.py").write_text("todo py\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("todo txt\n", encoding="utf-8")

    def _panel(self, qtbot, tmp_path, session=None):
        from spar.gui.files import SearchPanel

        self._write_fixture(tmp_path)
        panel = SearchPanel(tmp_path, session=session)
        qtbot.addWidget(panel)
        return panel

    def test_fresh_panel_mask_unchecked_and_combo_disabled(self, qtbot, tmp_path):
        # The checkbox state is NOT persisted — a fresh panel always
        # starts unchecked, with the combo disabled until checked.
        panel = self._panel(qtbot, tmp_path)
        assert panel.mask_check.isChecked() is False
        assert panel.mask_combo.isEnabled() is False
        panel.mask_check.setChecked(True)
        assert panel.mask_combo.isEnabled() is True

    def test_masked_search_scans_only_matching_files(self, qtbot, tmp_path):
        # Mask *.py → only a.py appears in the tree AND (spy) the
        # non-matching b.txt is never even read — the mask filters the
        # indexed file list BEFORE guards/engines.
        import spar.gui.files as fmod
        from spar.gui.files import SearchPanel, SearchSession

        self._write_fixture(tmp_path)
        seen: list[str] = []

        def spy(root, rel, pattern, limit=None):
            seen.append(rel)
            return fmod.search_file(root, rel, pattern, limit=limit)

        session = SearchSession(tmp_path, scan_file=spy)
        panel = SearchPanel(tmp_path, session=session)
        qtbot.addWidget(panel)
        panel.mask_check.setChecked(True)
        panel.mask_combo.setEditText("*.py")
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(lambda: panel._results_spec is not None, timeout=5000)
        assert panel.results.topLevelItemCount() == 1
        assert panel.results.topLevelItem(0).text(0).startswith("a.py")
        assert "b.txt" not in seen  # never scanned, not just filtered out

    def test_unchecked_mask_combo_is_ignored(self, qtbot, tmp_path):
        # A mask typed into the combo has NO effect while unchecked.
        panel = self._panel(qtbot, tmp_path)
        panel.mask_combo.setEditText("*.py")
        assert panel.mask_check.isChecked() is False
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(
            lambda: panel.results.topLevelItemCount() == 2, timeout=5000
        )

    def test_mask_drift_after_search_disables_replace(self, qtbot, tmp_path):
        # Editing the mask after a search is spec drift, exactly like
        # editing the query: replace disables with the existing tooltip.
        panel = self._panel(qtbot, tmp_path)
        panel.mask_check.setChecked(True)
        panel.mask_combo.setEditText("*.py")
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(lambda: panel._results_spec is not None, timeout=5000)
        assert panel.replace_button.isEnabled() is True
        panel.mask_combo.setEditText("*.txt")  # drift
        assert panel.replace_button.isEnabled() is False
        assert "nieaktualne" in panel.replace_button.toolTip()

    def test_replace_honors_stored_mask_and_noops_on_drift(self, qtbot, tmp_path):
        # Even a forced _apply_replace targets the STORED spec — a drifted
        # mask means no file is touched.
        panel = self._panel(qtbot, tmp_path)
        panel.mask_check.setChecked(True)
        panel.mask_combo.setEditText("*.py")
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(lambda: panel._results_spec is not None, timeout=5000)
        panel.replace.setText("done")
        panel.mask_combo.setEditText("*.txt")  # drift from stored spec
        panel._apply_replace()                 # guarded no-op
        assert (tmp_path / "a.py").read_text(encoding="utf-8") == "todo py\n"
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "todo txt\n"

    def test_mask_history_round_trip(self, qtbot, tmp_path):
        # A successful masked dispatch persists the mask; a NEW panel loads
        # it into the combo — but starts UNCHECKED (state not persisted).
        from spar.gui.files import SearchPanel

        panel = self._panel(qtbot, tmp_path)
        panel.mask_check.setChecked(True)
        panel.mask_combo.setEditText("*.py")
        panel.query.setText("todo")
        panel._run_search()
        qtbot.waitUntil(lambda: panel._results_spec is not None, timeout=5000)
        assert panel._mask_history() == ["*.py"]
        panel2 = SearchPanel(tmp_path)
        qtbot.addWidget(panel2)
        items = [panel2.mask_combo.itemText(i)
                 for i in range(panel2.mask_combo.count())]
        assert items == ["*.py"]
        assert panel2.mask_check.isChecked() is False  # not persisted

    def test_mask_history_dedups_and_keeps_last_eight(self, qtbot, tmp_path):
        panel = self._panel(qtbot, tmp_path)
        for i in range(10):
            panel._push_mask_history(f"*.m{i}")
        panel._push_mask_history("*.m5")  # dedup: moves to front
        hist = panel._mask_history()
        assert len(hist) == 8
        assert hist[0] == "*.m5"
        assert hist.count("*.m5") == 1
        assert "*.m0" not in hist and "*.m1" not in hist  # oldest dropped
