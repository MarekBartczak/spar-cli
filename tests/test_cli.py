"""Tests for the spar CLI module."""

from pathlib import Path

import pytest

import spar.cli as cli
from spar.cli import main


class _FakeOrch:
    """Records which run method the CLI drives and returns a sentinel code."""

    def __init__(self, code):
        self.code = code
        self.ran_new = None
        self.ran_continue = False

    def run_new(self, task_prompt):
        self.ran_new = task_prompt
        return self.code

    def run_continue(self):
        self.ran_continue = True
        return self.code


@pytest.fixture
def fake_orch(monkeypatch):
    """Replace ``_build_orchestrator`` so no real adapters/subprocesses run."""
    holder = {}

    def _build(args, config):
        orch = _FakeOrch(code=holder.get("code", 0))
        holder["orch"] = orch
        return orch

    monkeypatch.setattr(cli, "_build_orchestrator", _build)
    return holder


class TestHelpFlag:
    def test_help_exits_zero(self, capsys):
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "usage" in captured.out


class TestUsageErrors:
    def test_no_args_errors(self):
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2

    def test_prompt_and_continue_together_errors(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["some prompt", "--continue"])
        assert exc_info.value.code == 2

    def test_first_not_in_sides_errors(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["prompt", "--first", "gemini"])
        assert exc_info.value.code == 2


class TestWiring:
    def test_valid_prompt_runs_new(self, fake_orch):
        fake_orch["code"] = 0
        result = main(["my prompt"])
        assert result == 0
        assert fake_orch["orch"].ran_new == "my prompt"
        assert fake_orch["orch"].ran_continue is False

    def test_continue_runs_continue(self, fake_orch):
        fake_orch["code"] = 3
        result = main(["--continue"])
        assert result == 3
        assert fake_orch["orch"].ran_continue is True
        assert fake_orch["orch"].ran_new is None

    def test_exit_code_is_propagated(self, fake_orch):
        fake_orch["code"] = 5
        assert main(["do a thing"]) == 5


class TestBuildOrchestrator:
    def test_order_places_first_side_first(self):
        from spar.config import load_config
        from pathlib import Path

        parser = cli._build_parser()
        args = parser.parse_args(["prompt", "--sides", "claude,codex", "--first", "codex"])
        config = load_config(Path.cwd())
        orch = cli._build_orchestrator(args, config)
        assert orch.order == ["codex", "claude"]

    def test_unknown_side_is_usage_error(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["prompt", "--sides", "claude,ghost", "--first", "ghost"])
        assert exc_info.value.code == 2

    def test_max_rounds_override(self):
        from spar.config import load_config
        from pathlib import Path

        parser = cli._build_parser()
        args = parser.parse_args(["prompt", "--max-rounds", "11"])
        config = load_config(Path.cwd())
        orch = cli._build_orchestrator(args, config)
        assert orch.debate.max_rounds == 11


