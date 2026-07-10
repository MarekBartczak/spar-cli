"""Tests for ``spar.gui.grill``.

Two layers:

* ``parse_options`` / ``OPENING_PROMPT_TEMPLATE`` — pure, Qt-free; these tests
  run on every interpreter (including ``python3`` without the ``gui`` extra).
* ``GrillSession`` facade + ``_GrillWorker`` — require PySide6 + pytest-qt and
  are skipped when PySide6 is unavailable.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from spar.gui.grill import OPENING_PROMPT_TEMPLATE, Option, parse_options


# ----------------------------------------------------------------------
# Pure: parse_options (no Qt)
# ----------------------------------------------------------------------
class TestParseOptions:
    def test_plain_lettered_lines(self):
        reply = "A. foo\nB. bar\nC. baz"
        assert parse_options(reply) == [
            Option("A", "foo"),
            Option("B", "bar"),
            Option("C", "baz"),
        ]

    def test_paren_delimiter(self):
        assert parse_options("A) one\nB) two") == [
            Option("A", "one"),
            Option("B", "two"),
        ]

    def test_spike_shape_with_midline_bold_closer(self):
        # Real turn-1 shape from the live spike: list marker + bold wrapping the
        # letter AND a mid-line ``**`` closer after the option name.
        reply = (
            "Pytanie: która strategia?\n"
            "\n"
            "- **A. Explicit registry** — każdy handler zarejestrowany ręcznie\n"
            "- **B. Implicit fallback** — automatyczne wykrycie po nazwie\n"
            "\n"
            "Rekomendacja: B."
        )
        assert parse_options(reply) == [
            Option("A", "Explicit registry — każdy handler zarejestrowany ręcznie"),
            Option("B", "Implicit fallback — automatyczne wykrycie po nazwie"),
        ]

    def test_no_options_returns_empty(self):
        assert parse_options("Just a paragraph with no choices at all.") == []

    def test_non_contiguous_letters_ignored(self):
        # Starts at B (no A) — not a contiguous-from-A run.
        assert parse_options("B. second\nC. third") == []

    def test_gap_in_letters_truncates_at_break(self):
        # A, B contiguous then D — only the contiguous prefix would be valid,
        # but a single block with a gap is NOT contiguous, so it is rejected.
        assert parse_options("A. one\nB. two\nD. four") == []

    def test_two_blocks_last_wins_no_stale_letter_leak(self):
        # An earlier block ends at C; a later block restarts at A/B. Only the
        # LAST contiguous-from-A block is returned — C must not leak in.
        reply = (
            "A. old-one\n"
            "B. old-two\n"
            "C. old-three\n"
            "\n"
            "Po namyśle, nowe opcje:\n"
            "\n"
            "A. new-one\n"
            "B. new-two"
        )
        assert parse_options(reply) == [
            Option("A", "new-one"),
            Option("B", "new-two"),
        ]

    def test_single_continuation_line_keeps_block(self):
        # One intervening non-option line does not break the block.
        reply = "A. one\n   (still A)\nB. two"
        assert parse_options(reply) == [Option("A", "one"), Option("B", "two")]

    def test_bold_only_label_stripped_everywhere(self):
        assert parse_options("A. **fully bold**") == [Option("A", "fully bold")]


class TestOpeningPromptTemplate:
    def test_embeds_draft_and_requires_lettered_options(self):
        out = OPENING_PROMPT_TEMPLATE.format(draft="Zbuduj X")
        assert '"Zbuduj X".' in out
        assert "grill-with-docs" in out
        assert "LITERAMI (A., B., C., ...)" in out
        assert ".spar/requirements.md" in out


# ----------------------------------------------------------------------
# Qt: GrillSession facade + _GrillWorker
# ----------------------------------------------------------------------
try:
    import PySide6  # noqa: F401

    from spar.adapters.base import AdapterError, SessionLost, TurnResult
    from spar.config import SideConfig
    from spar.gui.grill import GrillSession

    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False


def _reply(text, session_id="sess-1", chunks=None):
    def _step(prompt, sid, on_event):
        for c in chunks or []:
            if on_event:
                on_event(c)
        return TurnResult(
            session_id=session_id,
            reply_text=text,
            events_path=Path("events.json"),
            exit_code=0,
        )

    return _step


def _writes_req(path, content, text="Zapisane. GOTOWE", session_id="sess-1"):
    def _step(prompt, sid, on_event):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return TurnResult(
            session_id=session_id,
            reply_text=text,
            events_path=Path("events.json"),
            exit_code=0,
        )

    return _step


def _raise(exc):
    def _step(prompt, sid, on_event):
        raise exc

    return _step


if _HAS_QT:

    class FakeAdapter:
        """Scripted adapter recording prompts, session ids and on_event."""

        name = "claude"

        def __init__(self, steps):
            self.steps = list(steps)
            self.calls = []
            self._idx = 0

        def run_turn(self, prompt, session_id, timeout_sec, on_event=None):
            self.calls.append(
                SimpleNamespace(
                    prompt=prompt,
                    session_id=session_id,
                    timeout_sec=timeout_sec,
                    on_event=on_event,
                )
            )
            step = self.steps[self._idx]
            self._idx += 1
            return step(prompt, session_id, on_event)


@pytest.fixture
def make_session(qtbot):
    created = []

    def _make(project_dir, adapter, timeout_sec=60, side_cfg=None):
        if side_cfg is None:
            side_cfg = SideConfig(adapter="claude", command="claude")
        sess = GrillSession(
            Path(project_dir),
            side_cfg,
            timeout_sec,
            adapter_factory=lambda: adapter,
        )
        created.append(sess)
        return sess

    yield _make

    for sess in created:
        sess.stop()
        sess._thread.wait(3000)


@pytest.mark.skipif(not _HAS_QT, reason="requires PySide6")
class TestGrillSession:
    def test_start_sends_template_with_draft_and_parses_options(
        self, tmp_path, qtbot, make_session
    ):
        adapter = FakeAdapter([_reply("A. tak\nB. nie", session_id="sess-1")])
        sess = make_session(tmp_path, adapter)

        with qtbot.waitSignal(sess.turn_finished, timeout=3000) as blocker:
            sess.start("Zbuduj X")

        reply_text, options = blocker.args
        assert options == [Option("A", "tak"), Option("B", "nie")]
        assert adapter.calls[0].session_id is None
        assert '"Zbuduj X".' in adapter.calls[0].prompt
        assert "grill-with-docs" in adapter.calls[0].prompt

    def test_answer_resumes_with_stored_session_id(
        self, tmp_path, qtbot, make_session
    ):
        adapter = FakeAdapter(
            [
                _reply("A. jeden", session_id="sess-1"),
                _reply("A. dwa", session_id="sess-2"),
            ]
        )
        sess = make_session(tmp_path, adapter)

        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.start("draft")
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.answer("B")

        assert adapter.calls[1].session_id == "sess-1"
        assert adapter.calls[1].prompt == "B"

    def test_streaming_chunks_reach_public_signal(
        self, tmp_path, qtbot, make_session
    ):
        adapter = FakeAdapter(
            [_reply("A. x", session_id="s1", chunks=["myśli...", "prawie"])]
        )
        sess = make_session(tmp_path, adapter)

        received = []
        sess.stream_chunk.connect(received.append)
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.start("draft")

        assert received == ["myśli...", "prawie"]

    def test_requirements_created_emits_content(
        self, tmp_path, qtbot, make_session
    ):
        req = tmp_path / ".spar" / "requirements.md"
        content = "# Wymagania\n\nTreść.\n\n## Tasks\n- a\n"
        adapter = FakeAdapter([_writes_req(req, content)])
        sess = make_session(tmp_path, adapter)

        with qtbot.waitSignal(sess.requirements_ready, timeout=3000) as blocker:
            sess.start("draft")

        assert blocker.args[0] == content

    def test_preexisting_unchanged_requirements_no_signal(
        self, tmp_path, qtbot, make_session
    ):
        req = tmp_path / ".spar" / "requirements.md"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text("OLD CONTENT\n", encoding="utf-8")
        # Turn does not touch the file.
        adapter = FakeAdapter([_reply("A. x", session_id="s1")])
        sess = make_session(tmp_path, adapter)

        ready = []
        sess.requirements_ready.connect(ready.append)
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.start("draft")
        qtbot.wait(100)

        assert ready == []

    def test_changed_requirements_content_emits(
        self, tmp_path, qtbot, make_session
    ):
        req = tmp_path / ".spar" / "requirements.md"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text("OLD CONTENT\n", encoding="utf-8")
        new_content = "NEW CONTENT\n\n## Tasks\n- t\n"
        adapter = FakeAdapter([_writes_req(req, new_content)])
        sess = make_session(tmp_path, adapter)

        with qtbot.waitSignal(sess.requirements_ready, timeout=3000) as blocker:
            sess.start("draft")

        assert blocker.args[0] == new_content

    def test_turn_failed_then_retry_reuses_session(
        self, tmp_path, qtbot, make_session
    ):
        adapter = FakeAdapter(
            [
                _reply("A. q1", session_id="sess-1"),
                _raise(AdapterError("boom")),
                _reply("A. q2", session_id="sess-1"),
            ]
        )
        sess = make_session(tmp_path, adapter)

        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.start("draft")
        with qtbot.waitSignal(sess.turn_failed, timeout=3000) as fail:
            sess.answer("B")
        assert "boom" in fail.args[0]
        # Session still alive: retry the same answer.
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.answer("B")

        assert adapter.calls[1].session_id == "sess-1"
        assert adapter.calls[2].session_id == "sess-1"

    def test_session_lost_then_fresh_start(self, tmp_path, qtbot, make_session):
        adapter = FakeAdapter(
            [
                _reply("A. q1", session_id="sess-1"),
                _raise(SessionLost("resume died")),
                _reply("A. q2", session_id="sess-2"),
            ]
        )
        sess = make_session(tmp_path, adapter)

        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.start("draft")
        with qtbot.waitSignal(sess.session_lost, timeout=3000):
            sess.answer("B")
        # Restart grilla → a FRESH session (session_id None again).
        with qtbot.waitSignal(sess.turn_finished, timeout=3000):
            sess.start("draft")

        assert adapter.calls[2].session_id is None

    def test_stop_suppresses_all_public_signals_mid_turn(
        self, tmp_path, qtbot, make_session
    ):
        started = threading.Event()
        release = threading.Event()

        def blocking(prompt, sid, on_event):
            started.set()
            release.wait(5)
            # Late chunk emitted by the in-flight turn AFTER stop().
            if on_event:
                on_event("late chunk after stop")
            return TurnResult(
                session_id="s1",
                reply_text="A. foo\nB. bar",
                events_path=Path("events.json"),
                exit_code=0,
            )

        adapter = FakeAdapter([blocking])
        sess = make_session(tmp_path, adapter)

        chunks = []
        finishes = []
        req_ready = []
        sess.stream_chunk.connect(chunks.append)
        sess.turn_finished.connect(lambda *a: finishes.append(a))
        sess.requirements_ready.connect(req_ready.append)

        sess.start("draft")
        qtbot.waitUntil(started.is_set, timeout=3000)
        sess.stop()  # generation++ on the GUI thread, mid-turn
        release.set()
        qtbot.wait(300)  # let any queued (stale) signals be delivered + dropped

        assert chunks == []
        assert finishes == []
        assert req_ready == []

    def test_default_adapter_factory_builds_claude_adapter(self, tmp_path, qtbot):
        side_cfg = SideConfig(
            adapter="claude",
            command="my-claude",
            model="m1",
            debate_model="debate-m",
            default_model="dm",
        )
        sess = GrillSession(tmp_path, side_cfg, 42)
        try:
            adapter = sess._worker._adapter
            assert adapter.command == "my-claude"
            assert adapter.model == "debate-m"  # debate_model wins
            assert adapter.side_name == "grill"
            assert adapter.cwd == Path(tmp_path)
            assert adapter.events_dir == Path(tmp_path) / ".spar" / "transcript"
        finally:
            sess.stop()
            sess._thread.wait(3000)
