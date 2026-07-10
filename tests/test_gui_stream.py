"""Tests for ``spar.gui.stream`` (LiveLogTailer + StreamPane).

Skipped entirely on interpreters without the optional ``gui`` extra.

Two layers:

* ``LiveLogTailer`` — a real ``QTimer``-driven incremental reader, exercised
  against a tmp ``live.log`` file (append / truncate / cwd-independence);
  ``poll()`` is called directly (rather than waiting on the real 250ms timer)
  so the tests stay fast and deterministic.
* ``StreamPane`` — fed raw lines directly (bypassing the tailer) and checked
  for filter/coloring/ring-buffer behavior via the underlying document, never
  pixels.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QFont

from spar.gui.stream import LiveLogTailer, StreamPane, humanize_prefix


# ----------------------------------------------------------------------
# LiveLogTailer
# ----------------------------------------------------------------------
class TestLiveLogTailer:
    def test_missing_file_waits_without_error(self, tmp_path):
        tailer = LiveLogTailer(tmp_path / "live.log")
        tailer.poll()  # must not raise

    def test_emits_appended_lines(self, tmp_path, qtbot):
        log_path = tmp_path / "live.log"
        log_path.write_text("", encoding="utf-8")
        tailer = LiveLogTailer(log_path)

        received: list[list[str]] = []
        tailer.lines.connect(received.append)

        with log_path.open("a", encoding="utf-8") as fh:
            fh.write("[claude r0] hello\n")
            fh.write("[codex r0] world\n")
        tailer.poll()

        assert received == [["[claude r0] hello", "[codex r0] world"]]

    def test_truncation_reopens_from_zero(self, tmp_path):
        log_path = tmp_path / "live.log"
        log_path.write_text(
            "[claude r0] a much longer first line so the file shrinks\n",
            encoding="utf-8",
        )
        tailer = LiveLogTailer(log_path)

        received: list[list[str]] = []
        tailer.lines.connect(received.append)

        tailer.poll()
        assert received == [
            ["[claude r0] a much longer first line so the file shrinks"]
        ]

        # New run started: live.log recreated, shorter.
        log_path.write_text("[claude r0] second\n", encoding="utf-8")
        # First poll notices the truncation (size < pos) and reopens; the
        # actual re-read happens on the following poll.
        tailer.poll()
        tailer.poll()

        assert received[-1] == ["[claude r0] second"]

    def test_reads_project_dir_log_when_cwd_differs(self, tmp_path, monkeypatch):
        project_dir = tmp_path / "project"
        other_cwd = tmp_path / "elsewhere"
        project_dir.mkdir()
        other_cwd.mkdir()
        monkeypatch.chdir(other_cwd)

        log_path = project_dir / ".spar" / "live.log"
        log_path.parent.mkdir(parents=True)
        log_path.write_text("[claude r0] from project dir\n", encoding="utf-8")

        # A cwd-relative path would resolve to other_cwd/.spar/live.log and
        # find nothing; the tailer must have been given the absolute path.
        assert os.getcwd() == str(other_cwd)
        tailer = LiveLogTailer(project_dir / ".spar" / "live.log")

        received: list[list[str]] = []
        tailer.lines.connect(received.append)
        tailer.poll()

        assert received == [["[claude r0] from project dir"]]


# ----------------------------------------------------------------------
# StreamPane
# ----------------------------------------------------------------------
class TestStreamPane:
    def test_feed_lines_appends_to_document(self, qtbot):
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(["[claude r0] hello", "[codex r0] world"])

        # Rendered text shows the humanized prefix (fix 4); the raw prefix
        # lives on in the ring buffer / filter chips, checked elsewhere.
        text = pane.text.toPlainText()
        assert "[claude · runda 1] hello" in text
        assert "[codex · runda 1] world" in text

    def test_filter_by_side_leaves_only_that_sides_lines(self, qtbot):
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(
            [
                "[claude r0] alpha",
                "[codex r0] beta",
                "[claude r1] gamma",
                "spar: starting round 2",
            ]
        )
        pane.set_filter("side", "claude")

        text = pane.text.toPlainText()
        assert "alpha" in text
        assert "gamma" in text
        assert "beta" not in text
        assert "starting round 2" not in text

    def test_filter_spar_shows_only_spar_log_lines(self, qtbot):
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(
            [
                "[claude r0] alpha",
                "spar: starting round 2",
                "spar exec: task t1 complete",
            ]
        )
        pane.set_filter("spar")

        text = pane.text.toPlainText()
        assert "alpha" not in text
        assert "starting round 2" in text
        assert "task t1 complete" in text

    def test_filter_wszystko_shows_everything_again(self, qtbot):
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(["[claude r0] alpha", "[codex r0] beta"])
        pane.set_filter("side", "claude")
        pane.set_filter("all")

        text = pane.text.toPlainText()
        assert "alpha" in text
        assert "beta" in text

    def test_filter_by_task_leaves_only_that_tasks_lines(self, qtbot):
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(
            [
                "[A t1 impl] doing impl work",
                "[B t2 review] reviewing",
                "[A t1 review] reviewing t1",
            ]
        )
        pane.set_filter("task", "t1")

        text = pane.text.toPlainText()
        assert "doing impl work" in text
        assert "reviewing t1" in text
        assert "reviewing" in text  # substring of both -- checked precisely below
        assert "t2" not in [
            line for line in text.splitlines() if "reviewing" in line
        ][0]

    def test_gate_pending_line_gets_bold_gate_char_format(self, qtbot):
        from spar.gui.theme import TOKENS

        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(
            ["[claude r0] normal line", "spar: gate 'consensus' pending (options: ...)"]
        )

        doc = pane.text.document()
        found = False
        block = doc.begin()
        while block.isValid():
            if "gate 'consensus' pending" in block.text():
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid() and "gate 'consensus' pending" in frag.text():
                        fmt = frag.charFormat()
                        assert fmt.fontWeight() > QFont.Weight.Normal
                        assert fmt.foreground().color().name() == TOKENS["gate"]
                        found = True
                    it += 1
            block = block.next()
        assert found

    def test_ring_buffer_caps_at_20000(self, qtbot):
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines([f"[claude r0] line-{i}" for i in range(20500)])

        assert len(pane._ring) == 20000
        assert pane._ring[0] == "[claude r0] line-500"
        assert pane._ring[-1] == "[claude r0] line-20499"

    def test_following_survives_20k_cap_trim(self, qtbot):
        # Final review minor #3: appending past the 20k-line
        # ``setMaximumBlockCount`` cap makes Qt trim blocks off the top,
        # which can fire transient ``valueChanged`` signals on the vertical
        # scrollbar before the final scroll-to-bottom lands. Those must be
        # ignored (``_programmatic_scroll`` guard) rather than misread as
        # the user manually scrolling away.
        pane = StreamPane()
        qtbot.addWidget(pane)
        pane.show()

        assert pane._following is True
        pane.feed_lines([f"[claude r0] line-{i}" for i in range(20500)])

        assert pane._following is True
        assert pane.follow_button.isChecked() is True
        assert pane.jump_button.isVisible() is False

    def test_filter_chips_auto_populated_from_seen_prefixes(self, qtbot):
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(
            [
                "[claude r0] alpha",
                "[codex r0] beta",
                "[A t1 impl] gamma",
            ]
        )

        assert "claude" in pane._known_sides
        assert "codex" in pane._known_sides
        assert "A" in pane._known_sides
        assert "t1" in pane._known_tasks

    def test_set_models_rerenders_with_translated_prefixes(self, qtbot):
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(["[claude r0] hello", "[codex t1 impl] working"])
        pane.set_models(
            {
                "sides": {"claude": "sonnet"},
                "tasks": {"t1": {"model": "gpt-5.5", "review_model": "sonnet"}},
            }
        )

        text = pane.text.toPlainText()
        assert "[claude · sonnet · runda 1] hello" in text
        assert "[codex · gpt-5.5 · t1 · implementacja] working" in text

    def test_filter_still_matches_raw_side_after_translation(self, qtbot):
        # Filter chips must key on the RAW side/task, not the translated
        # label (fix 4) -- the chip for "claude" must still isolate its
        # lines even once they render with a model segment.
        pane = StreamPane()
        qtbot.addWidget(pane)

        pane.feed_lines(["[claude r0] alpha", "[codex r0] beta"])
        pane.set_models({"sides": {"claude": "sonnet", "codex": "gpt-5.5"}, "tasks": {}})
        pane.set_filter("side", "claude")

        text = pane.text.toPlainText()
        assert "alpha" in text
        assert "beta" not in text


# ----------------------------------------------------------------------
# humanize_prefix -- pure
# ----------------------------------------------------------------------
class TestHumanizePrefix:
    def test_debate_round_without_model(self):
        assert humanize_prefix("claude r0") == "claude · runda 1"

    def test_debate_round_with_model_and_round_offset(self):
        models = {"sides": {"claude": "sonnet"}}
        assert humanize_prefix("claude r0", models) == "claude · sonnet · runda 1"
        assert humanize_prefix("claude r2", models) == "claude · sonnet · runda 3"

    def test_exec_impl_with_model(self):
        models = {"tasks": {"t1": {"model": "gpt-5.5", "review_model": "sonnet"}}}
        assert humanize_prefix("codex t1 impl", models) == "codex · gpt-5.5 · t1 · implementacja"

    def test_exec_review_uses_review_model_not_model(self):
        models = {"tasks": {"t1": {"model": "gpt-5.5", "review_model": "sonnet"}}}
        assert humanize_prefix("claude t1 review", models) == "claude · sonnet · t1 · recenzja"

    def test_missing_model_omits_segment(self):
        assert humanize_prefix("codex t1 impl", {}) == "codex · t1 · implementacja"
        assert humanize_prefix("claude t1 review", {}) == "claude · t1 · recenzja"

    def test_unrecognized_shape_returned_unchanged(self):
        assert humanize_prefix("A", {}) == "A"
        assert humanize_prefix("spar-log", {}) == "spar-log"
        assert humanize_prefix("claude weirdtoken", {}) == "claude weirdtoken"

    def test_none_models_defaults_to_empty(self):
        assert humanize_prefix("claude r0", None) == "claude · runda 1"