class TestSetCommandMode:
    """`spar -m <side> -setCommand <binary>` persists a global command override."""

    def test_set_command_writes_global_and_exits_zero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        rc = main(["-m", "codex", "-setCommand", "codex-priv"])
        assert rc == 0

        from spar.config import load_config

        config = load_config(tmp_path / "project")
        assert config.sides["codex"].command == "codex-priv"

    def test_set_command_does_not_run_debate(self, tmp_path, monkeypatch, fake_orch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        rc = main(["-m", "claude", "-setCommand", "claude-erli"])
        assert rc == 0
        # orchestrator never built → no debate ran
        assert "orch" not in fake_orch

    def test_long_form_flags(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

        rc = main(["--adapter", "claude", "--set-command", "claude-erli"])
        assert rc == 0

        from spar.config import load_config

        config = load_config(tmp_path / "project")
        assert config.sides["claude"].command == "claude-erli"

    def test_set_command_requires_adapter(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["-setCommand", "codex-priv"])
        assert exc_info.value.code == 2

    def test_unknown_adapter_is_usage_error(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["-m", "gemini", "-setCommand", "gemini"])
        assert exc_info.value.code == 2

    def test_adapter_without_set_command_is_usage_error(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["-m", "claude", "some prompt"])
        assert exc_info.value.code == 2

    def test_list_commands_shows_resolved_commands(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        main(["-m", "claude", "-setCommand", "claude-erli"])

        rc = main(["--list-commands"])
        assert rc == 0

        out = capsys.readouterr().out
        assert "claude" in out
        assert "claude-erli" in out
        assert "codex" in out


VALID_PLAN = """# Plan
blah
## Tasks
- [t1] config bits | side=claude | model=sonnet | review=gpt-5.4 | deps=- | files=spar/config.py
## Next
"""

NO_TASKS_PLAN = "# Plan\nno tasks here\n"

_SIDE_CONFIG_TOML = """
[sides.claude]
adapter = "claude"
command = "claude"
models = ["opus", "sonnet"]
default_model = "sonnet"

[sides.codex]
adapter = "codex"
command = "codex"
models = ["gpt-5.5", "gpt-5.4"]
default_model = "gpt-5.4"
"""


class _FakeExecutor:
    """Records which run method the CLI drives and returns a sentinel code."""

    def __init__(self, code, auto_integration_merge, merge_sessions):
        self.code = code
        self.auto_integration_merge = auto_integration_merge
        self.merge_sessions = merge_sessions
        self.ran = None

    def run(self):
        self.ran = "run"
        return self.code

    def run_continue(self):
        self.ran = "run_continue"
        return self.code


@pytest.fixture
def fake_executor(monkeypatch):
    """Replace ``_build_executor`` so no real adapters/git run."""
    holder = {}

    def _build(args, config, tasks, order, plan_path):
        executor = _FakeExecutor(
            code=holder.get("code", 0),
            auto_integration_merge=args.auto_integration_merge,
            merge_sessions=args.merge_sessions,
        )
        holder["executor"] = executor
        holder["tasks"] = tasks
        holder["order"] = order
        return executor

    monkeypatch.setattr(cli, "_build_executor", _build)
    return holder


def _write_plan(tmp_path, monkeypatch, text, *, with_side_config=False):
    spar_dir = tmp_path / ".spar"
    spar_dir.mkdir(parents=True, exist_ok=True)
    (spar_dir / "artifact.md").write_text(text, encoding="utf-8")
    if with_side_config:
        (spar_dir / "config.toml").write_text(_SIDE_CONFIG_TOML, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config-home"))
    monkeypatch.chdir(tmp_path)


class TestExecSubcommand:
    def test_no_tasks_section_is_usage_error(self, tmp_path, monkeypatch, capsys):
        _write_plan(tmp_path, monkeypatch, NO_TASKS_PLAN)
        result = main(["exec"])
        assert result == 2
        err = capsys.readouterr().err
        assert "run a debate to consensus" in err

    def test_missing_artifact_is_usage_error(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-config-home"))
        monkeypatch.chdir(tmp_path)
        result = main(["exec"])
        assert result == 2

    def test_valid_plan_runs_executor(self, tmp_path, monkeypatch, fake_executor):
        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        fake_executor["code"] = 0
        result = main(["exec"])
        assert result == 0
        assert fake_executor["executor"].ran == "run"
        assert [t.id for t in fake_executor["tasks"]] == ["t1"]

    def test_continue_calls_run_continue(self, tmp_path, monkeypatch, fake_executor):
        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        fake_executor["code"] = 3
        result = main(["exec", "--continue"])
        assert result == 3
        assert fake_executor["executor"].ran == "run_continue"

    def test_exit_code_propagated(self, tmp_path, monkeypatch, fake_executor):
        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        fake_executor["code"] = 5
        assert main(["exec"]) == 5

    def test_auto_integration_merge_and_merge_sessions_parse(
        self, tmp_path, monkeypatch, fake_executor
    ):
        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        result = main(["exec", "--auto-integration-merge", "--merge-sessions"])
        assert result == 0
        assert fake_executor["executor"].auto_integration_merge is True
        assert fake_executor["executor"].merge_sessions is True

    def test_debate_command_still_works_after_exec_added(self, fake_orch):
        fake_orch["code"] = 0
        result = main(["my prompt"])
        assert result == 0
        assert fake_orch["orch"].ran_new == "my prompt"
