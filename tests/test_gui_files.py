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
