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
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools",
        "Read,Edit,Write,Bash,Grep,Glob",
        "--permission-mode",
        "acceptEdits",
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
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools",
        "Read,Edit,Write,Bash,Grep,Glob",
        "--permission-mode",
        "acceptEdits",
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
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools",
        "Read,Edit,Write,Bash,Grep,Glob",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "opus-9",
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
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools",
        "Read,Edit,Write,Bash,Grep,Glob",
        "--permission-mode",
        "acceptEdits",
        "--model",
        "opus-9",
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
    # Transcript is now the stream-json JSONL, not a single JSON document.
    # session_id and reply come from the terminal ``result`` event.
    lines = [ln for ln in result.events_path.read_text().splitlines() if ln.strip()]
    terminal = json.loads(lines[-1])
    assert terminal["type"] == "result"
    assert terminal["result"] == "the reply text"
    assert terminal["session_id"] == "abc-123"


def test_events_file_naming(tmp_path):
    adapter = make_adapter(tmp_path, side_name="claude-left")
    result = adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert result.events_path.parent == tmp_path / "events"
    assert result.events_path.name.startswith("claude-left-")
    assert result.events_path.name.endswith(".json")


# --- live streaming (on_event) -------------------------------------------


def test_on_event_streams_display_lines_and_extracts_result(tmp_path, monkeypatch):
    # Drive the fake into stream-json mode: a tool_use content_block_start,
    # buffered input_json_delta fragments, a content_block_stop, a text_delta
    # chunk, and a terminal result event with duration_ms.
    monkeypatch.setenv(
        "FAKE_CLAUDE_STDOUT",
        json.dumps({"session_id": "sid-9", "result": "hello world"}),
    )
    monkeypatch.setenv("FAKE_CLAUDE_STREAM_TOOL", "Edit")
    monkeypatch.setenv(
        "FAKE_CLAUDE_STREAM_TOOL_INPUT", json.dumps({"file_path": "src/main.cpp"})
    )
    monkeypatch.setenv("FAKE_CLAUDE_DURATION_MS", "1234")

    events: list[str] = []
    adapter = make_adapter(tmp_path)
    result = adapter.run_turn(
        "hello", session_id=None, timeout_sec=5, on_event=events.append
    )

    assert events == ["tool: Edit src/main.cpp", "hello world", "done (1.2s)"]
    # No bare "tool: Edit" duplicate line.
    assert "tool: Edit" not in events
    assert result.session_id == "sid-9"
    assert result.reply_text == "hello world"


def test_on_event_tool_with_unparseable_input_falls_back_to_bare_name(
    tmp_path, monkeypatch
):
    # No FAKE_CLAUDE_STREAM_TOOL_INPUT set: the tool block opens and closes
    # with an empty input buffer, which fails to parse as JSON.
    monkeypatch.setenv(
        "FAKE_CLAUDE_STDOUT",
        json.dumps({"session_id": "sid-10", "result": "no input reply"}),
    )
    monkeypatch.setenv("FAKE_CLAUDE_STREAM_TOOL", "Bash")

    events: list[str] = []
    adapter = make_adapter(tmp_path)
    adapter.run_turn("hello", session_id=None, timeout_sec=5, on_event=events.append)

    assert events[0] == "tool: Bash"


def test_on_event_missing_stop_is_flushed_by_terminal_result(tmp_path, monkeypatch):
    # The tool's content_block_stop never arrives; the terminal result event
    # must still flush the buffered tool line rather than swallowing it.
    monkeypatch.setenv(
        "FAKE_CLAUDE_STDOUT",
        json.dumps({"session_id": "sid-11", "result": "flushed by result"}),
    )
    monkeypatch.setenv("FAKE_CLAUDE_STREAM_TOOL", "Read")
    monkeypatch.setenv(
        "FAKE_CLAUDE_STREAM_TOOL_INPUT", json.dumps({"file_path": "src/lib.cpp"})
    )
    monkeypatch.setenv("FAKE_CLAUDE_STREAM_TOOL_NO_STOP", "1")

    events: list[str] = []
    adapter = make_adapter(tmp_path)
    result = adapter.run_turn(
        "hello", session_id=None, timeout_sec=5, on_event=events.append
    )

    assert "tool: Read src/lib.cpp" in events
    assert result.reply_text == "flushed by result"


