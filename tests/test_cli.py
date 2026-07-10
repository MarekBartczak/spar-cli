"""Tests for the spar CLI module."""

import json
from pathlib import Path

import pytest

import spar.cli as cli
from spar.cli import main
from spar.headless import HeadlessGate
from spar.exec.headless import HeadlessExecGate


class _FakeOrch:
    """Records which run method the CLI drives and returns a sentinel code."""

    def __init__(self, code):
        self.code = code
        self.ran_new = None
        self.ran_continue = False
        self.gate_choice = "unset"

    def run_new(self, task_prompt):
        self.ran_new = task_prompt
        return self.code

    def run_continue(self, gate_choice=None):
        self.ran_continue = True
        self.gate_choice = gate_choice
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

    def test_tasks_flag_threads_require_tasks_and_side_configs(self):
        from spar.config import load_config
        from pathlib import Path

        parser = cli._build_parser()
        args = parser.parse_args(["prompt", "--tasks"])
        config = load_config(Path.cwd())
        orch = cli._build_orchestrator(args, config)
        assert orch.require_tasks is True
        assert orch.side_configs == config.sides

    def test_default_has_require_tasks_false(self):
        from spar.config import load_config
        from pathlib import Path

        parser = cli._build_parser()
        args = parser.parse_args(["prompt"])
        config = load_config(Path.cwd())
        orch = cli._build_orchestrator(args, config)
        assert orch.require_tasks is False

    def test_quiet_flag_reaches_sink(self, tmp_path, monkeypatch):
        from spar.config import load_config

        monkeypatch.chdir(tmp_path)
        parser = cli._build_parser()
        args = parser.parse_args(["prompt", "--quiet"])
        config = load_config(Path.cwd())
        orch = cli._build_orchestrator(args, config)
        assert orch.sink is not None
        assert orch.sink.quiet is True
        orch.sink.close()

    def test_default_quiet_is_false(self, tmp_path, monkeypatch):
        from spar.config import load_config

        monkeypatch.chdir(tmp_path)
        parser = cli._build_parser()
        args = parser.parse_args(["prompt"])
        config = load_config(Path.cwd())
        orch = cli._build_orchestrator(args, config)
        assert orch.sink is not None
        assert orch.sink.quiet is False
        orch.sink.close()


class TestTasksFlagWiring:
    """`main(...)` threads the --tasks flag through to _build_orchestrator."""

    def test_tasks_flag_reaches_build(self, monkeypatch):
        captured = {}

        def _build(args, config):
            captured["tasks"] = args.tasks
            return _FakeOrch(code=0)

        monkeypatch.setattr(cli, "_build_orchestrator", _build)
        assert main(["prompt", "--tasks"]) == 0
        assert captured["tasks"] is True

    def test_no_tasks_flag_defaults_false(self, monkeypatch):
        captured = {}

        def _build(args, config):
            captured["tasks"] = args.tasks
            return _FakeOrch(code=0)

        monkeypatch.setattr(cli, "_build_orchestrator", _build)
        assert main(["prompt"]) == 0
        assert captured["tasks"] is False


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
        self.gate_choice = "unset"

    def run(self):
        self.ran = "run"
        return self.code

    def run_continue(self, gate_choice=None):
        self.ran = "run_continue"
        self.gate_choice = gate_choice
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


