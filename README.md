# spar

Two-vendor AI engineering engine: **Claude Code and Codex debate a plan to
consensus, then implement it task-by-task with cross-vendor code review and
objective test gates.** One side implements, the other reviews — always the
opposite vendor — so every plan, every task, and every diff gets a genuinely
independent second opinion. You (or your host agent) arbitrate at a few
well-defined gates.

Born from a manual workflow where two coding assistants iteratively review
and improve each other's work. The protocol details are in [PLAN.md](PLAN.md);
the agent-facing contract is in [docs/AGENT.md](docs/AGENT.md).

## The pipeline

```
requirements ──► spar (debate, --tasks) ──► agreed Plan + ## Tasks
                                                 │
                                     spar exec   ▼
      ┌──────────── per task: implement (side A) → cross-review (side B)
      │                        → per-task test → merge to integration ──┐
      │                                                                 │
      └── next task ◄───────────────────────────────────────────────────┘
                                                 │ all merged
                                                 ▼
                              final Test → user gate → merge to your branch
```

- **Debate**: both sides edit one artifact in alternating turns until they
  agree on the same version (hash consensus). With `--tasks` the plan must
  end with a machine-parsable `## Tasks` section — each task carries a side,
  an implementation model, a reviewer model, dependencies, a file scope, and
  a per-task test command.
- **Execution**: each task runs on its own branch + worktree; the implementing
  side writes code, the opposite side reviews the diff (read-only) until DONE;
  the task's test gates its merge into an integration branch. A final test
  over the whole integration gates the user-approved merge into your branch.
- **Guards everywhere**: file-scope enforcement with rollback, anti-spin and
  empty-implementation aborts, review-round and fix-task budgets, per-side
  implementation-model floors, crash-safe resumable state.

## Install

Not yet published to PyPI. For now:

```bash
pip install -e .
# or with uv:
uv tool install -e .
```

Requires `claude` and `codex` CLIs installed and authenticated:

```bash
claude -p "Hello"
codex exec "Hello"
```

## Quick start

```bash
# 1. Debate a plan with a task list
spar "Build a small CLI that ..." --sides claude,codex --first claude --tasks

# 2. Accept at the consensus gate, then execute the plan
spar exec

# 3. Approve the final merge when the integrated result passes its tests
```

Interrupted at any point? `spar --continue` (debate) or
`spar exec --continue` (execution) resumes from persisted state.

## Live output (watching the models work)

Everything both models say — full text, tool calls, executed commands —
streams live, prefixed `[side task role]` (e.g. `[claude t1 impl]`,
`[codex r0]`):

- **Running spar yourself?** The full stream is on stdout by default —
  nothing to set up.
- **An agent is driving spar?** The agent passes `--quiet` (only spar's own
  protocol lines reach its context), while the full stream ALWAYS lands in
  **`.spar/live.log`**. Watch it from a second terminal/split:

  ```bash
  spar watch               # colorized live viewer (gate banners, per-side colors)
  spar watch --from-start  # include what already happened this invocation
  ```

  Or let spar open the viewer window for you:

  ```bash
  spar ui   # tmux split / terminal window with `spar watch`; prints the
            # manual instruction when no known terminal is available
  ```

Notes: each spar invocation truncates `live.log` (fresh view per command);
the raw, complete event streams are always persisted per turn in
`.spar/transcript/` (claude: JSONL stream events; codex: JSONL).

### `spar gui` (dashboard-pilot)

For driving spar yourself with a GUI instead of a terminal:

```bash
pip install -e ".[gui]"   # the [gui] extra (PySide6 + pygments)
spar gui                  # operates on the current directory
spar gui --dir PATH       # operate on a different project directory
```

It shows a live stream pane (the same feed as `spar watch`), a task board,
a gate panel that lights up with the right buttons for whichever gate is
pending (consensus `Accept` auto-starts execution; `final_merge` always
requires an explicit manual confirmation; test escalations add a
**Popraw komendę…** button — the GUI form of the `fix:<command>` gate
decision, replacing a broken per-task test command and re-running it),
a toolbar for the run lifecycle
(New debate / Start exec / Resume / Stop), and Plan/Diff viewers. It is a
**solo pilot**: one person clicks through gates for one run, same as
running spar interactively in a terminal — it does not add multi-user or
remote-control capability.