def test_on_event_none_is_behaviorally_identical(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_STDOUT",
        json.dumps({"session_id": "sid-0", "result": "no callback"}),
    )
    adapter = make_adapter(tmp_path)
    result = adapter.run_turn("hi", session_id=None, timeout_sec=5)
    assert result.session_id == "sid-0"
    assert result.reply_text == "no callback"


def test_callback_exception_does_not_kill_turn(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_STDOUT",
        json.dumps({"session_id": "sid-x", "result": "still works"}),
    )

    def boom(line: str) -> None:
        raise RuntimeError("nope")

    adapter = make_adapter(tmp_path)
    result = adapter.run_turn("hi", session_id=None, timeout_sec=5, on_event=boom)
    assert result.reply_text == "still works"


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
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--allowedTools",
        "Read",
    ]


# --- _DisplayMapper: text deltas buffered to whole lines -------------------
#
# Regression: every ``text_delta`` used to be emitted as its own display line.
# A delta is a network-sized chunk (observed on a real turn: 1..141 chars, of
# 35 deltas 9 carried embedded newlines), so the live pane showed words cut in
# half with the prefix repeated mid-word -- "[claude r1] Te" followed by
# "[claude r1] raz naprawa ..." -- and delta-internal newlines produced
# physical lines with no prefix at all (44 of 131 lines in one real log).


def _text_delta(text, index=0):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _block_stop(index=0):
    return {"type": "stream_event", "event": {"type": "content_block_stop", "index": index}}


def _tool_start(name, index=0):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "tool_use", "name": name},
        },
    }


def _tool_input_delta(partial, index=0):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial},
        },
    }


def _drive(events):
    from spar.adapters.claude import _DisplayMapper

    mapper = _DisplayMapper()
    out = []
    for obj in events:
        out.extend(mapper.map(obj))
    return out


def test_text_deltas_are_joined_into_whole_lines():
    # the exact chunking seen on a real turn
    out = _drive([
        _text_delta("Te"),
        _text_delta("raz naprawa niedozwolonych `model=haiku` i kom"),
        _text_delta("end `yarn` w `test=`:\n"),
    ])
    assert out == ["Teraz naprawa niedozwolonych `model=haiku` i komend `yarn` w `test=`:"]


def test_partial_text_line_is_not_emitted_until_its_newline():
    out = _drive([_text_delta("half a sen")])
    assert out == []


def test_text_tail_is_flushed_on_content_block_stop():
    out = _drive([_text_delta("no trailing newline"), _block_stop()])
    assert out == ["no trailing newline"]


def test_text_tail_is_flushed_by_terminal_result():
    out = _drive([
        _text_delta("tail without stop"),
        {"type": "result", "duration_ms": 2000},
    ])
    assert out == ["tail without stop", "done (2.0s)"]


def test_no_display_line_ever_contains_a_newline():
    out = _drive([
        _text_delta("one\ntwo\nthree"),
        _block_stop(),
    ])
    assert out == ["one", "two", "three"]
    assert all("\n" not in line for line in out)


def test_blank_lines_are_preserved_as_paragraph_structure():
    out = _drive([_text_delta("para one\n\npara two\n")])
    assert out == ["para one", "", "para two"]


def test_text_tail_is_flushed_when_a_tool_block_reuses_the_index():
    out = _drive([
        _text_delta("thinking out loud"),
        _tool_start("Edit"),
        _tool_input_delta(json.dumps({"file_path": "a.py"})),
        _block_stop(),
    ])
    assert out == ["thinking out loud", "tool: Edit a.py"]


