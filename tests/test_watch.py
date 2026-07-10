"""Tests for spar.watch: follow() generator + colorize() + CLI routing."""

import threading
import time

import pytest

from spar.watch import colorize, follow


class TestFollow:
    def test_yields_appended_lines_only_by_default(self, tmp_path):
        path = tmp_path / "live.log"
        path.write_text("pre-existing\n", encoding="utf-8")

        state = {"stop": False}
        lines = []

        def writer():
            time.sleep(0.05)
            with path.open("a", encoding="utf-8") as f:
                f.write("new1\n")
                f.flush()
            time.sleep(0.05)
            with path.open("a", encoding="utf-8") as f:
                f.write("new2\n")
                f.flush()
            time.sleep(0.05)
            state["stop"] = True

        t = threading.Thread(target=writer)
        t.start()
        for line in follow(path, from_start=False, poll_sec=0.01, stop=lambda: state["stop"]):
            lines.append(line)
        t.join()

        assert lines == ["new1", "new2"]

    def test_from_start_yields_preexisting_content_first(self, tmp_path):
        path = tmp_path / "live.log"
        path.write_text("first\nsecond\n", encoding="utf-8")

        state = {"stop": False}
        lines = []

        def stopper():
            time.sleep(0.08)
            state["stop"] = True

        t = threading.Thread(target=stopper)
        t.start()
        for line in follow(path, from_start=True, poll_sec=0.01, stop=lambda: state["stop"]):
            lines.append(line)
        t.join()

        assert lines[:2] == ["first", "second"]

    def test_waits_for_missing_file_then_reads_it(self, tmp_path):
        path = tmp_path / "live.log"
        state = {"stop": False}
        lines = []

        def writer():
            time.sleep(0.05)
            path.write_text("hello\n", encoding="utf-8")
            time.sleep(0.08)
            state["stop"] = True

        t = threading.Thread(target=writer)
        t.start()
        for line in follow(path, from_start=True, poll_sec=0.01, stop=lambda: state["stop"]):
            lines.append(line)
        t.join()

        assert lines == ["hello"]

    def test_survives_truncation_by_reopening_from_zero(self, tmp_path):
        path = tmp_path / "live.log"
        path.write_text("aaaaaaaaaa\n", encoding="utf-8")

        state = {"stop": False}
        lines = []

        def writer():
            time.sleep(0.05)
            # truncate: a new run started, file is now shorter
            path.write_text("b\n", encoding="utf-8")
            time.sleep(0.08)
            state["stop"] = True

        t = threading.Thread(target=writer)
        t.start()
        for line in follow(path, from_start=False, poll_sec=0.01, stop=lambda: state["stop"]):
            lines.append(line)
        t.join()

        assert lines == ["b"]

    def test_stop_ends_iteration(self, tmp_path):
        path = tmp_path / "live.log"
        path.write_text("x\n", encoding="utf-8")
        collected = []
        for line in follow(path, from_start=True, poll_sec=0.01, stop=lambda: len(collected) >= 1):
            collected.append(line)
        assert collected == ["x"]


class TestColorize:
    def test_prefixed_line_gets_ansi_wrapped_prefix(self):
        out = colorize("[claude r0] hello there")
        assert "\x1b[" in out
        assert "[claude r0]" in out
        assert "hello there" in out

    def test_same_prefix_is_stable_color(self):
        a = colorize("[claude r0] one")
        b = colorize("[claude r0] two")
        # extract the color escape prefix before the bracket
        a_color = a.split("[claude r0]")[0]
        b_color = b.split("[claude r0]")[0]
        assert a_color == b_color

    def test_different_prefixes_can_differ(self):
        a = colorize("[claude r0] one")
        b = colorize("[codex r0] one")
        assert a.split("]")[0] != b.split("]")[0]

    def test_gate_pending_line_gets_bright_banner(self):
        line = "spar: gate 'consensus' pending (options: accept, remarks, abort)"
        out = colorize(line)
        assert "\x1b[" in out
        assert "gate 'consensus' pending" in out

    def test_spar_log_line_gets_highlighted(self):
        out = colorize("spar exec: [t1] merged into integration.")
        assert "\x1b[" in out
        assert "spar exec: [t1] merged into integration." in out

    def test_plain_line_passthrough(self):
        assert colorize("just some text") == "just some text"


class TestWatchCliRouting:
    def test_watch_help_exits_zero(self, capsys):
        from spar.cli import main

        with pytest.raises(SystemExit) as exc_info:
            main(["watch", "--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "usage" in captured.out
