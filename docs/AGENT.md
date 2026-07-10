# AGENT.md — driving spar as a host agent

This is the protocol contract for a host agent (Claude Code, Codex CLI, or
any conversational agent) that operates `spar` on behalf of a human. Per
[ADR 0003](adr/0003-spar-as-agent-operated-engine.md), spar has no
interactive front-end of its own beyond a console fallback: the host agent
grills requirements, drives spar headlessly, relays every gate decision to
the human, and reports results. Terminology (Phase, Plan, Session, Side,
Task, Task List) follows [CONTEXT.md](../CONTEXT.md).

All commands below were verified against `spar --help`, `spar exec --help`,
`spar watch --help`, `spar ui --help`, and `spar status --json` output on
this version of spar-cli.

## Prerequisites

Before driving a run, confirm:

- `spar` resolves on `PATH` (or the project's venv `bin/spar`).
- `.spar/config.toml` exists in the target repo, or the global
  `~/.config/spar/config.toml` has both sides configured. `spar
  --list-commands` prints the resolved CLI command per side and exits 0;
  a `ConfigError` there means misconfiguration (exit 2) and must be fixed
  before starting a run.

## Live output

Model chatter is verbose, and a headless-driven agent should not have it
interleaved with its own reasoning. The pattern:

1. **Run `spar ui` once, at the start of the session**, before starting a
   debate or execution run. It best-effort spawns a terminal (a tmux split
   if already inside tmux, otherwise a detected terminal emulator) running
   `spar watch`, a live colorized tail of `.spar/live.log`. It always exits
   0 — if no terminal could be spawned it just prints a manual instruction
   (`Open a split/terminal and run: spar watch`) instead of failing.
   **Tell the human what the window is** (e.g. "I've opened a live view of
   spar's output in a new terminal split — you don't need to watch it, but
   it's there if you want to follow along").
2. **Always pass `--quiet` to every `spar`/`spar exec` invocation** you
   drive from here on. `--quiet` suppresses the verbose per-turn model
   output on *this* session's stdout; spar's own status/gate/error lines
   still print (you still need those to drive the loop), and
   `.spar/live.log` still gets everything regardless of `--quiet` — that's
   what the `spar watch` window is tailing.
3. `spar watch [--from-start]` can also be run standalone (e.g. by the
   human directly) — it tolerates `.spar/live.log` not existing yet (waits
   for it) and being truncated by a fresh run (reopens from 0), and exits
   cleanly (rc 0) on Ctrl+C.

Transcripts under `.spar/transcript/` remain the authoritative record of a
run; `live.log`/`spar watch` are a convenience live view only, not durable
(each CLI invocation, including a `--continue` resume, truncates
`live.log` fresh — see `spar/stream.py`).

## The two phases

1. **Debate** (`spar`, no subcommand) — two Sides argue over and edit
   `.spar/artifact.md` until Consensus. With `--tasks`, the agreed artifact
   must end in a machine-parsable `## Tasks` section — this is the bridge
   into Execution and MUST be passed for agent-driven runs.
2. **Execution** (`spar exec`) — Sides implement the agreed Plan's tasks,
   cross-review each other's work, run per-task and final tests, and merge
   into the caller's branch behind a user gate.

Both phases accept `--headless`: instead of blocking on stdin at a user
gate, the run persists the pending gate to state and exits **10**. The host
agent then reads `spar status --json`, decides (or relays the decision to
the human), and resumes with `--continue --gate <value>`.

## Commands

### `spar` (debate)

```
spar [prompt] --sides claude,codex --first claude --tasks --headless
spar --continue --headless --gate <value>
```

Flags relevant to agent mode (see `spar --help` for the full list):

- `prompt` — positional task description; mutually exclusive with
  `--task-file` and `--continue`.
- `--task-file PATH` — read the task prompt from a file instead of the
  positional argument (main debate command only; `spar exec` has no
  equivalent, it always reads the existing `.spar/artifact.md`).
- `--tasks` — require the agreed Plan to end with a `## Tasks` section.
  Off by default; always pass it when the goal is to reach `spar exec`.
- `--sides` (default `claude,codex`), `--first` (default `claude`).
- `--headless` — exit 10 with a pending gate instead of blocking on stdin.
- `--gate VALUE` — resolve a pending gate headlessly. Requires
  `--continue` **and** `--headless`. `VALUE` is one of `accept`, `abort`,
  `extend:<n>`, or `remarks:<file>` (file: one remark per non-empty line).
  A `VALUE` not in the pending gate's allowed options is a usage error
  (exit 2), e.g. passing `extend:2` to a gate that only accepts
  accept/abort.
- `--continue` — resume an interrupted debate.

### `spar exec` (execution)

```
spar exec --headless [--sides claude,codex --first claude]
spar exec --continue --headless --gate <value>
```

Same `--headless`/`--gate`/`--continue`/`--sides`/`--first` semantics as
above. `spar exec` has no `--task-file` or `--tasks` — it always parses the
task list out of the existing `.spar/artifact.md`, which must already carry
the `## Tasks` section from a debate run with `--tasks`. There is also
`--auto-integration-merge`, which skips the interactive `final_merge`
confirmation gate — do **not** use this from agent mode; drive
`final_merge` through the normal headless gate flow instead so the human
is always shown the merge summary before it lands (see Gate-relay
etiquette below). `--merge-sessions` is reserved for a future release and
has no effect yet.

### `spar status --json`

Read-only, side-effect-free projection of current state:

```json
{
  "phase": null,
  "pending_gate": null,
  "tasks": {},
  "artifact": null
}
```

This is the exact output in a directory with no `.spar/` state yet (every
field `None`/empty — not an error). Fields:

- `phase` — `null` (nothing has run), `"debate"`, or an execution phase
  string once `spar exec` has started. Exec state takes precedence over
  debate state once it exists.
- `pending_gate` — `null`, or `{"name": ..., "options": [...], "context": {...}}`
  describing the gate the last headless run stopped at. `name` is one of
  the gate names in the matrix below; `options` are the values valid for
  `--gate` right now; `context` carries gate-specific detail (e.g. open
  remarks, the final-merge summary).
- `tasks` — `{}` during debate; once execution starts, one entry per task
  id: `{"status": ..., "side": ..., "model": ...}`.
- `artifact` — path to `.spar/artifact.md` if it exists, else `null`.

Always call `spar status --json` immediately after any exit-10 run to read
`pending_gate` before deciding.

## Gate matrix

| Gate | Phase | Options | Notes |
|------|-------|---------|-------|
| `consensus` | debate | `accept` / `remarks:<file>` / `abort` | Both sides gave `AGREE` on the same artifact hash. `accept` ends the debate; `remarks:<file>` re-opens the loop with new `[USER]` remarks; `abort` ends it. |
| `rounds_exhausted` | debate | `accept` / `extend:<n>` / `abort` | Round budget hit with open remarks still unresolved. `accept` takes the artifact as-is; `extend:<n>` adds `n` more rounds; `abort` ends it. |
| `review_rounds` | execution | `accept` / `extend:<n>` / `abort` | A task's cross-review loop hit `max_review_rounds` without a `DONE` verdict. `accept` merges as-is; `extend:<n>` adds rounds; `abort` ends the run. |
| `final_merge` | execution | `accept` / `abort` | All tasks merged into the integration branch and the final test command passed; this is the last gate before merging into the caller's branch. `accept` performs the merge; `abort` leaves the integration branch untouched. |
| `recovery` | debate (internal) | n/a | Not a real user gate: on resume after an interrupted turn, headless mode always auto-repeats the turn (`recovery_gate` never pends). Nothing to relay here. |

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | Run completed: debate accepted, or execution merged and reported. |
| 2 | Config/usage error (bad flags, missing/invalid config, mismatched `--gate` for the pending gate, no plan/tasks found for `spar exec`). |
| 3 | Lock/state error (another instance running, missing or corrupted `.spar/session.json` or `.spar/exec` state, e.g. `--continue` with no prior run). |
| 4 | Protocol/adapter abort (guard rejected an out-of-contract change, verdict/task-list parsing failed, adapter/session error, merge conflict). |
| 5 | User abort (a gate was resolved with `--gate abort`). |
| 10 | Gate pending in headless mode — not a failure. Read `spar status --json`, decide, resume with `--continue --headless --gate <value>`. |
| 130 | Interrupted (SIGINT/Ctrl-C). Resume with `--continue`. |

Only 0 is unconditional success. 10 is the expected steady state of a
headless-driven run; 2/3/4/5/130 all need host-agent handling — see
Failure surfacing.

## Config keys worth knowing

`.spar/config.toml` (project) overrides `~/.config/spar/config.toml`
(global). Relevant keys for agent-driven setups:

```toml
[sides.claude]
impl_models = ["opus", "sonnet"]   # models allowed to IMPLEMENT tasks (never haiku: it fabricates "done" without writing)

[execution]
test_command = "..."        # final comprehensive test run gating the final_merge gate
max_review_rounds = 3       # cross-review rounds before the review_rounds gate fires
max_fix_tasks = 2           # integration-fix tasks allowed before an abort
turn_timeout_sec = 900      # per-turn timeout, execution phase
```

`[debate].turn_timeout_sec` is the equivalent knob for the debate phase.
Set `impl_models` per side to exclude weak/cheap models from implementation
— they are permitted in the model catalog for review but must never be
assigned to build code.

## The canonical driving loop

0. **Open the live view once, at session start:** `spar ui` (see Live
   output above) — tell the human what the window is. From here on, always
   pass `--quiet` to `spar`/`spar exec`.
1. **Grill requirements with the human.** Use your normal requirements
   process (e.g. a grilling skill) to turn a request into a concrete task
   description. Write it to a file, e.g. `requirements.md`.
2. **Start the debate:**
   ```bash
   spar --task-file requirements.md --sides claude,codex --first claude --tasks --headless --quiet
   ```
   This exits **10** at the first gate (`consensus` or `rounds_exhausted`).
   Read `spar status --json`, inspect `pending_gate`, decide or relay the
   decision to the human, then:
   ```bash
   spar --continue --headless --quiet --gate accept   # or remarks:<file> / abort
   ```
   Repeat until the debate phase exits 0 (Plan agreed) or a terminal
   non-zero code (see Failure surfacing).
3. **Start execution:**
   ```bash
   spar exec --headless --sides claude,codex --first claude --quiet
   ```
   On every exit **10**: `spar status --json` → decide/relay → resume:
   ```bash
   spar exec --continue --headless --quiet --gate accept   # or extend:<n> / abort
   ```
4. **On exit 0:** report the final-merge summary to the human (the gate's
   `context.summary` field, captured from the `pending_gate` context just
   before the accepted `final_merge`). **On exit 2/4/5:** surface the
   error verbatim to the human — never bury, retry-hide, or paraphrase away
   a failure. Exit 130 means the run was interrupted; resume with
   `--continue` (add `--headless` if driving unattended) rather than
   starting fresh.

## Gate-relay etiquette

- **`final_merge` always requires the human.** Regardless of any prior
  authorization, show the human the merge summary (test results, task
  list, open `[NICE]` backlog) and get explicit approval before issuing
  `--gate accept` on a `final_merge` gate. This is the point where code
  lands in the caller's branch — never auto-accept it.
- **`consensus` and `review_rounds` may be auto-decided only with prior
  human authorization** for this run (e.g. "auto-accept consensus and
  review rounds, but always ask me before the final merge"). Without that
  authorization, relay every gate to the human, including these.
- **Never invent a `--gate` value.** Only pass one of the options listed
  in `pending_gate.options`; anything else is a usage error (exit 2).
- **Failure surfacing is mandatory.** Any exit code other than 0 or 10 must
  be shown to the human verbatim (command run, exit code, stderr/summary),
  even mid-loop. Do not silently retry a 2/3/4/5 exit — those are
  substantive failures (config, state corruption, protocol/guard abort,
  explicit abort), not transient conditions.
