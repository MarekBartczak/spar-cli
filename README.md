# spar

Adversarial debate between two AI CLIs (Claude Code, Codex) over a shared artifact until per-hash consensus. The artifact — a plan, spec, review, or any text — is edited by both sides in alternating turns until they agree on the same version. You are the final arbiter.

Born from a manual workflow where two coding assistants iteratively review and improve each other's work. The protocol is in [PLAN.md](PLAN.md).

## Install

Not yet published to PyPI. For now:

```bash
pip install -e .
# or with uv:
uv tool install -e .
```

Requires `claude` and `codex` CLIs installed and authenticated:

```bash
# Install Claude Code (https://docs.anthropic.com/claude-code/install-claude-code)
# and Codex (https://docs.anthropic.com/codex/install)
# Then verify they work:
claude -p "Hello"
codex exec "Hello"
```

## Usage

Start a new debate with a task prompt:

```bash
spar "Plan a migration to OAuth2" \
  --sides claude,codex \
  --first claude \
  --max-rounds 6 \
  --artifact .spar/artifact.md
```

Resume an interrupted debate:

```bash
spar --continue
```

### What happens during a debate

1. The first side creates an initial version of the artifact.
2. The second side reads the artifact, edits it, responds to remarks, adds their own, and ends with a verdict.
3. Sides alternate until consensus (both agree on the same artifact hash) or the round budget is exhausted.
4. **User gate**: when consensus is reached or rounds exhausted, you decide: accept, request changes, extend rounds, or abort.
5. Output: `.spar/artifact.md` (the agreed-upon result) + full debate history in `.spar/transcript/`.

### Options

- `--sides` (default: `claude,codex`): comma-separated list of sides
- `--first` (default: `claude`): which side goes first
- `--max-rounds` (default: configured in `~/.config/spar/config.toml`): override max rounds
- `--artifact` (default: `.spar/artifact.md`): path to the artifact file
- `--continue`: resume an interrupted debate (requires a `.spar/session.json`)

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Debate ended with consensus and user accepted the result |
| 2 | Usage error (missing prompt, invalid flags, configuration error) |
| 3 | State/lock error (another debate running, corrupted `.spar/session.json`) |
| 4 | Protocol abort (guard rejected out-of-contract changes, verdict parsing failed, adapter error) |
| 5 | User abort (Ctrl+C during debate, user rejected result at gate) |

## How it works

- **Artifact is the single source of truth**: both sides edit `.spar/artifact.md` directly. The orchestrator does not hold a copy in memory; it reads the file each turn.
- **Structured verdicts**: each side ends their reply with a `<verdict>` block that lists resolved remarks (accepted/rejected) and new concerns. The orchestrator parses only this block; the rest is logged.
- **Consensus = hash agreement**: consensus is reached when both sides give `AGREE` verdicts on the same artifact hash. If one side edits the artifact, that hash changes, and the other side must review and agree to it.
- **Guard prevents out-of-contract changes**: the guard ensures each side edits only `.spar/artifact.md` and nothing else. If a side tries to modify other files or delete more than 60% of the artifact without justification, the turn is rejected and the side retries. Changes outside the artifact are rolled back.
- **State is fully resumable**: all debate state (rounds, verdicts, remarks, session IDs, artifact hash) is stored in `.spar/session.json` with atomic writes and a single-instance lock. Interrupted debates can be resumed with `--continue`.

## Development

Run the full test suite:

```bash
python3 -m pytest
```

Opt-in real-CLI contract tests (verifies claude/codex flag stability):

```bash
SPAR_CONTRACT_TESTS=1 python3 -m pytest tests/test_contract_real_cli.py
```

### Project layout

- `spar/cli.py` — command-line interface (argparse, entry point)
- `spar/orchestrator.py` — debate loop, consensus logic, user gate, turn prompts
- `spar/guard.py` — artifact contract enforcement, rollback on violations
- `spar/adapters/base.py` — adapter protocol (TurnResult, Adapter)
- `spar/adapters/claude.py` — Claude Code CLI adapter
- `spar/adapters/codex.py` — Codex CLI adapter
- `spar/verdict.py` — parser for `<verdict>` blocks
- `spar/state.py` — session.json persistence, flock single-instance lock, recovery
- `spar/config.py` — TOML configuration (global + project-level)

## License

MIT
