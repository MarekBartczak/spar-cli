"""Tests for ``spar.sidecheck`` — the side-CLI preflight.

A debate/execution whose side command is not on PATH used to die with a bare
``FileNotFoundError`` traceback *after* the first turn had already run (the GUI
swallows the child's stderr, so it looked like the run simply froze). These
tests pin the pure check that refuses such a run up front, naming the side, the
command and the PATH that was searched.
"""

from __future__ import annotations

from spar.sidecheck import missing_side_commands, missing_side_commands_message


def _which_only(*present: str):
    def which(cmd: str) -> str | None:
        return f"/fake/bin/{cmd}" if cmd in present else None

    return which


def test_no_missing_commands_when_every_side_resolves():
    missing = missing_side_commands(
        {"claude": "claude", "codex": "codex"}, which=_which_only("claude", "codex")
    )

    assert missing == []


def test_reports_the_side_whose_command_is_not_on_path():
    missing = missing_side_commands(
        {"claude": "claude", "codex": "codex"}, which=_which_only("claude")
    )

    assert missing == [("codex", "codex")]


def test_only_the_program_token_of_a_command_is_probed():
    missing = missing_side_commands(
        {"codex": "codex --dangerously-bypass"}, which=_which_only("codex")
    )

    assert missing == []


def test_missing_order_follows_the_side_mapping():
    missing = missing_side_commands(
        {"codex": "codex", "claude": "claude"}, which=_which_only()
    )

    assert missing == [("codex", "codex"), ("claude", "claude")]


def test_message_names_side_command_and_searched_path():
    text = missing_side_commands_message([("codex", "codex")], path="/usr/bin:/bin")

    assert "codex" in text
    assert "/usr/bin:/bin" in text
    assert "PATH" in text
