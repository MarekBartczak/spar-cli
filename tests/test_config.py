"""Tests for the spar configuration module."""

import os
import pytest
from pathlib import Path
from spar.config import (
    SideConfig,
    DebateConfig,
    Config,
    ConfigError,
    load_config,
    set_global_command,
)


class TestDataclasses:
    """Test frozen dataclasses creation."""

    def test_side_config_creation(self):
        """Test SideConfig creation with required fields."""
        side = SideConfig(adapter="claude", command="claude")
        assert side.adapter == "claude"
        assert side.command == "claude"
        assert side.model == ""

    def test_side_config_with_model(self):
        """Test SideConfig with model override."""
        side = SideConfig(adapter="claude", command="claude", model="claude-3-opus")
        assert side.model == "claude-3-opus"

    def test_side_config_frozen(self):
        """Test that SideConfig is frozen."""
        side = SideConfig(adapter="claude", command="claude")
        with pytest.raises(Exception):  # FrozenInstanceError
            side.adapter = "codex"

    def test_debate_config_creation(self):
        """Test DebateConfig creation with defaults."""
        debate = DebateConfig()
        assert debate.max_rounds == 6
        assert debate.turn_timeout_sec == 900

    def test_debate_config_custom_values(self):
        """Test DebateConfig with custom values."""
        debate = DebateConfig(max_rounds=10, turn_timeout_sec=1200)
        assert debate.max_rounds == 10
        assert debate.turn_timeout_sec == 1200

    def test_debate_config_frozen(self):
        """Test that DebateConfig is frozen."""
        debate = DebateConfig()
        with pytest.raises(Exception):  # FrozenInstanceError
            debate.max_rounds = 10

    def test_config_creation(self):
        """Test Config creation."""
        sides = {
            "claude": SideConfig(adapter="claude", command="claude"),
            "codex": SideConfig(adapter="codex", command="codex"),
        }
        debate = DebateConfig()
        config = Config(sides=sides, debate=debate)
        assert config.sides == sides
        assert config.debate == debate

    def test_config_frozen(self):
        """Test that Config is frozen."""
        sides = {"claude": SideConfig(adapter="claude", command="claude")}
        config = Config(sides=sides, debate=DebateConfig())
        with pytest.raises(Exception):  # FrozenInstanceError
            config.sides = {}


class TestLoadConfigNoFiles:
    """Test loading config with no files — pure defaults."""

    def test_no_files_returns_defaults(self, tmp_path):
        """Test that missing files return default config."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        config = load_config(project_dir)

        assert "claude" in config.sides
        assert "codex" in config.sides
        assert config.sides["claude"].adapter == "claude"
        assert config.sides["claude"].command == "claude"
        assert config.sides["claude"].model == ""
        assert config.sides["codex"].adapter == "codex"
        assert config.sides["codex"].command == "codex"
        assert config.sides["codex"].model == ""
        assert config.debate.max_rounds == 6
        assert config.debate.turn_timeout_sec == 900


class TestLoadConfigGlobalOnly:
    """Test loading config with global file only."""

    def test_global_only_overrides_defaults(self, tmp_path):
        """Test that global config overrides defaults."""
        global_config = tmp_path / "global_config.toml"
        global_config.write_text(
            """
[sides.claude]
adapter = "claude"
command = "custom-claude"

[debate]
max_rounds = 8
"""
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        config = load_config(project_dir, global_path=global_config)

        # Global overrides
        assert config.sides["claude"].command == "custom-claude"
        assert config.debate.max_rounds == 8
        # Defaults remain for what's not overridden
        assert config.sides["claude"].model == ""
        assert config.debate.turn_timeout_sec == 900
        assert "codex" in config.sides


class TestLoadConfigProjectOnly:
    """Test loading config with project file only."""

    def test_project_only_overrides_defaults(self, tmp_path):
        """Test that project config overrides defaults."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[sides.claude]
model = "claude-3-opus"

[debate]
turn_timeout_sec = 1200
"""
        )

        config = load_config(project_dir)

        # Project overrides
        assert config.sides["claude"].model == "claude-3-opus"
        assert config.debate.turn_timeout_sec == 1200
        # Defaults remain for what's not overridden
        assert config.sides["claude"].adapter == "claude"
        assert config.sides["claude"].command == "claude"
        assert config.debate.max_rounds == 6


class TestLoadConfigBothFiles:
    """Test loading config with both global and project files."""

    def test_project_overrides_global_per_key(self, tmp_path):
        """Test that project config deep-merges and overrides global per-key."""
        global_config = tmp_path / "global_config.toml"
        global_config.write_text(
            """
[sides.claude]
adapter = "claude"
command = "global-claude"
model = "claude-3-sonnet"

[sides.codex]
adapter = "codex"
command = "global-codex"

