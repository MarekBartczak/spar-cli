"""Tests for spar.adapters.codex.CodexAdapter.

These tests never invoke the real ``codex`` CLI. Instead they point the
adapter's ``command`` at ``tests/fakes/fake_codex.py``, a small script
driven by environment variables (see that file's docstring).
"""

import json
from pathlib import Path

import pytest

from spar.adapters.base import AdapterError, SessionLost, TurnResult
from spar.adapters.codex import CodexAdapter

FAKE_CODEX = str(Path(__file__).parent / "fakes" / "fake_codex.py")


def make_adapter(tmp_path, model="", **kwargs):
    return CodexAdapter(
        command=FAKE_CODEX,
        model=model,
        events_dir=tmp_path / "events",
        **kwargs,
    )


def read_argv_lines(args_file: Path) -> list[list[str]]:
    return [json.loads(line) for line in args_file.read_text().splitlines()]


def extract_last_msg_path(argv: list[str]) -> str:
    idx = argv.index("--output-last-message")
    return argv[idx + 1]


# --- argv contract -----------------------------------------------------


def test_new_session_argv_contract(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path)
    adapter.run_turn("hello there", session_id=None, timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    assert len(argv_list) == 1
    argv = argv_list[0]
    last_msg_path = extract_last_msg_path(argv)

    assert argv == [
        FAKE_CODEX,
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--output-last-message",
        last_msg_path,
        "hello there",
    ]
    assert Path(last_msg_path).is_absolute()
    assert Path(last_msg_path).name.startswith("codex-last-")
    assert last_msg_path.endswith(".md")


def test_resume_argv_contract(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path)
    adapter.run_turn("continue please", session_id="sess-42", timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    argv = argv_list[0]
    last_msg_path = extract_last_msg_path(argv)

    assert argv == [
        FAKE_CODEX,
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--output-last-message",
        last_msg_path,
        "resume",
        "sess-42",
        "continue please",
    ]


def test_cd_flag_included_when_cwd_set(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))
    work_dir = tmp_path / "workdir"
    work_dir.mkdir()

    adapter = make_adapter(tmp_path, cwd=work_dir)
    adapter.run_turn("hi", session_id=None, timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    argv = argv_list[0]
    last_msg_path = extract_last_msg_path(argv)

    assert argv == [
        FAKE_CODEX,
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(work_dir),
        "--output-last-message",
        last_msg_path,
        "hi",
    ]


def test_cd_flag_absent_when_cwd_not_set(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path)
    adapter.run_turn("hi", session_id=None, timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    assert "--cd" not in argv_list[0]


def test_model_flag_included_when_set(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path, model="o9-codex")
    adapter.run_turn("hi", session_id=None, timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    argv = argv_list[0]
    last_msg_path = extract_last_msg_path(argv)

    assert argv == [
        FAKE_CODEX,
        "exec",
        "--json",
        "--sandbox",
        "workspace-write",
        "-m",
        "o9-codex",
        "--output-last-message",
        last_msg_path,
        "hi",
    ]


def test_model_flag_absent_when_not_set(tmp_path, monkeypatch):
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path, model="")
    adapter.run_turn("hi", session_id=None, timeout_sec=5)

    argv_list = read_argv_lines(args_file)
    assert "-m" not in argv_list[0]


# --- happy path ----------------------------------------------------------


def test_happy_path_extracts_session_and_reply_from_last_msg_file(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CODEX_STDOUT",
        "\n".join(
            [
                json.dumps({"type": "thread.started", "thread_id": "abc-123"}),
                "not json at all",
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "item_0", "type": "agent_message", "text": "ignored, not the reply"},
                    }
                ),
            ]
        )
        + "\n",
    )
    monkeypatch.setenv("FAKE_CODEX_LAST_MSG", "the real final reply")

    adapter = make_adapter(tmp_path)
    result = adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert isinstance(result, TurnResult)
    assert result.session_id == "abc-123"
    assert result.reply_text == "the real final reply"
    assert result.exit_code == 0
    assert result.events_path.exists()

    # raw JSONL stream is preserved verbatim in the events file
    raw = result.events_path.read_text()
    lines = raw.splitlines()
    assert json.loads(lines[0]) == {"type": "thread.started", "thread_id": "abc-123"}
    assert lines[1] == "not json at all"
    assert json.loads(lines[2]) == {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": "ignored, not the reply"},
    }


def test_events_file_naming(tmp_path):
    adapter = make_adapter(tmp_path, side_name="codex-right")
    result = adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert result.events_path.parent == tmp_path / "events"
    assert result.events_path.name.startswith("codex-right-")
    assert result.events_path.name.endswith(".jsonl")


def test_session_id_extracted_from_nested_msg_shape(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CODEX_STDOUT",
        json.dumps({"msg": {"session_id": "nested-sess-7"}}) + "\n",
    )

    adapter = make_adapter(tmp_path)
    result = adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert result.session_id == "nested-sess-7"


def test_session_id_absent_yields_none(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "FAKE_CODEX_STDOUT",
        json.dumps({"type": "agent_message", "message": "hi"}) + "\n",
    )

    adapter = make_adapter(tmp_path)
    result = adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert result.session_id is None


# --- error handling --------------------------------------------------------


def test_missing_last_msg_file_on_zero_exit_raises_adapter_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_NO_LAST_MSG", "1")

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError) as excinfo:
        adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert "no final message" in str(excinfo.value)


def test_empty_last_msg_file_on_zero_exit_raises_adapter_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_LAST_MSG", "")

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError) as excinfo:
        adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert "no final message" in str(excinfo.value)


def test_resume_nonzero_exit_raises_session_lost(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "no session found")

    adapter = make_adapter(tmp_path)
    with pytest.raises(SessionLost):
        adapter.run_turn("continue", session_id="sess-999", timeout_sec=5)


def test_fresh_nonzero_exit_raises_adapter_error_with_stderr_excerpt(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_EXIT", "1")
    monkeypatch.setenv("FAKE_CODEX_STDERR", "boom: something broke")

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError) as excinfo:
        adapter.run_turn("hello", session_id=None, timeout_sec=5)

    assert "boom: something broke" in str(excinfo.value)
    assert "1" in str(excinfo.value)


def test_timeout_raises_adapter_error_and_writes_events_file(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CODEX_SLEEP", "5")

    adapter = make_adapter(tmp_path)
    with pytest.raises(AdapterError) as excinfo:
        adapter.run_turn("hello", session_id=None, timeout_sec=1)

    assert "timeout" in str(excinfo.value).lower()

    events_files = list((tmp_path / "events").glob("*.jsonl"))
    assert len(events_files) == 1


def test_readonly_adapter_uses_readonly_sandbox(tmp_path, monkeypatch):
    # A reviewer-side adapter must not be able to write to the repo.
    args_file = tmp_path / "args.jsonl"
    monkeypatch.setenv("FAKE_CODEX_ARGS_FILE", str(args_file))

    adapter = make_adapter(tmp_path, readonly=True)
    adapter.run_turn("review this", session_id=None, timeout_sec=5)

    argv = read_argv_lines(args_file)[0]
    idx = argv.index("--sandbox")
    assert argv[idx + 1] == "read-only"
