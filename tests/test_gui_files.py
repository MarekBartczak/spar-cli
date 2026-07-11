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
