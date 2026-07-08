"""Command-line interface for spar-cli."""

import argparse
import sys
from pathlib import Path

from spar.adapters.claude import ClaudeAdapter
from spar.adapters.codex import CodexAdapter
from spar.config import ConfigError, DebateConfig, load_config, set_global_command
from spar.exec.loop import ConsoleExecGate, Executor
from spar.exec.state import ExecStateStore
from spar.exec.tasklist import TaskListError, parse_task_list
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
    parser.add_argument(
        "-m", "--adapter", choices=sorted(_ADAPTERS),
        help="Side whose CLI command to configure (used with -setCommand)",
    )
    parser.add_argument(
        "-setCommand", "--set-command", dest="set_command", metavar="COMMAND",
        help="Globally persist the CLI binary to run for the side given by -m "
             "(e.g. -m claude -setCommand claude-erli)",
    )
    parser.add_argument(
        "--list-commands", dest="list_commands", action="store_true",
        help="List the resolved CLI command for each configured side and exit",
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


def _build_exec_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spar exec",
        description="Run the execution engine over a consensus Plan's task list",
    )
    parser.add_argument(
        "--continue", dest="cont", action="store_true",
        help="Resume an interrupted execution",
    )
    parser.add_argument(
        "--merge-sessions", dest="merge_sessions", action="store_true",
        help="Reserved for a future release; does not change session "
             "lifetime yet (spec deferred)",
    )
    parser.add_argument(
        "--auto-integration-merge", dest="auto_integration_merge",
        action="store_true",
        help="Skip the interactive final-merge confirmation gate",
    )
    parser.add_argument(
        "--sides", default="claude,codex",
        help="Comma-separated list of sides (default: claude,codex)",
    )
    parser.add_argument(
        "--first", default="claude",
        help="Which side goes first (default: claude)",
    )
    return parser


def _build_executor(
    args, config, tasks, order: list[str], plan_path: Path
) -> Executor:
    """Wire config + CLI args into a ready-to-run :class:`Executor`.

    Kept deliberately thin (mirrors ``_build_orchestrator``): no business
    logic beyond adapter-factory construction, so it is easy for tests to
    monkeypatch wholesale.
    """

    def make_adapter(side: str, worktree: Path, model: str):
        side_cfg = config.sides[side]
        adapter_cls = _ADAPTERS[side_cfg.adapter]
        return adapter_cls(
            command=side_cfg.command,
            model=model,
            cwd=worktree,
            events_dir=Path(".spar/transcript"),
            side_name=side,
        )

    return Executor(
        repo=Path.cwd(),
        spar_dir=Path(".spar"),
        make_adapter=make_adapter,
        sides=config.sides,
        order=order,
        plan_path=plan_path,
        tasks=tasks,
        execution=config.execution,
        gate=ConsoleExecGate(),
        store=ExecStateStore(Path(".spar")),
        auto_integration_merge=args.auto_integration_merge,
    )


def _run_exec(argv) -> int:
    """Handler for the ``spar exec`` subcommand."""
    parser = _build_exec_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(Path.cwd())
    except ConfigError as exc:
        sys.stderr.write(f"spar: configuration error: {exc}\n")
        return 2

    sides = [s.strip() for s in args.sides.split(",") if s.strip()]
    order = [args.first] + [s for s in sides if s != args.first]

    plan_path = Path(".spar/artifact.md")
    if not plan_path.exists():
        sys.stderr.write(
            f"spar: no plan found at {plan_path}; run a debate to consensus "
            "over a plan before running 'spar exec'.\n"
        )
        return 2
    plan_text = plan_path.read_text(encoding="utf-8")

    try:
        tasks = parse_task_list(plan_text, sides=config.sides, order=order)
    except TaskListError as exc:
        sys.stderr.write(
            "spar: run a debate to consensus over the plan and its tasks "
            f"first: {exc}\n"
        )
        return 2

    try:
        executor = _build_executor(args, config, tasks, order, plan_path)
    except ValueError as exc:
        parser.error(str(exc))

    if args.cont:
        return executor.run_continue()
    return executor.run()


def _run_list_commands() -> int:
    """Print the resolved CLI command for each configured side."""
    try:
        config = load_config(Path.cwd())
    except ConfigError as exc:
        sys.stderr.write(f"spar: configuration error: {exc}\n")
        return 2
    for name, side in config.sides.items():
        print(f"{name}: {side.command} (adapter: {side.adapter})")
    return 0


def main(argv=None) -> int:
    """Main entry point for spar-cli.

    Exit codes: 0 ok, 2 usage error, 3 lock/state, 4 protocol/guard abort,
    5 user abort.
    """
    if argv is None:
        argv = sys.argv[1:]

    # Leading-token subcommand: ``spar exec ...`` routes to a dedicated
    # handler/parser over the remaining tokens, entirely separate from the
    # debate command below (which stays unchanged so `spar "<prompt>"`,
    # `--continue`, `-m/-setCommand`, `--list-commands` keep working).
    if argv and argv[0] == "exec":
        return _run_exec(argv[1:])

    parser = _build_parser()
    args = parser.parse_args(argv)

    # Management modes: configure/inspect side commands, then exit without a debate.
    if args.list_commands:
        return _run_list_commands()
    if args.set_command is not None:
        if args.adapter is None:
            parser.error("-setCommand requires -m/--adapter to name the side")
        try:
            path = set_global_command(args.adapter, args.set_command)
        except ConfigError as exc:
            parser.error(str(exc))
        print(
            f"spar: side '{args.adapter}' command set to "
            f"'{args.set_command.strip()}' in {path}"
        )
        return 0
    if args.adapter is not None:
        parser.error("-m/--adapter is only valid together with -setCommand")

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