def test_two_text_blocks_buffer_independently():
    out = _drive([
        _text_delta("first ", index=0),
        _text_delta("second ", index=1),
        _text_delta("block\n", index=1),
        _text_delta("block\n", index=0),
    ])
    assert out == ["second block", "first block"]


# --- one tool call == one display line -------------------------------------


def test_multiline_tool_argument_is_collapsed_to_one_line():
    # A heredoc script emitted verbatim left its body as prefix-less lines.
    command = "cd /repo; python3 - <<'PY'\nimport pathlib\np = pathlib.Path('x')\nPY"
    out = _drive([
        _tool_start("Bash"),
        _tool_input_delta(json.dumps({"command": command})),
        _block_stop(),
    ])
    assert out == ["tool: Bash cd /repo; python3 - <<'PY' import pathlib p = pathlib.Path('x') PY"]
    assert "\n" not in out[0]


# --- prompt travels on stdin, never in argv ----------------------------
# Live failure: a reviewer prompt (plan + diff) exceeded Linux's 128 KiB
# per-argument limit and the spawn died with
# "[Errno 7] Argument list too long" mid-execution.


def test_prompt_is_fed_on_stdin_not_argv(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    stdin_file = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_FILE", str(stdin_file))

    adapter = make_adapter(tmp_path)
    adapter.run_turn("prompt body", session_id=None, timeout_sec=5)

    assert "prompt body" not in read_argv_lines(args_file)[0]
    assert stdin_file.read_text() == "prompt body"


def test_resume_prompt_is_fed_on_stdin_not_argv(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    stdin_file = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_CLAUDE_ARGS_FILE", str(args_file))
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_FILE", str(stdin_file))

    adapter = make_adapter(tmp_path)
    adapter.run_turn("resumed body", session_id="sess-7", timeout_sec=5)

    assert "resumed body" not in read_argv_lines(args_file)[0]
    assert stdin_file.read_text() == "resumed body"


def test_prompt_far_over_the_per_argument_limit_still_runs(tmp_path, monkeypatch):
    # 300 KiB: well past MAX_ARG_STRLEN (128 KiB), the limit that killed a
    # live exec run. On stdin there is no such ceiling.
    big = "x" * (300 * 1024)
    stdin_file = tmp_path / "stdin.txt"
    monkeypatch.setenv("FAKE_CLAUDE_STDIN_FILE", str(stdin_file))

    adapter = make_adapter(tmp_path)
    result = adapter.run_turn(big, session_id=None, timeout_sec=30)

    assert result.exit_code == 0
    assert stdin_file.read_text() == big


# --- a finished turn survives the wall clock ---------------------------
# Live failure: codex emitted its terminal event and wrote its final message,
# then the process lingered past turn_timeout_sec — spar killed it, called the
# whole turn a timeout and threw ~17 minutes of completed work away.


def test_turn_that_already_emitted_its_result_survives_a_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_STDOUT",
        json.dumps({"session_id": "sess-late", "result": "finished before the clock"}),
    )
    monkeypatch.setenv("FAKE_CLAUDE_HANG_AFTER", "30")

    adapter = make_adapter(tmp_path)
    result = adapter.run_turn("go", session_id=None, timeout_sec=1)

    assert result.reply_text == "finished before the clock"
    assert result.session_id == "sess-late"


def test_recovered_timeout_is_announced_on_the_event_stream(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CLAUDE_STDOUT",
        json.dumps({"session_id": "sess-late", "result": "done anyway"}),
    )
    monkeypatch.setenv("FAKE_CLAUDE_HANG_AFTER", "30")
    lines: list[str] = []

    adapter = make_adapter(tmp_path)
    adapter.run_turn("go", session_id=None, timeout_sec=1, on_event=lines.append)

    assert any("timeout" in line for line in lines)


def test_timeout_without_a_result_event_still_fails(tmp_path, monkeypatch):
    # No terminal result: nothing was finished, so the turn MUST still fail —
    # the recovery must never invent a reply.
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError):
        adapter.run_turn("go", session_id=None, timeout_sec=1)
