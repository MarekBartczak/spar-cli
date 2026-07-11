"""Tests for the shared spar.gui.conversation layer.

Pure parse_options coverage lives in test_gui_grill.py (re-exported symbol);
here we test the base ConversationSession via a minimal concrete subclass and
a scripted fake adapter (no real claude subprocess).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    import PySide6  # noqa: F401

    from spar.adapters.base import AdapterError, SessionLost, TurnResult
    from spar.config import SideConfig
    from spar.gui.conversation import ConversationSession, Option

    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False


def _reply(text, session_id="sess-1", chunks=None):
    def _step(prompt, sid, on_event):
        for c in chunks or []:
            if on_event:
                on_event(c)
        return TurnResult(
            session_id=session_id, reply_text=text,
            events_path=Path("events.json"), exit_code=0,
        )
    return _step


def _raise(exc):
    def _step(prompt, sid, on_event):
        raise exc
    return _step


if _HAS_QT:

    class FakeAdapter:
        name = "claude"

        def __init__(self, steps):
            self.steps = list(steps)
            self.calls = []
            self._idx = 0

        def run_turn(self, prompt, session_id, timeout_sec, on_event=None):
            self.calls.append(
                SimpleNamespace(prompt=prompt, session_id=session_id,
                                timeout_sec=timeout_sec, on_event=on_event)
            )
            step = self.steps[self._idx]
            self._idx += 1
            return step(prompt, session_id, on_event)

    class _ProbeSession(ConversationSession):
        """Minimal concrete subclass: records extras it was handed."""

        def __init__(self, *a, **k):
            self.extras = []
            super().__init__(*a, **k)

        def _default_adapter_factory(self):  # pragma: no cover - unused (factory injected)
            raise AssertionError("test always injects an adapter_factory")

        def _handle_extra(self, extra):
            self.extras.append(extra)


@pytest.fixture
def make_probe(qtbot):
    created = []

    def _make(project_dir, adapter, timeout_sec=60, initial_session_id=None):
        sess = _ProbeSession(
            Path(project_dir), SideConfig(adapter="claude", command="claude"),
            timeout_sec, adapter_factory=lambda: adapter,
            initial_session_id=initial_session_id,
        )
        created.append(sess)
        return sess

    yield _make
    for sess in created:
        sess.stop()
        try:
            sess._thread.wait(3000)
        except RuntimeError:
            pass


@pytest.mark.skipif(not _HAS_QT, reason="requires PySide6")
class TestConversationSession:
    def test_send_fresh_then_resume_tracks_session_id(self, tmp_path, qtbot, make_probe):
        adapter = FakeAdapter([_reply("A. a\nB. b", session_id="s1"),
                               _reply("ok", session_id="s2")])
        sess = make_probe(tmp_path, adapter)
        with qtbot.waitSignal(sess.turn_finished, timeout=3000) as b:
            sess.send("hello", reset=True)
        assert b.args[1] == [Option("A", "a"), Option("B", "b")]
        assert adapter.calls[0].session_id is None
        assert sess.session_id == "s1"
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.send("more")
        assert adapter.calls[1].session_id == "s1"
        assert sess.session_id == "s2"

    def test_initial_session_id_resumes_on_first_send(self, tmp_path, qtbot, make_probe):
        adapter = FakeAdapter([_reply("ok", session_id="s9")])
        sess = make_probe(tmp_path, adapter, initial_session_id="restored")
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.send("q")  # reset defaults False -> resumes the restored id
        assert adapter.calls[0].session_id == "restored"

    def test_stream_chunks_reach_public_signal(self, tmp_path, qtbot, make_probe):
        adapter = FakeAdapter([_reply("x", chunks=["a", "b"])])
        sess = make_probe(tmp_path, adapter)
        got = []
        sess.stream_chunk.connect(got.append)
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.send("q", reset=True)
        assert got == ["a", "b"]

    def test_session_lost_signal(self, tmp_path, qtbot, make_probe):
        adapter = FakeAdapter([_reply("x", session_id="s1"),
                               _raise(SessionLost("dead"))])
        sess = make_probe(tmp_path, adapter)
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.send("q", reset=True)
        with qtbot.waitSignal(sess.session_lost, timeout=3000):
            sess.send("q2")

    def test_turn_failed_signal(self, tmp_path, qtbot, make_probe):
        adapter = FakeAdapter([_raise(AdapterError("boom"))])
        sess = make_probe(tmp_path, adapter)
        with qtbot.waitSignal(sess.turn_failed, timeout=3000) as f:
            sess.send("q", reset=True)
        assert "boom" in f.args[0]

    def test_stop_from_turn_finished_subscriber_suppresses_extra(
        self, tmp_path, qtbot, make_probe
    ):
        # Review #9 re-entrancy hole: a subscriber that calls stop() from
        # inside turn_finished must NOT then receive _handle_extra for the
        # abandoned generation. The _ProbeSession records every extra it is
        # handed; after a stop() re-entrant on turn_finished it must record
        # none.
        adapter = FakeAdapter([_reply("done", session_id="s1")])
        sess = make_probe(tmp_path, adapter)
        sess.turn_finished.connect(lambda *_: sess.stop())
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.send("q", reset=True)
        qtbot.wait(50)
        assert sess.extras == []  # extra suppressed after the re-entrant stop()
