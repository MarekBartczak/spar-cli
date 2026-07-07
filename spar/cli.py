"""Command-line interface for spar-cli."""

import argparse
import sys
from pathlib import Path

from spar.adapters.claude import ClaudeAdapter
from spar.adapters.codex import CodexAdapter
from spar.config import ConfigError, DebateConfig, load_config
from spar.guard import Guard
from spar.orchestrator import ConsoleGate, Orchestrator
from spar.state import StateStore

_ADAPTERS = {"claude": ClaudeAdapter, "codex": CodexAdapter}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spar",
        description="CLI tool orchestrating debates between AI CLIs",
    )
    parser.add_argument(
        "prompt", nargs="?", help="Task description starting a new debate"
    )
    parser.add_argument(
        "--continue", dest="cont", action="store_true",
        help="Resume interrupted debate",
    )
    parser.add_argument(
        "--sides", default="claude,codex",
        help="Comma-separated list of sides (default: claude,codex)",
    )
    parser.add_argument(
        "--first", default="claude",
        help="Which side goes first (default: claude)",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=None,
        help="Override the configured maximum number of rounds",
    )
    parser.add_argument(
        "--artifact", default=".spar/artifact.md",
        help="Artifact file path (default: .spar/artifact.md)",
    )
    return parser


def _build_orchestrator(args, config) -> Orchestrator:
    """Wire config + CLI args into a ready-to-run :class:`Orchestrator`.

    Kept deliberately thin: no business logic, only construction. Raises
    ``ValueError`` for a side that is not present in the loaded config (the
    caller turns that into a usage error).
    """
    sides = [s.strip() for s in args.sides.split(",") if s.strip()]
    order = [args.first] + [s for s in sides if s != args.first]

    cwd = Path.cwd()
    events_dir = Path(".spar/transcript")
    adapters: dict[str, object] = {}
    for name in order:
        side_cfg = config.sides.get(name)
        if side_cfg is None:
            raise ValueError(f"side {name!r} is not defined in the configuration")
        adapter_cls = _ADAPTERS[side_cfg.adapter]
        adapters[name] = adapter_cls(
            command=side_cfg.command,
            model=side_cfg.model,
            cwd=cwd,
            events_dir=events_dir,
            side_name=name,
        )

    debate = config.debate
    if args.max_rounds is not None:
        debate = DebateConfig(
            max_rounds=args.max_rounds,
            turn_timeout_sec=config.debate.turn_timeout_sec,
        )

    store = StateStore(Path(".spar"))
    artifact_path = Path(args.artifact)
    guard = Guard(repo_dir=cwd, artifact_path=artifact_path, spar_dir=Path(".spar"))
    return Orchestrator(
        sides=adapters,
        order=order,
        store=store,
        artifact_path=artifact_path,
        debate=debate,
        gate=ConsoleGate(),
        guard=guard,
    )


def main(argv=None) -> int:
    """Main entry point for spar-cli.

    Exit codes: 0 ok, 2 usage error, 3 lock/state, 4 protocol/guard abort,
    5 user abort.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.prompt is not None and args.cont:
        parser.error("prompt and --continue are mutually exclusive")
    if args.prompt is None and not args.cont:
        parser.error("either prompt or --continue is required")

    sides = [s.strip() for s in args.sides.split(",")]
    if args.first not in sides:
        parser.error(f"--first must be one of: {', '.join(sides)}")

    try:
        config = load_config(Path.cwd())
    except ConfigError as exc:
        sys.stderr.write(f"spar: configuration error: {exc}\n")
        return 2

    try:
        orchestrator = _build_orchestrator(args, config)
    except ValueError as exc:
        parser.error(str(exc))

    if args.cont:
        return orchestrator.run_continue()
    return orchestrator.run_new(args.prompt)


if __name__ == "__main__":
    sys.exit(main())