class TestHeadlessGateFlags:
    """--gate requires --continue and --headless together; --headless swaps gate."""

    def test_gate_without_continue_and_headless_is_usage_error(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["prompt", "--gate", "accept"])
        assert exc_info.value.code == 2

    def test_gate_without_headless_is_usage_error(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["--continue", "--gate", "accept"])
        assert exc_info.value.code == 2

    def test_gate_without_continue_but_headless_is_usage_error(self):
        with pytest.raises(SystemExit) as exc_info:
            main(["prompt", "--headless", "--gate", "accept"])
        assert exc_info.value.code == 2

    def test_bad_gate_value_is_exit_2(self, fake_orch, capsys):
        result = main(["--continue", "--headless", "--gate", "bogus"])
        assert result == 2
        err = capsys.readouterr().err
        assert "bogus" in err

    def test_valid_gate_value_threads_into_run_continue(self, fake_orch):
        fake_orch["code"] = 0
        result = main(["--continue", "--headless", "--gate", "accept"])
        assert result == 0
        gate_choice = fake_orch["orch"].gate_choice
        assert gate_choice is not None
        assert gate_choice.action == "accept"

    def test_plain_continue_passes_no_gate_choice(self, fake_orch):
        fake_orch["code"] = 0
        result = main(["--continue"])
        assert result == 0
        assert fake_orch["orch"].gate_choice is None

    def test_headless_flag_swaps_in_headless_gate(self):
        from spar.config import load_config

        parser = cli._build_parser()
        args = parser.parse_args(["prompt", "--headless"])
        config = load_config(Path.cwd())
        orch = cli._build_orchestrator(args, config)
        assert isinstance(orch.gate, HeadlessGate)

    def test_no_headless_flag_keeps_console_gate(self):
        from spar.config import load_config
        from spar.orchestrator import ConsoleGate

        parser = cli._build_parser()
        args = parser.parse_args(["prompt"])
        config = load_config(Path.cwd())
        orch = cli._build_orchestrator(args, config)
        assert isinstance(orch.gate, ConsoleGate)


