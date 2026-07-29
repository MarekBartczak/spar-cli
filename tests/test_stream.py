"""Tests for spar/stream.py: StreamSink fan-out (stdout + always-on live.log)."""

import io
from pathlib import Path

from spar.stream import StreamSink


def test_event_writes_prefixed_line_to_stdout_and_live_log(tmp_path):
    out = io.StringIO()
    sink = StreamSink(tmp_path, quiet=False, stdout=out)
    sink.event("A r0", "hello there")
    sink.close()

    assert out.getvalue() == "[A r0] hello there\n"
    assert (tmp_path / "live.log").read_text(encoding="utf-8") == "[A r0] hello there\n"


def test_quiet_suppresses_stdout_for_event_but_not_log(tmp_path):
    out = io.StringIO()
    sink = StreamSink(tmp_path, quiet=True, stdout=out)
    sink.event("A r0", "hello there")
    sink.log("spar: turn complete")
    sink.close()

    # event() is suppressed on stdout when quiet...
    assert "hello there" not in out.getvalue()
    # ...but log() always reaches stdout, even quiet.
    assert "spar: turn complete" in out.getvalue()

    # Both always land in live.log regardless of quiet.
    log_text = (tmp_path / "live.log").read_text(encoding="utf-8")
    assert "[A r0] hello there" in log_text
    assert "spar: turn complete" in log_text


def test_log_always_reaches_stdout_when_not_quiet(tmp_path):
    out = io.StringIO()
    sink = StreamSink(tmp_path, quiet=False, stdout=out)
    sink.log("spar: hello")
    sink.close()
    assert "spar: hello" in out.getvalue()


def test_live_log_truncated_on_construction(tmp_path):
    spar_dir = tmp_path
    spar_dir.mkdir(parents=True, exist_ok=True)
    (spar_dir / "live.log").write_text("stale content from a prior run\n", encoding="utf-8")

    out = io.StringIO()
    sink = StreamSink(spar_dir, quiet=False, stdout=out)
    sink.close()

    assert (spar_dir / "live.log").read_text(encoding="utf-8") == ""


def test_close_flushes(tmp_path):
    out = io.StringIO()
    sink = StreamSink(tmp_path, quiet=False, stdout=out)
    sink.log("before close")
    sink.close()
    # File must be readable (and fully written) immediately after close().
    assert "before close" in (tmp_path / "live.log").read_text(encoding="utf-8")


def test_spar_dir_created_if_missing(tmp_path):
    spar_dir = tmp_path / "nested" / ".spar"
    out = io.StringIO()
    sink = StreamSink(spar_dir, quiet=False, stdout=out)
    sink.close()
    assert (spar_dir / "live.log").exists()


def test_event_prefixes_every_physical_line_of_a_multiline_payload(tmp_path):
    # An embedded newline used to emit prefix-less physical lines, which
    # readers attribute to the wrong source: the GUI pane classifies a
    # prefix-less line as spar's OWN log (stream.py's _FILTER_SPAR), so a
    # side's own output vanished when filtering by that side.
    out = io.StringIO()
    sink = StreamSink(tmp_path, quiet=False, stdout=out)
    sink.event("A r0", "exec: run this\nand that")
    sink.close()

    expected = "[A r0] exec: run this\n[A r0] and that\n"
    assert out.getvalue() == expected
    assert (tmp_path / "live.log").read_text(encoding="utf-8") == expected


def test_event_keeps_blank_lines_prefixed(tmp_path):
    out = io.StringIO()
    sink = StreamSink(tmp_path, quiet=False, stdout=out)
    sink.event("A r0", "para one\n\npara two")
    sink.close()

    assert out.getvalue() == "[A r0] para one\n[A r0] \n[A r0] para two\n"


def test_log_lines_stay_unprefixed(tmp_path):
    # spar's own log lines are the ONLY prefix-less lines in live.log -- that
    # is what the GUI's "spar" filter chip keys off.
    out = io.StringIO()
    sink = StreamSink(tmp_path, quiet=False, stdout=out)
    sink.log("spar: turn complete")
    sink.close()

    assert (tmp_path / "live.log").read_text(encoding="utf-8") == "spar: turn complete\n"
