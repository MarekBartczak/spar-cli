"""Configuration module for spar-cli."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ConfigError(Exception):
    """Exception raised for configuration errors."""

    pass


@dataclass(frozen=True)
class SideConfig:
    """Configuration for a debate side (adapter)."""

    adapter: str
    command: str
    model: str = ""
    models: tuple[str, ...] = ()
    default_model: str = ""


@dataclass(frozen=True)
class DebateConfig:
    """Configuration for debate parameters."""

    max_rounds: int = 6
    turn_timeout_sec: int = 900


@dataclass(frozen=True)
class ExecutionConfig:
    """Configuration for execution parameters."""

    test_command: str = ""
    max_review_rounds: int = 0


@dataclass(frozen=True)
class Config:
    """Complete application configuration."""

    sides: dict[str, SideConfig]
    debate: DebateConfig
    execution: ExecutionConfig


def _get_default_config() -> Config:
    """Return the default configuration."""
    return Config(
        sides={
            "claude": SideConfig(adapter="claude", command="claude"),
            "codex": SideConfig(adapter="codex", command="codex"),
        },
        debate=DebateConfig(),
        execution=ExecutionConfig(),
    )


def _load_toml_file(file_path: Path) -> dict:
    """Load and parse a TOML file, return empty dict if file doesn't exist."""
    if not file_path.exists():
        return {}

    try:
        with open(file_path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Malformed TOML in {file_path}: {e}")


def _get_global_config_path() -> Path:
    """Get the global config file path, respecting XDG_CONFIG_HOME."""
    if xdg_config_home := os.environ.get("XDG_CONFIG_HOME"):
        return Path(xdg_config_home) / "spar" / "config.toml"
    else:
        return Path.home() / ".config" / "spar" / "config.toml"


def _validate_adapter(adapter: str) -> None:
    """Validate that adapter is one of the allowed values."""
    allowed = {"claude", "codex"}
    if adapter not in allowed:
        raise ConfigError(f"Unknown adapter value: {adapter}")


def _validate_debate_config(config: dict) -> None:
    """Validate debate configuration."""
    allowed_keys = {"max_rounds", "turn_timeout_sec"}
    for key in config.keys():
        if key not in allowed_keys:
            raise ConfigError(f"Unknown key in debate config: {key}")

    if "max_rounds" in config:
        value = config["max_rounds"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"max_rounds must be an integer, got {type(value).__name__}")
        if value < 1:
            raise ConfigError(f"max_rounds must be >= 1, got {value}")

    if "turn_timeout_sec" in config:
        value = config["turn_timeout_sec"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(
                f"turn_timeout_sec must be an integer, got {type(value).__name__}"
            )
        if value < 1:
            raise ConfigError(f"turn_timeout_sec must be >= 1, got {value}")


def _validate_execution_config(config: dict) -> None:
    """Validate execution configuration."""
    allowed_keys = {"test_command", "max_review_rounds"}
    for key in config.keys():
        if key not in allowed_keys:
            raise ConfigError(f"Unknown key in execution config: {key}")

    if "test_command" in config:
        value = config["test_command"]
        if not isinstance(value, str):
            raise ConfigError(f"test_command must be a string, got {type(value).__name__}")

    if "max_review_rounds" in config:
        value = config["max_review_rounds"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"max_review_rounds must be an integer, got {type(value).__name__}")
        if value < 0:
            raise ConfigError(f"max_review_rounds must be >= 0, got {value}")


def _validate_side_config(side_name: str, config: dict) -> None:
    """Validate a side configuration."""
    allowed_keys = {"adapter", "command", "model", "models", "default_model"}
    for key in config.keys():
        if key not in allowed_keys:
            raise ConfigError(f"Unknown key in side '{side_name}': {key}")

    if "adapter" in config:
        _validate_adapter(config["adapter"])

    if "command" in config:
        command = config["command"]
        if not command or (isinstance(command, str) and not command.strip()):
            raise ConfigError(f"Side '{side_name}': command cannot be empty")

    if "models" in config:
        models = config["models"]
        if not isinstance(models, list):
            raise ConfigError(f"Side '{side_name}': models must be a list, got {type(models).__name__}")
        for model in models:
            if not isinstance(model, str) or not model.strip():
                raise ConfigError(f"Side '{side_name}': models must be a list of non-empty strings")

    if "default_model" in config:
        default_model = config["default_model"]
        if not isinstance(default_model, str):
            raise ConfigError(f"Side '{side_name}': default_model must be a string, got {type(default_model).__name__}")
        # Only validate if both models and default_model are provided and non-empty
        if "models" in config and default_model and config["models"]:
            models = config["models"]
            if default_model not in models:
                raise ConfigError(f"Side '{side_name}': default_model '{default_model}' must be in models")


def _merge_dicts(base: dict, override: dict) -> dict:
    """Deep merge override dict into base dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _dict_to_config(config_dict: dict) -> Config:
    """Convert a config dictionary to a Config object."""
    defaults = _get_default_config()

    # Validate top-level keys
    allowed_top_level = {"sides", "debate", "execution"}
    for key in config_dict.keys():
        if key not in allowed_top_level:
            raise ConfigError(f"Unknown top-level key: {key}")

    # Validate that sides is a dict (table) if present
    if "sides" in config_dict and not isinstance(config_dict["sides"], dict):
        raise ConfigError(f"sides must be a table (dict), got {type(config_dict['sides']).__name__}")

    # Validate that debate is a dict (table) if present
    if "debate" in config_dict and not isinstance(config_dict["debate"], dict):
        raise ConfigError(f"debate must be a table (dict), got {type(config_dict['debate']).__name__}")

    # Validate that execution is a dict (table) if present
    if "execution" in config_dict and not isinstance(config_dict["execution"], dict):
        raise ConfigError(f"execution must be a table (dict), got {type(config_dict['execution']).__name__}")

    # Process sides
    sides_dict = config_dict.get("sides", {})
    sides = {}

    # Start with defaults
    for side_name, default_side in defaults.sides.items():
        sides[side_name] = default_side

    # Merge in provided sides
    for side_name, side_config in sides_dict.items():
        if not isinstance(side_config, dict):
            raise ConfigError(f"Side '{side_name}' must be a table")

        _validate_side_config(side_name, side_config)

        # Get base side (default or existing)
        if side_name in sides:
            base_side = sides[side_name]
            adapter = side_config.get("adapter", base_side.adapter)
            command = side_config.get("command", base_side.command)
            model = side_config.get("model", base_side.model)
            models = tuple(side_config.get("models", base_side.models)) if "models" in side_config else base_side.models
            default_model = side_config.get("default_model", base_side.default_model)
        else:
            # New side - must have adapter and command
            if "adapter" not in side_config:
                raise ConfigError(f"Side '{side_name}': adapter is required")
            if "command" not in side_config:
                raise ConfigError(f"Side '{side_name}': command is required")
            adapter = side_config["adapter"]
            command = side_config["command"]
            model = side_config.get("model", "")
            models = tuple(side_config.get("models", ())) if "models" in side_config else ()
            default_model = side_config.get("default_model", "")

        # Final validation of command
        if not command or (isinstance(command, str) and not command.strip()):
            raise ConfigError(f"Side '{side_name}': command cannot be empty")

        sides[side_name] = SideConfig(
            adapter=adapter, command=command, model=model, models=models, default_model=default_model
        )

    # Process debate config
    debate_dict = config_dict.get("debate", {})
    if debate_dict:
        _validate_debate_config(debate_dict)
        max_rounds = debate_dict.get("max_rounds", defaults.debate.max_rounds)
        turn_timeout_sec = debate_dict.get(
            "turn_timeout_sec", defaults.debate.turn_timeout_sec
        )
    else:
        max_rounds = defaults.debate.max_rounds
        turn_timeout_sec = defaults.debate.turn_timeout_sec

    debate = DebateConfig(max_rounds=max_rounds, turn_timeout_sec=turn_timeout_sec)

    # Process execution config
    execution_dict = config_dict.get("execution", {})
    if execution_dict:
        _validate_execution_config(execution_dict)
        test_command = execution_dict.get("test_command", defaults.execution.test_command)
        max_review_rounds = execution_dict.get(
            "max_review_rounds", defaults.execution.max_review_rounds
        )
    else:
        test_command = defaults.execution.test_command
        max_review_rounds = defaults.execution.max_review_rounds

    execution = ExecutionConfig(test_command=test_command, max_review_rounds=max_review_rounds)

    return Config(sides=sides, debate=debate, execution=execution)


def load_config(project_dir: Path, global_path: Optional[Path] = None) -> Config:
    """
    Load and merge configuration from global and project files.

    Merge precedence: defaults < global < project

    Args:
        project_dir: Path to the project directory
        global_path: Optional path to global config file. If not provided,
                    uses ~/.config/spar/config.toml (or $XDG_CONFIG_HOME/spar/config.toml)

    Returns:
        Merged Config object

    Raises:
        ConfigError: If configuration is invalid or malformed
    """
    # Determine global config path
    if global_path is None:
        global_path = _get_global_config_path()

    # Load files
    global_toml = _load_toml_file(global_path)
    project_toml_path = project_dir / ".spar" / "config.toml"
    project_toml = _load_toml_file(project_toml_path)

    # Merge configs (defaults < global < project)
    # Start with defaults as a dict
    defaults = _get_default_config()
    defaults_dict = {
        "sides": {
            name: {"adapter": side.adapter, "command": side.command, "model": side.model, "models": list(side.models), "default_model": side.default_model}
            for name, side in defaults.sides.items()
        },
        "debate": {
            "max_rounds": defaults.debate.max_rounds,
            "turn_timeout_sec": defaults.debate.turn_timeout_sec,
        },
        "execution": {
            "test_command": defaults.execution.test_command,
            "max_review_rounds": defaults.execution.max_review_rounds,
        },
    }

    # Merge global into defaults
    merged = _merge_dicts(defaults_dict, global_toml)

    # Merge project into merged
    merged = _merge_dicts(merged, project_toml)

    # Convert merged dict to Config object
    return _dict_to_config(merged)


def _escape_toml_str(value: object) -> str:
    """Escape a value for a TOML basic (double-quoted) string."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _dump_config_toml(raw: dict) -> str:
    """Serialize a config dict (our restricted schema) back to TOML text.

    Handles only the structure spar itself produces: ``[sides.<name>]`` tables
    with string scalars and a ``[debate]`` table with string/int scalars. This
    is a deliberate, minimal writer — stdlib has no TOML serializer and the
    schema is small and fully controlled.
    """
    lines: list[str] = []
    for name, cfg in raw.get("sides", {}).items():
        lines.append(f"[sides.{name}]")
        for key in ("adapter", "command", "model", "default_model"):
            val = cfg.get(key)
            if val is None or val == "":
                continue
            lines.append(f'{key} = "{_escape_toml_str(val)}"')
        # Handle models list separately
        models = cfg.get("models")
        if models:
            models_str = "[" + ", ".join(f'"{_escape_toml_str(m)}"' for m in models) + "]"
            lines.append(f"models = {models_str}")
        lines.append("")

    debate = raw.get("debate", {})
    if debate:
        lines.append("[debate]")
        for key, val in debate.items():
            if isinstance(val, bool):
                lines.append(f"{key} = {'true' if val else 'false'}")
            elif isinstance(val, (int, float)):
                lines.append(f"{key} = {val}")
            else:
                lines.append(f'{key} = "{_escape_toml_str(val)}"')
        lines.append("")

    execution = raw.get("execution", {})
    if execution:
        lines.append("[execution]")
        for key, val in execution.items():
            if isinstance(val, bool):
                lines.append(f"{key} = {'true' if val else 'false'}")
            elif isinstance(val, (int, float)):
                lines.append(f"{key} = {val}")
            else:
                lines.append(f'{key} = "{_escape_toml_str(val)}"')
        lines.append("")

    text = "\n".join(lines).strip("\n")
    return text + "\n" if text else ""


def set_global_command(
    adapter: str, command: str, global_path: Optional[Path] = None
) -> Path:
    """Persist ``command`` as the CLI binary for side ``adapter`` in the global config.

    Reads the existing global config (if any), sets ``[sides.<adapter>].command``
    (and its ``adapter`` field), then writes the file back atomically. Other
    sides and the ``[debate]`` section are preserved. Returns the path written.

    Raises ``ConfigError`` for an unknown adapter or an empty command.
    """
    _validate_adapter(adapter)
    if not command or not command.strip():
        raise ConfigError(f"command for side '{adapter}' cannot be empty")
    command = command.strip()

    if global_path is None:
        global_path = _get_global_config_path()

    raw = _load_toml_file(global_path)
    sides = raw.setdefault("sides", {})
    side = sides.setdefault(adapter, {})
    side["adapter"] = adapter
    side["command"] = command

    global_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = global_path.with_name(global_path.name + ".tmp")
    tmp.write_text(_dump_config_toml(raw), encoding="utf-8")
    os.replace(tmp, global_path)
    return global_path
