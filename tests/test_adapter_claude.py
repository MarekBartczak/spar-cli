"""Tests for spar.adapters.claude.ClaudeAdapter.

These tests never invoke the real ``claude`` CLI. Instead they point the
adapter's ``command`` at ``tests/fakes/fake_claude.py``, a small script
driven by environment variables (see that file's docstring).
"""

import json
from pathlib import Path

import pytest

from spar.adapters.base import AdapterError, SessionLost, TurnResult
from spar.adapters.claude import ClaudeAdapter

FAKE_CLAUDE = str(Path(__file__).parent / "fakes" / "fake_claude.py")


def make_adapter(tmp_path, model="", **kwargs):
    return ClaudeAdapter(
        command=FAKE_CLAUDE,
        model=model,
        events_dir=tmp_path / "events",
        **kwargs,
    )


def read_argv_lines(args_file: Path) -> list[list[str]]:
    return [json.loads(line) for line in args_file.read_text().splitlines()]


# --- argv contract -----------------------------------------------------


def test_new_session_argv_contract(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path)
    adapter.run_turn("hello there", session_id=None, timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    assert len(argv_list) == 1
    assert argv_list[0] == [
        FAKE_CLAUDE,
        "-p",
        "--output-format",
        "json",
        "--allowedTools",
        "Read,Edit,Write",
        "--permission-mode",
        "acceptEdits",
        "hello there",
    ]


def test_resume_argv_contract(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path)
    adapter.run_turn("continue please", session_id="sess-42", timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    assert argv_list[0] == [
        FAKE_CLAUDE,
        "-p",
        "--resume",
        "sess-42",
        "--output-format",
        "json",
        "--allowedTools",
        "Read,Edit,Write",
        "--permission-mode",
        "acceptEdits",
        "continue please",
    ]


def test_model_flag_included_when_set_new_session(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path, model="opus-9")
    adapter.run_turn("hi", session_id=None, timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    assert argv_list[0] == [
        FAKE_CLAUDE,
        "-p",
        "--output-format",
        "json",
        "--allowedTools",
        "Read,Edit,Write",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "opus-9",
        "hi",
    ]


def test_model_flag_included_when_set_resume(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path, model="opus-9")
    adapter.run_turn("hi again", session_id="sess-1", timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    assert argv_list[0] == [
        FAKE_CLAUDE,
        "-p",
        "--resume",
        "sess-1",
        "--output-format",
        "json",
        "--allowedTools",
        "Read,Edit,Write",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "opus-9",
        "hi again",
    ]


def test_model_flag_absent_when_not_set(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path, model="")
    adapter.run_turn("hi", session_id=None, timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    assert "--model" not in argv_list[0]


# --- happy path ----------------------------------------------------------


def test_happy_path_extracts_session_and_reply(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_STDOUT",
        json.dumps({"session_id": "abc-123", "result": "the reply text"}),
    )

    adapter = make_adapter(tmp_path)
    result = adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert isinstance(result, TurnResult)
    assert result.session_id == "abc-123"
    assert result.reply_text == "the reply text"
    assert result.exit_code == 0
    assert result.events_path.exists()
    assert json.loads(result.events_path.read_text()) == {
        "session_id": "abc-123",
        "result": "the reply text",
    }


def test_events_file_naming(tmp_path):
    adapter = make_adapter(tmp_path, side_name="claude-left")
    result = adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert result.events_path.parent == tmp_path / "events"
    assert result.events_path.name.startswith("claude-left-")
    assert result.events_path.name.endswith(".json")


# --- error handling --------------------------------------------------------


def test_resume_nonzero_exit_raises_session_lost(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "no session found")

    adapter = make_adapter(tmp_path)
    with pytest.raises(SessionLost):
        adapter.run_turn("continue", session_id="sess-999", timeout_sec=5)


def test_fresh_nonzero_exit_raises_adapter_error_with_stderr_excerpt(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "boom: something broke")

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError) as excinfo:
        adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert "boom: something broke" in str(excinfo.value)
    assert "1" in str(excinfo.value)


def test_malformed_json_stdout_raises_adapter_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", "not json at all {{{")

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError):
        adapter.run_turn("hello", session_id=None, timeout_sec=5)


def test_json_missing_result_raises_adapter_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", json.dumps({"session_id": "abc"}))

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError):
        adapter.run_turn("hello", session_id=None, timeout_sec=5)


def test_missing_session_id_yields_none(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_STDOUT", json.dumps({"result": "no session here"}))

    adapter = make_adapter(tmp_path)
    result = adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert result.session_id is None
    assert result.reply_text == "no session here"


def test_timeout_raises_adapter_error_and_writes_events_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "5")

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError) as excinfo:
        adapter.run_turn("hello", session_id=None, timeout_sec=1)

    assert "timeout" in str(excinfo.value).lower()

    # events file exists (possibly empty) even though the call raised
    events_files = list((tmp_path / "events").glob("*.json"))
    assert len(events_files) == 1


def test_readonly_adapter_drops_edit_tools(tmp_path, monkeypatch):
    # A reviewer-side adapter must not be able to write: only Read is allowed
    # and no permission mode that auto-approves edits is passed.
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path, readonly=True)
    adapter.run_turn("review this", session_id=None, timeout_sec=5)

    argv = read_argv_lines(args_file)[0]
    assert argv == [
        FAKE_CLAUDE,
        "-p",
        "--output-format",
        "json",
        "--allowedTools",
        "Read",
        "review this",
    ]