[debate]
max_rounds = 8
turn_timeout_sec = 1000
"""
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[sides.claude]
command = "project-claude"

[debate]
max_rounds = 10
"""
        )

        config = load_config(project_dir, global_path=global_config)

        # Project overrides global per-key (deep merge)
        assert config.sides["claude"].command == "project-claude"  # project wins
        assert config.sides["claude"].model == "claude-3-sonnet"  # from global
        assert config.sides["claude"].adapter == "claude"  # from global
        # Global still applies where not overridden by project
        assert config.sides["codex"].command == "global-codex"
        assert config.debate.max_rounds == 10  # project wins
        assert config.debate.turn_timeout_sec == 1000  # from global

    def test_defaults_apply_where_no_config(self, tmp_path):
        """Test that defaults apply where neither global nor project define values."""
        global_config = tmp_path / "global_config.toml"
        global_config.write_text(
            """
[sides.claude]
command = "custom-claude"
"""
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[debate]
max_rounds = 12
"""
        )

        config = load_config(project_dir, global_path=global_config)

        # Defaults + global + project
        assert config.sides["claude"].command == "custom-claude"  # from global
        assert config.sides["claude"].adapter == "claude"  # from defaults
        assert config.sides["claude"].model == ""  # from defaults
        assert config.sides["codex"].command == "codex"  # from defaults
        assert config.debate.max_rounds == 12  # from project
        assert config.debate.turn_timeout_sec == 900  # from defaults


class TestLoadConfigXDGConfigHome:
    """Test XDG_CONFIG_HOME support."""

    def test_xdg_config_home_honored(self, tmp_path, monkeypatch):
        """Test that XDG_CONFIG_HOME is used for global config when set."""
        xdg_config = tmp_path / "xdg_config"
        xdg_config.mkdir()
        spar_dir = xdg_config / "spar"
        spar_dir.mkdir()
        global_config = spar_dir / "config.toml"
        global_config.write_text(
            """
[sides.claude]
command = "xdg-claude"
"""
        )

        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        config = load_config(project_dir)

        assert config.sides["claude"].command == "xdg-claude"

    def test_home_config_fallback(self, tmp_path, monkeypatch):
        """Test that ~/.config/spar/config.toml is used when XDG_CONFIG_HOME not set."""
        # Create a fake home directory
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        config_dir = fake_home / ".config" / "spar"
        config_dir.mkdir(parents=True)
        global_config = config_dir / "config.toml"
        global_config.write_text(
            """
[sides.claude]
command = "home-claude"
"""
        )

        # Unset XDG_CONFIG_HOME and set HOME
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(fake_home))

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        config = load_config(project_dir)

        assert config.sides["claude"].command == "home-claude"

    def test_explicit_global_path_overrides_xdg(self, tmp_path, monkeypatch):
        """Test that explicit global_path parameter overrides XDG/HOME lookup."""
        # Create XDG config (should be ignored)
        xdg_config = tmp_path / "xdg_config"
        xdg_config.mkdir()
        spar_dir = xdg_config / "spar"
        spar_dir.mkdir()
        xdg_global = spar_dir / "config.toml"
        xdg_global.write_text(
            """
[sides.claude]
command = "xdg-claude"
"""
        )

        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))

        # Create explicit config (should be used)
        explicit_config = tmp_path / "explicit.toml"
        explicit_config.write_text(
            """
[sides.claude]
command = "explicit-claude"
"""
        )

        project_dir = tmp_path / "project"
        project_dir.mkdir()

        config = load_config(project_dir, global_path=explicit_config)

        assert config.sides["claude"].command == "explicit-claude"


class TestSetGlobalCommand:
    """Test persisting a per-side command override to the global config."""

    def test_writes_new_global_config_file(self, tmp_path):
        gp = tmp_path / "cfg" / "config.toml"
        set_global_command("codex", "codex-priv", global_path=gp)

        assert gp.exists()
        config = load_config(tmp_path / "project", global_path=gp)
        assert config.sides["codex"].command == "codex-priv"
        assert config.sides["codex"].adapter == "codex"

    def test_leaves_other_default_side_untouched(self, tmp_path):
        gp = tmp_path / "config.toml"
        set_global_command("codex", "codex-priv", global_path=gp)

        config = load_config(tmp_path / "project", global_path=gp)
        assert config.sides["claude"].command == "claude"

    def test_multiple_sides_accumulate(self, tmp_path):
        gp = tmp_path / "config.toml"
        set_global_command("claude", "claude-erli", global_path=gp)
        set_global_command("codex", "codex-priv", global_path=gp)

        config = load_config(tmp_path / "project", global_path=gp)
        assert config.sides["claude"].command == "claude-erli"
        assert config.sides["codex"].command == "codex-priv"

    def test_overwrites_existing_command(self, tmp_path):
        gp = tmp_path / "config.toml"
        set_global_command("claude", "claude-erli", global_path=gp)
        set_global_command("claude", "claude-priv", global_path=gp)

        config = load_config(tmp_path / "project", global_path=gp)
        assert config.sides["claude"].command == "claude-priv"

    def test_preserves_unrelated_debate_section(self, tmp_path):
        gp = tmp_path / "config.toml"
        gp.write_text("[debate]\nmax_rounds = 9\n")
        set_global_command("claude", "claude-erli", global_path=gp)

        config = load_config(tmp_path / "project", global_path=gp)
        assert config.debate.max_rounds == 9
        assert config.sides["claude"].command == "claude-erli"

    def test_rejects_unknown_adapter(self, tmp_path):
        with pytest.raises(ConfigError):
            set_global_command("gemini", "gemini", global_path=tmp_path / "c.toml")

    def test_rejects_empty_command(self, tmp_path):
        with pytest.raises(ConfigError):
            set_global_command("claude", "   ", global_path=tmp_path / "c.toml")

    def test_returns_written_path(self, tmp_path):
        gp = tmp_path / "config.toml"
        result = set_global_command("codex", "codex-priv", global_path=gp)
        assert result == gp


class TestValidationAdapterValues:
    """Test validation of adapter values."""

    def test_unknown_adapter_raises_error(self, tmp_path):
        """Test that unknown adapter value raises ConfigError."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[sides.gemini]
adapter = "gemini"
command = "gemini"
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "adapter" in str(exc_info.value).lower()

    def test_valid_adapters_accepted(self, tmp_path):
        """Test that valid adapter values are accepted."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[sides.claude]
adapter = "claude"
command = "claude"

[sides.codex]
adapter = "codex"
command = "codex"
"""
        )

        config = load_config(project_dir)
        assert config.sides["claude"].adapter == "claude"
        assert config.sides["codex"].adapter == "codex"


