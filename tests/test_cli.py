"""Tests for the spar CLI module."""

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