**When an agent is driving spar, use the GUI for observation only** —
leave gate decisions to the agent. Running the GUI's own gate buttons
concurrently with an agent-driven headless run races against the agent's
`--gate` resumes and corrupts whose decision actually lands; if you open
the GUI on a directory another process already holds the run lock for, it
shows a read-only "locked" banner instead of live controls.

![spar gui — execution](docs/img/gui-exec.png)
*A real run: live transcript (colored per side/model/task/role — reviewer
gpt-5.6-sol cross-checking claude's work, verdicts, gate lines) and the task
board tracking each task's merged/review/pending status.*

![spar gui — new debate](docs/img/gui-new-debate.png)
*Starting a run: task description, configured sides as checkboxes, first
speaker, and the `## Tasks` requirement toggle.*

The new-debate dialog also lets you sharpen a rough task draft with the
model before starting the run: a **"Grilluj z modelem…"** button opens a
chat dialog that drives a real `claude` session running the user's
grill-with-docs skill — questions arrive one at a time with lettered
options rendered as buttons (free-text answers also work); the session
ends when the model writes `.spar/requirements.md`, and its content
pre-fills the task field back in the new-debate form. Flow: draft task →
"Grilluj z modelem…" → chat Q&A (option buttons / free text) → model
writes `.spar/requirements.md` → task field pre-filled → debate.
Prerequisite: the user's `claude` CLI with a grill-with-docs skill
installed (`~/.claude/skills`); the grill runs on the claude side's
`debate_model`.

The main window has vertical **icon rails** on both edges (JetBrains-style
tool windows). The right rail toggles the **Taski** panel (task board +
gate) and the **Czat** panel, and has a **Bramka** icon that lights up with
an attention dot while a gate is pending and force-opens the Taski panel —
collapsing the panel never discards the pending decision. The left rail
carries two exclusive view toggles — **Strumień** (▤, the live stream) and
**Pliki** (🗀, a file browser + editor). Exactly one is active; starting or
resuming a run (and the consensus auto-exec chain) auto-switches back to
Strumień. A pending gate does not change the view. The active view persists
across restarts (QSettings).

**Pliki view.** A project tree (left) beside a tabbed editor (right).
Double-click a file to open it in a tab; re-opening focuses the existing
tab. The editor has line numbers, current-line highlight and Pygments
syntax colouring (lexer picked by filename; unknown types show as plain
text). Ctrl+S saves; closing a tab, switching away, or closing the window
with unsaved changes prompts save/discard/cancel. While a run is live
(RUNNING / gate pending / locked) the editor is **read-only** — a
"run w toku — tylko podgląd" banner shows, tabs carry a 🔒, and files the
engine rewrites on disk auto-reload when you have no local edits (a
"plik zmienił się na dysku" banner with **Przeładuj** appears instead when
you do, so nothing is silently clobbered).

**Double Shift** opens a fuzzy file finder overlay — type part of a path,
Enter (or double-click) opens it in the Pliki view, Esc closes.

**Find in files (Ctrl+Shift+F).** Opens a floating **Szukaj w plikach**
window (non-modal, resizable — its size and position are remembered):
type a query, toggle **Aa** (case), **.*** (regex) or **W** (whole word);
results group as file → matching lines with a per-file count, and clicking
a line opens the file at that match and dismisses the window. A second
Ctrl+Shift+F brings the window back to front; **Esc** (or the window's
close button) closes it — reopening restores the last query and results.
Search runs off the UI
thread and a new query cancels the previous one (ripgrep, when on PATH,
accelerates only case-sensitive literal non-whole-word searches; every
other search — case-insensitive, whole-word or regex — uses the built-in
Python scan). **Replace in files:** fill the *Zamień na…* field, keep the
files you want checked (all checked by default) and press **Zamień
zaznaczone**. A checked file is skipped and reported (`pominięto N`) when
it has unsaved edits in an open tab (niezapisane zmiany), changed on disk
since the search (plik zmienił się), is not valid UTF-8 (nie-UTF-8), is a
symlink pointing outside the project (dowiązanie poza projektem), or its
write fails (błąd zapisu); every other checked file is rewritten on disk
and any open clean tab auto-reloads. Replace is disabled while a run is
live (read-only matrix); search stays available.

**Find in the editor (Ctrl+F).** Opens a find/replace bar in the current
tab, prefilled with the selection: **F3 / Shift+F3** jump to the next/previous
match (wrapping around), all matches are highlighted, and **Zamień** /
**Zamień wszystko** replace (disabled while the editor is read-only). **Esc**
closes the bar.

![spar gui — widok Pliki z edytorem, taskami i czatem orkiestratora](docs/img/gui-files.png)

Docked under the task board is the **orchestrator chat** — a persistent,
**read-only advisor** (chat bubbles, lettered options as buttons, free-text
always available). It reads the repo and `.spar/` state but never edits
files, never holds the run lock, and **never makes gate decisions** — the
gate panel stays the only pilot. The conversation persists across GUI
restarts via `.spar/chat.json`; the **Wyczyść** button in the chat header
drops it on demand and the next message starts a fresh session (a persisted
session is also invalidated automatically when the built-in opening prompt
changes between versions). During a live run the chat shows a
"run w toku — tylko odczyt" banner and stays available for questions;
while a gate pends, the chat silently receives the gate context, so you
can ask e.g. "co byś wybrał i dlaczego?". When a reply contains a task
draft in a ` ```zadanie ` fenced block, a green **"Nowa debata z tym
szkicem"** button appears (enabled when the engine is free) and opens the
new-debate form pre-filled with the draft. Flow:
`pytanie → czat (advisor) → szkic w bloku ```zadanie``` → „Nowa debata z tym szkicem" → prefilled formularz`.

(Czat orkiestratora widać w prawej kolumnie zrzutu widoku Pliki powyżej.)

## Agent mode (headless)

spar is designed to be **driven by a host agent** (Claude Code / Codex) — see
[docs/adr/0003](docs/adr/0003-spar-as-agent-operated-engine.md). You grill the
requirements with your agent in conversation; the agent runs spar and relays
the gates. A ready-made Claude Code skill lives in
[skills/spar/SKILL.md](skills/spar/SKILL.md).

```bash
spar ui                                                       # open the live viewer for the human (once)
spar --task-file requirements.md --tasks --headless --quiet   # exit 10 = gate pending
spar status --json                                            # which gate, what options
spar --continue --headless --quiet --gate remarks:notes.md    # inject remarks into the debate
spar --continue --headless --quiet --gate accept              # accept the consensus

spar exec --headless --quiet                                  # gates pend the same way
spar exec --continue --headless --quiet --gate accept         # e.g. approve the final merge
```

Every interactive gate becomes: persist state → exit `10` → decision returns
via `--gate accept | abort | extend:<n> | remarks:<file> | fix:<command>` on
resume. Without `--headless` all gates stay interactive on stdin.

`fix:<command>` applies only at a per-task **test** gate (a review-round or
stalled test that surfaced a failing test command): it replaces that task's
`test` command and re-runs it immediately. A test command that exits `126`
(not executable) or `127` (command not found) is treated as a broken command,
not a code failure — spar escalates to this gate at once instead of burning
re-implement turns, and the message names the offending command (e.g. a plan
generated for `python` on a `python3`-only host). The value is split on the
FIRST colon only, so the command may contain spaces and colons:
`--gate fix:python3 -m py_compile todo.py`.

A fresh `spar exec` additionally **preflights** every task's `test` command
before any work starts: the first shell token (after skipping `VAR=val`
assignments) must be a shell builtin or resolvable on `PATH`. Any missing tool
refuses the whole run with exit `2` — listing each offending task, its
command, and the missing token (with a `python` → `python3` hint) — before
the integration branch or any other git state is created. Commands using
shell substitution (`` $( ) ``, backticks) are skipped rather than guessed at,
and `--continue` never re-runs the preflight; both stay covered by the mid-run
126/127 gate above.

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | Success (consensus accepted / execution merged / already done) |
| 2 | Usage or configuration error (incl. a `--gate` that doesn't match the pending gate, or a preflight refusal: a task `test` command names a missing tool) |
| 3 | State/lock guard (another instance, dirty target, leftover artifacts) |
| 4 | Protocol abort (guard violation, unusable verdicts, adapter failure) |
| 5 | User abort at a gate |
| 10 | Gate pending (headless) — inspect `spar status --json`, resume with `--gate` |
| 130 | Interrupted (Ctrl+C) — state saved, resume with `--continue` |

## Configuration

Global `~/.config/spar/config.toml`, overridden by project `.spar/config.toml`:

```toml
[sides.claude]
models = ["opus", "sonnet", "haiku"]
default_model = "sonnet"
impl_models = ["opus", "sonnet"]   # floor: models allowed to IMPLEMENT tasks

[sides.codex]
models = ["gpt-5.5", "gpt-5.4"]
default_model = "gpt-5.5"

[debate]
max_rounds = 6
turn_timeout_sec = 900

[execution]
test_command = "make test"      # the final Test phase (and per-task fallback)
max_review_rounds = 3           # review-round budget before the user gate
max_fix_tasks = 2               # integration-fix budget before aborting
turn_timeout_sec = 900
```

Custom CLI binaries per side (e.g. wrappers): `spar -m claude -setCommand
claude-erli`, inspect with `spar --list-commands`.

## How it works

- **Artifact is the single source of truth**: both sides edit
  `.spar/artifact.md` directly; consensus is agreement on its hash.
- **Structured verdicts**: every turn ends with a parsed `<verdict>` block
  (status, resolved remarks, new remarks); prose is logged, never trusted.
- **Asymmetric cross-review**: in execution only the implementer edits (in an
  isolated worktree, write-enabled adapter); the reviewer reads the diff with
  a read-only adapter and blocks the merge with MUST remarks until satisfied.
  The reviewer is told which files belong to other, not-yet-merged tasks
  (foreign files) so isolation never produces false "missing file" blockers.
- **Objective gates**: per-task test commands and the final test command are
  hard exit-code gates, not model opinions.
- **Environment probe**: with `--tasks`, the debate prompt includes a
  once-per-run probe of the local tooling (`spar/envprobe.py`: python3, node,
  gcc, …), so planners only write `test=` commands that actually exist on the
  machine (e.g. `python3`, never `python`, when python is absent).
- **State is fully resumable**: `.spar/session.json` (debate) and
  `.spar/exec.json` (execution) with atomic writes, single-instance locks,
  and git-reconciling crash recovery.

## Development

```bash
python3 -m pytest                 # full suite
SPAR_CONTRACT_TESTS=1 python3 -m pytest tests/test_contract_real_cli.py  # real-CLI contract tests
```

### Project layout

- `spar/cli.py` — CLI entry point (`spar`, `spar exec`, `spar status`, `spar watch`, `spar ui`)
- `spar/stream.py` — StreamSink: stdout + always-on `.spar/live.log`, `--quiet`
- `spar/watch.py` / `spar/ui.py` — live viewer + viewer-window spawner
- `spar/orchestrator.py` — debate loop, consensus, gates, turn prompts
- `spar/exec/loop.py` — execution FSM: task branches, merges, final test
- `spar/exec/review.py` — asymmetric cross-review loop, scope guard
- `spar/exec/tasklist.py` — `## Tasks` parser + validation (deps, models, scopes)
- `spar/exec/prompts.py` — implementer/reviewer prompt builders
- `spar/exec/gitops.py` — thin git wrappers (branches, worktrees, diffs)
- `spar/exec/state.py` / `spar/state.py` — persistent state + locks + recovery
- `spar/gates.py`, `spar/headless.py`, `spar/exec/headless.py` — agent mode
- `spar/status.py` — `spar status --json`
- `spar/guard.py` — debate artifact contract enforcement
- `spar/adapters/` — Claude Code / Codex CLI adapters
- `spar/verdict.py` — `<verdict>` block parser
- `spar/config.py` — TOML configuration
- `docs/AGENT.md` — host-agent protocol; `skills/spar/` — Claude Code skill
- `docs/adr/` — architecture decisions; `CONTEXT.md` — domain glossary

## License

MIT