class TestValidationDebateConfig:
    """Test validation of debate configuration."""

    def test_max_rounds_must_be_positive(self, tmp_path):
        """Test that max_rounds must be >= 1."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[debate]
max_rounds = 0
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "max_rounds" in str(exc_info.value).lower()

    def test_turn_timeout_sec_must_be_positive(self, tmp_path):
        """Test that turn_timeout_sec must be >= 1."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[debate]
turn_timeout_sec = -100
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "turn_timeout_sec" in str(exc_info.value).lower()

    def test_max_rounds_not_integer_raises_error(self, tmp_path):
        """Test that max_rounds must be an integer."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[debate]
max_rounds = 6.5
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "max_rounds" in str(exc_info.value).lower()


class TestValidationSideConfig:
    """Test validation of side configuration."""

    def test_side_with_empty_command_raises_error(self, tmp_path):
        """Test that side with empty command raises ConfigError."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[sides.claude]
adapter = "claude"
command = ""
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "command" in str(exc_info.value).lower()

    def test_side_without_command_uses_default_or_errors(self, tmp_path):
        """Test that side without explicit command must have default set."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[sides.custom]
adapter = "claude"
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "command" in str(exc_info.value).lower()


class TestValidationUnknownKeys:
    """Test validation of unknown keys and tables."""

    def test_unknown_top_level_table_raises_error(self, tmp_path):
        """Test that unknown top-level table raises ConfigError."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[unknown_table]
key = "value"
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "unknown_table" in str(exc_info.value).lower()

    def test_unknown_key_in_debate_raises_error(self, tmp_path):
        """Test that unknown key in debate table raises ConfigError."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[debate]
max_rounds = 6
unknown_key = "value"
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "unknown_key" in str(exc_info.value).lower()

    def test_unknown_key_in_side_raises_error(self, tmp_path):
        """Test that unknown key in side table raises ConfigError."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text(
            """
[sides.claude]
adapter = "claude"
command = "claude"
unknown_field = "value"
"""
        )

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "unknown_field" in str(exc_info.value).lower()


class TestValidationMalformedTOML:
    """Test validation of malformed TOML."""

    def test_malformed_toml_raises_config_error(self, tmp_path):
        """Test that malformed TOML raises ConfigError."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text("this is not valid TOML [[[")

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        # Should wrap the tomllib error
        assert len(str(exc_info.value)) > 0


class TestValidationMalformedShape:
    """Test validation of malformed config shapes (sides/debate not tables)."""

    def test_sides_as_string_raises_error(self, tmp_path):
        """Test that sides = "oops" raises ConfigError naming the key."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text('sides = "oops"')

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "sides" in str(exc_info.value).lower()

    def test_debate_as_integer_raises_error(self, tmp_path):
        """Test that debate = 5 raises ConfigError naming the key."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text("debate = 5")

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "debate" in str(exc_info.value).lower()

    def test_side_with_non_table_value_raises_error(self, tmp_path):
        """Test that [sides.claude] with non-table value raises ConfigError."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        project_config = project_dir / ".spar" / "config.toml"
        project_config.parent.mkdir(parents=True)
        project_config.write_text('[sides]\nclaude = "oops"')

        with pytest.raises(ConfigError) as exc_info:
            load_config(project_dir)

        assert "claude" in str(exc_info.value).lower() and "table" in str(exc_info.value).lower()


class TestConfigErrorException:
    """Test ConfigError exception."""

    def test_config_error_is_exception(self):
        """Test that ConfigError is an Exception."""
        error = ConfigError("test message")
        assert isinstance(error, Exception)
        assert str(error) == "test message"
