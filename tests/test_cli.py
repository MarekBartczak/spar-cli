"""Tests for the spar CLI module."""

import pytest
from spar.cli import main


class TestHelpFlag:
    """Test --help flag behavior."""

    def test_help_exits_zero(self, capsys):
        """Test that --help exits with code 0."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "usage:" in captured.out or "usage" in captured.out


class TestNoArguments:
    """Test behavior with no arguments."""

    def test_no_args_errors(self):
        """Test that no arguments produces an error."""
        with pytest.raises(SystemExit) as exc_info:
            main([])
        assert exc_info.value.code == 2


class TestMutualExclusion:
    """Test mutual exclusion of prompt and --continue."""

    def test_prompt_and_continue_together_errors(self):
        """Test that prompt and --continue together produce an error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["some prompt", "--continue"])
        assert exc_info.value.code == 2

    def test_empty_string_prompt_and_continue_errors(self):
        """Test that empty string prompt and --continue together produce an error."""
        with pytest.raises(SystemExit) as exc_info:
            main(["", "--continue"])
        assert exc_info.value.code == 2


class TestFirstValidation:
    """Test --first validation against --sides."""

    def test_first_not_in_sides_errors(self):
        """Test that --first must be one of the --sides values."""
        with pytest.raises(SystemExit) as exc_info:
            main(["prompt", "--first", "gemini"])
        assert exc_info.value.code == 2


class TestValidPrompt:
    """Test valid prompt invocation."""

    def test_valid_prompt_returns_2_and_prints_stub(self, capsys):
        """Test that valid prompt returns 2 and prints the stub message."""
        result = main(["my prompt"])
        assert result == 2
        captured = capsys.readouterr()
        assert captured.err == "spar: not implemented yet\n"

    def test_empty_string_prompt_returns_2_and_prints_stub(self, capsys):
        """Test that empty string prompt is accepted and returns 2 with stub message."""
        result = main([""])
        assert result == 2
        captured = capsys.readouterr()
        assert captured.err == "spar: not implemented yet\n"


class TestValidContinue:
    """Test valid --continue invocation."""

    def test_valid_continue_returns_2_and_prints_stub(self, capsys):
        """Test that valid --continue returns 2 and prints the stub message."""
        result = main(["--continue"])
        assert result == 2
        captured = capsys.readouterr()
        assert captured.err == "spar: not implemented yet\n"


class TestArgumentParsing:
    """Test argument parsing with various combinations."""

    def test_valid_prompt_with_sides(self, capsys):
        """Test valid prompt with custom sides."""
        result = main(["prompt", "--sides", "claude,openai"])
        assert result == 2
        captured = capsys.readouterr()
        assert captured.err == "spar: not implemented yet\n"

    def test_valid_prompt_with_first_in_sides(self, capsys):
        """Test valid prompt with --first that is in --sides."""
        result = main(["prompt", "--sides", "claude,codex", "--first", "codex"])
        assert result == 2
        captured = capsys.readouterr()
        assert captured.err == "spar: not implemented yet\n"

    def test_valid_prompt_with_max_rounds(self, capsys):
        """Test valid prompt with custom max-rounds."""
        result = main(["prompt", "--max-rounds", "10"])
        assert result == 2
        captured = capsys.readouterr()
        assert captured.err == "spar: not implemented yet\n"

    def test_valid_prompt_with_artifact(self, capsys):
        """Test valid prompt with custom artifact path."""
        result = main(["prompt", "--artifact", "output.md"])
        assert result == 2
        captured = capsys.readouterr()
        assert captured.err == "spar: not implemented yet\n"
