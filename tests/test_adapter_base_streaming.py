"""Tests for the streaming ``run_cli`` (Popen + reader threads + on_line).

These tests drive real ``python3 -c`` subprocesses (no fakes needed) to
exercise the live-streaming contract: lines arrive incrementally via the
``on_line`` callback, the events file is written incrementally, stdout is
still returned in full, stderr is drained on its own thread (an undrained
stderr pipe would fill and block the child → false timeout), ``stdin_text``
reaches the child, timeouts raise ``AdapterError`` with the partial stream
already on disk, and a raising callback never kills the turn.
"""

import subprocess
import sys
import time

import pytest

from spar.adapters.base import AdapterError, run_cli


def test_lines_stream_live_and_stdout_is_complete(tmp_path):
    events = tmp_path / "events.txt"
    producer = (
        "import sys, time\n"
        "for i in range(3):\n"
        "    print(f'line {i}', flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    collected: list[tuple[float, str]] = []

    def on_line(line: str) -> None:
        collected.append((time.monotonic(), line))

    result = run_cli(
        [sys.executable, "-c", producer],
        timeout_sec=10,
        events_path=events,
        on_line=on_line,
    )

    lines = [line for _, line in collected]
    assert lines == ["line 0", "line 1", "line 2"]
    # arrived live, not all at once at the end
    assert collected[-1][0] - collected[0][0] >= 0.15

    assert events.read_text() == "line 0\nline 1\nline 2\n"
    assert result.stdout == "line 0\nline 1\nline 2\n"
    assert result.returncode == 0


def test_on_line_none_still_writes_events_and_stdout(tmp_path):
    events = tmp_path / "events.txt"
    result = run_cli(
        [sys.executable, "-c", "print('hello')"],
        timeout_sec=10,
        events_path=events,
        on_line=None,
    )
    assert result.stdout == "hello\n"
    assert events.read_text() == "hello\n"


def test_timeout_raises_and_partial_stream_on_disk(tmp_path):
    events = tmp_path / "events.txt"
    producer = (
        "import time\n"
        "print('partial', flush=True)\n"
        "time.sleep(10)\n"
    )
    start = time.monotonic()
    with pytest.raises(AdapterError) as excinfo:
        run_cli(
            [sys.executable, "-c", producer],
            timeout_sec=1,
            events_path=events,
        )
    elapsed = time.monotonic() - start
    assert "timeout" in str(excinfo.value).lower()
    assert elapsed < 5  # killed promptly, not after the full 10s sleep
    # partial line already flushed to disk before the kill
    assert "partial" in events.read_text()


def test_callback_exception_never_kills_the_turn(tmp_path):
    events = tmp_path / "events.txt"

    def boom(line: str) -> None:
        raise RuntimeError("callback blew up")

    result = run_cli(
        [sys.executable, "-c", "print('a'); print('b')"],
        timeout_sec=10,
        events_path=events,
        on_line=boom,
    )
    assert result.returncode == 0
    assert result.stdout == "a\nb\n"
    assert events.read_text() == "a\nb\n"


def test_stderr_flood_does_not_cause_false_timeout(tmp_path):
    events = tmp_path / "events.txt"
    # Write well over 1 MB to stderr, then exit 0 within the timeout. If the
    # stderr pipe is not drained concurrently it fills (~64 KB) and blocks the
    # child forever → false timeout. This pins the dedicated stderr reader.
    producer = (
        "import sys\n"
        "chunk = 'x' * 1024\n"
        "for _ in range(1500):\n"  # ~1.5 MB
        "    sys.stderr.write(chunk)\n"
        "sys.stderr.flush()\n"
        "print('done-stdout', flush=True)\n"
    )
    result = run_cli(
        [sys.executable, "-c", producer],
        timeout_sec=15,
        events_path=events,
    )
    assert result.returncode == 0
    assert result.stdout == "done-stdout\n"
    assert len(result.stderr) >= 1024 * 1024


def test_stdin_text_reaches_the_child(tmp_path):
    events = tmp_path / "events.txt"
    result = run_cli(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        timeout_sec=10,
        events_path=events,
        stdin_text="fed via stdin\n",
    )
    assert result.stdout == "fed via stdin\n"


def test_returns_completed_process_with_stderr(tmp_path):
    events = tmp_path / "events.txt"
    result = run_cli(
        [sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err text')"],
        timeout_sec=10,
        events_path=events,
    )
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.stdout == "out\n"
    assert result.stderr == "err text"