class TestTaskFile:
    """--task-file is an alternate prompt source, mutually exclusive with the positional prompt."""

    def test_task_file_and_prompt_together_is_usage_error(self, tmp_path):
        task_file = tmp_path / "task.txt"
        task_file.write_text("do the thing", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            main(["some prompt", "--task-file", str(task_file)])
        assert exc_info.value.code == 2

    def test_task_file_and_continue_together_is_usage_error(self, tmp_path):
        task_file = tmp_path / "task.txt"
        task_file.write_text("do the thing", encoding="utf-8")
        with pytest.raises(SystemExit) as exc_info:
            main(["--continue", "--task-file", str(task_file)])
        assert exc_info.value.code == 2

    def test_task_file_content_reaches_orchestrator(self, tmp_path, fake_orch):
        task_file = tmp_path / "task.txt"
        task_file.write_text("build the widget\n", encoding="utf-8")
        fake_orch["code"] = 0
        result = main(["--task-file", str(task_file)])
        assert result == 0
        assert fake_orch["orch"].ran_new == "build the widget\n"

    def test_missing_task_file_is_usage_error(self, tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            main(["--task-file", str(tmp_path / "nope.txt")])
        assert exc_info.value.code == 2


class TestExecHeadlessGateFlags:
    """Same --gate/--headless contract, mirrored on the exec parser."""

    def test_gate_without_continue_and_headless_is_usage_error(self, tmp_path, monkeypatch):
        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        with pytest.raises(SystemExit) as exc_info:
            main(["exec", "--gate", "accept"])
        assert exc_info.value.code == 2

    def test_gate_without_headless_is_usage_error(self, tmp_path, monkeypatch):
        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        with pytest.raises(SystemExit) as exc_info:
            main(["exec", "--continue", "--gate", "accept"])
        assert exc_info.value.code == 2

    def test_valid_gate_value_threads_into_run_continue(
        self, tmp_path, monkeypatch, fake_executor
    ):
        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        fake_executor["code"] = 0
        result = main(["exec", "--continue", "--headless", "--gate", "accept"])
        assert result == 0
        gate_choice = fake_executor["executor"].gate_choice
        assert gate_choice is not None
        assert gate_choice.action == "accept"

    def test_headless_flag_swaps_in_headless_exec_gate(self, tmp_path, monkeypatch):
        from spar.config import load_config

        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        parser = cli._build_exec_parser()
        args = parser.parse_args(["--headless"])
        config = load_config(Path.cwd())
        plan_path = Path(".spar/artifact.md")
        plan_text = plan_path.read_text(encoding="utf-8")
        from spar.exec.tasklist import parse_task_list

        tasks = parse_task_list(plan_text, sides=config.sides, order=["claude", "codex"])
        executor = cli._build_executor(args, config, tasks, ["claude", "codex"], plan_path)
        assert isinstance(executor.gate, HeadlessExecGate)

    def test_quiet_flag_reaches_sink(self, tmp_path, monkeypatch):
        from spar.config import load_config
        from spar.exec.tasklist import parse_task_list

        _write_plan(tmp_path, monkeypatch, VALID_PLAN, with_side_config=True)
        parser = cli._build_exec_parser()
        args = parser.parse_args(["--quiet"])
        config = load_config(Path.cwd())
        plan_path = Path(".spar/artifact.md")
        plan_text = plan_path.read_text(encoding="utf-8")
        tasks = parse_task_list(plan_text, sides=config.sides, order=["claude", "codex"])
        executor = cli._build_executor(args, config, tasks, ["claude", "codex"], plan_path)
        assert executor.sink is not None
        assert executor.sink.quiet is True
        executor.sink.close()


class TestStatusSubcommand:
    """`spar status --json` reports current debate/execution state, or all-null if none."""

    def test_no_state_reports_all_null(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        result = main(["status", "--json"])
        assert result == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "phase": None,
            "pending_gate": None,
            "tasks": {},
            "artifact": None,
        }

    def test_debate_state_reports_phase_debate(self, tmp_path, monkeypatch, capsys):
        from spar.state import DebateState, StateStore

        monkeypatch.chdir(tmp_path)
        spar_dir = tmp_path / ".spar"
        store = StateStore(spar_dir)
        state = DebateState(
            round=2,
            pending_gate={"name": "consensus", "options": ["accept", "remarks", "abort"], "context": {}},
        )
        store.save(state)

        result = main(["status", "--json"])
        assert result == 0
        out = json.loads(capsys.readouterr().out)
        assert out["phase"] == "debate"
        assert out["tasks"] == {}
        assert out["pending_gate"] == {
            "name": "consensus",
            "options": ["accept", "remarks", "abort"],
            "context": {},
        }

    def test_exec_state_reports_tasks_and_takes_precedence(
        self, tmp_path, monkeypatch, capsys
    ):
        from spar.exec.state import ExecState, ExecStateStore, TaskState
        from spar.exec.tasklist import Task
        from spar.state import DebateState, StateStore

        monkeypatch.chdir(tmp_path)
        spar_dir = tmp_path / ".spar"

        # A debate session also exists, but exec state must take precedence.
        StateStore(spar_dir).save(DebateState())

        (spar_dir / "artifact.md").parent.mkdir(parents=True, exist_ok=True)
        (spar_dir / "artifact.md").write_text("# Plan\n", encoding="utf-8")

        task = Task(
            id="t1", description="do it", side="claude", model="sonnet",
            review_model="gpt-5.4", deps=(), files=("a.py",), test=None,
        )
        exec_state = ExecState(
            phase="execution",
            tasks={"t1": TaskState(task=task, status="merged")},
            pending_gate={"name": "final_merge", "options": ["accept", "abort"], "context": {}},
        )
        ExecStateStore(spar_dir).save(exec_state)

        result = main(["status", "--json"])
        assert result == 0
        out = json.loads(capsys.readouterr().out)
        assert out["phase"] == "execution"
        assert out["tasks"] == {"t1": {"status": "merged", "side": "claude", "model": "sonnet"}}
        assert out["pending_gate"]["name"] == "final_merge"
        assert out["artifact"] == str(Path(".spar") / "artifact.md")

    def test_status_without_json_flag_is_usage_error(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc_info:
            main(["status"])
        assert exc_info.value.code == 2
