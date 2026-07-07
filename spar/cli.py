"""Command-line interface for spar-cli."""

import argparse
import sys


def main(argv=None) -> int:
    """
    Main entry point for spar-cli.

    Args:
        argv: Command-line arguments (defaults to sys.argv[1:] if None)

    Returns:
        Exit code (0 for help, 2 for not implemented or error)
    """
    parser = argparse.ArgumentParser(
        prog="spar",
        description="CLI tool orchestrating debates between AI CLIs"
    )

    parser.add_argument(
        "prompt",
        nargs="?",
        help="Task description starting a new debate"
    )
    parser.add_argument(
        "--continue",
        dest="cont",
        action="store_true",
        help="Resume interrupted debate"
    )
    parser.add_argument(
        "--sides",
        default="claude,codex",
        help="Comma-separated list of sides (default: claude,codex)"
    )
    parser.add_argument(
        "--first",
        default="claude",
        help="Which side goes first (default: claude)"
    )
    parser.add_argument(
        "--max-rounds",
        type=int,
        default=6,
        help="Maximum number of rounds (default: 6)"
    )
    parser.add_argument(
        "--artifact",
        default=".spar/artifact.md",
        help="Artifact file path (default: .spar/artifact.md)"
    )

    args = parser.parse_args(argv)

    # Validation 1: prompt and --continue are mutually exclusive
    if args.prompt and args.cont:
        parser.error("prompt and --continue are mutually exclusive")

    # Validation 2: one of them is required
    if not args.prompt and not args.cont:
        parser.error("either prompt or --continue is required")

    # Validation 3: --first must be one of the --sides values
    sides = [s.strip() for s in args.sides.split(",")]
    if args.first not in sides:
        parser.error(f"--first must be one of: {', '.join(sides)}")

    # Valid invocation: print stub message and return 2
    sys.stderr.write("spar: not implemented yet\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
