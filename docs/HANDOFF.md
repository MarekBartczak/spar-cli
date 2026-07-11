# HANDOFF — live C++ execution test PASSED (2026-07-09)

**The v2 goal is achieved:** a full real-agent run `plan (debate, --tasks) →
implement → cross-review → per-task test → final Test → user-gated merge`
SUCCEEDED end-to-end on a C++ app in `/home/marek/P_PROJ/spar_tests`
(factorial CLI: 4 tasks, all merged, final test green, black-box suite 13/13,
merged into the target master as `b5e3850`).

## Files module tranche A (2026-07-11, `6f4ea8a..da3f87e`)

Per ADR 0006 and the plan (`docs/superpowers/plans/2026-07-11-files-tranche-a.md`):
the Pliki left-rail view replaces the disabled placeholder. Centre `QStackedWidget`
carries `Strumień` (live stream) and `Pliki` views, driven by two exclusive
toggles in `rails/centre_view` QSettings key; run-start and resume auto-switch to
Strumień via the `runner.started` signal. `FilesView` pairs a `QFileSystemModel`
tree (hides `.git`, shows `.spar` collapsed, hidden files visible) with a
tabbed `FileEditor` (gutter with line numbers, current-line highlight,
Pygments syntax-aware lexer selected by filename, Ctrl+S atomic save, dirty
marker, save-failure dialog). The read-only matrix, enforced via the same
`RunnerState` signal (RUNNING / GATE_PENDING / LOCKED), surfaces via a
"run w toku — tylko podgląd" banner and lock icons on tabs; `QFileSystemWatcher`
auto-reloads clean buffers when files change on disk, showing a "plik zmienił
się na dysku / Przeładuj" conflict banner when the user has local edits (no
silent clobber). `FileFinderOverlay` implements double-Shift WebStorm-style fuzzy
file lookup (in-memory index built once per session, subsequence-scoring algorithm
with path-position bonuses). Pre-spawn guard `_ensure_editors_clean()` added to
every execution path (new-debate, chat-handoff, exec, resume, and gate
accept/abort/extend/fix/remarks via a new `GatePanel.preflight_resume` hook)
to catch uncommitted edits before the engine runs. New dependency: `pygments`
in the `[gui]` extra. Test baseline 906 passed, 2 skipped — full baseline plus
the tranche's new tests, no failures/regressions. Deferred tranche B (find-in-files,
replace-in-files, Ctrl+F) and the git module. README screenshot TODO at
`docs/img/gui-files.png`.

## GrillPane implemented (2026-07-10, `ff65e18..f35fb8b`)

Per ADR 0004 and the plan (`docs/superpowers/plans/2026-07-10-grill-pane.md`,
13 challenge findings over 6 rounds, final round AGREE): `spar/gui/grill.py`
(`GrillSession` GUI-thread facade + `_GrillWorker` on a persistent `QThread`,
`OPENING_PROMPT_TEMPLATE`, block-based `parse_options`) and
`spar/gui/grill_dialog.py` (`GrillDialog` chat window) wired into the
new-debate dialog via a **"Grilluj z modelem…"** button
(`spar/gui/toolbar.py`). The grill drives a real `claude` session running
the user's grill-with-docs skill; finish is detected by a content-hash
comparison on `.spar/requirements.md`, whose content then pre-fills the
new-debate task field via "Użyj w debacie". Manual smoke test (live grill
session end-to-end in the running GUI) still pending.

## Where things are

- Repo: `github.com/MarekBartczak/spar-cli`, branch **master**, latest `442d5fc`.
- Suite: **311 passed, 2 skipped** (`python3 -m pytest -q`).
- venv with `spar` editable: `/home/marek/P_PROJ/ai_fight/.venv/bin/spar`.
- SDD ledger: `.superpowers/sdd/progress.md` (gitignored).
- Blocker A design + 6-round challenge history:
  `docs/superpowers/plans/2026-07-09-blocker-a-foreign-files.md`.

## What landed on 2026-07-09

- `8e71fd8` — five hardening fixes: `max_review_rounds` gate (accept/extend/
  abort), read-only reviewer adapters, absolute plan path in impl prompts,
  agent self-commit handling (scope + anti-spin), `max_fix_tasks` cap.
- `58c36d1` — **A1**: `--tasks` contract teaches isolation invariants
  (cross-reference rule → scaffold LAST; per-task test satisfiability;
  omitted `test=` semantics). Validated live: the debate produced core-first,
  Makefile-last with full deps and per-branch-satisfiable `test=` commands.
- `16a57c2` — **A2**: review prompt gets *foreign files* (unmerged tasks'
  globs; absence ≠ defect, hard-reference = plan-ordering MUST) + *merged
  files* (`gitops.present_files`, deletions excluded) + permanent
  missing-file rule. Validated live: no unsatisfiable MUSTs, every reviewer
  DONE'd on the merits.
- `442d5fc` — codex `--cd` double-resolution fix (relative worktree path +
  subprocess cwd → ENOENT), found live at t2.

## Known nits / follow-up backlog

Cleanup tranche 1 (2026-07-09, `44f6efc..9574c6e`) closed: graceful SIGINT
(exit 130 + `--continue` hint), per-side `impl_models` floor (config +
task-list validation + planner contract; set in spar_tests for claude),
open NICE remarks surfaced at the final-merge gate, omit-empty-remarks
protocol rule, numeric task-id ordering.

Cleanup tranche 2 (2026-07-09, `889b565..ea4eaef`) closed the rest:
`[execution] turn_timeout_sec`, target-checkout restore on abort/error exits
(best-effort, guard-tested), claude implementer with Bash/Grep/Glob (parity
with codex's shell; reviewer stays read-only), hedged foreign-section
reference, no-invented-remark-ids rule in the implementer protocol.

**Second live test (2026-07-09, brownfield) PASSED**: extended the existing
factorial app with `--table N` on top of the previous run's code (2 tasks,
plan → exec → merge `621c6d8`, black-box 24/24, old behavior byte-preserved).
Validated in battle: `impl_models` floor (no haiku in the plan), claude
implementer compiling its own work via Bash, scope guard catching the stray
build binary (`factorial`) from those compiles — rollback + retry, no churn.
Also fixed live: restore-helper noise when no state exists yet (`007863f`).

Small backlog from run 2:
- A finished run leaves `spar/integration` behind; the leftover guard then
  refuses the NEXT fresh run even though the branch is fully merged. Delete
  it at phase=done, or teach the guard to auto-clean a fully-merged
  integration branch.
- Build artifacts produced by implementer compile-checks trip the scope
  guard (costs one rollback+retry per task). Cheapest mitigation: gitignore
  the artifact in the project repo (e.g. `factorial` in spar_tests) — the
  scope guard already respects .gitignore. Engine change not obviously
  needed.

Next feature work comes from the roadmap below.

## Roadmap (PIVOTED 2026-07-09 — see docs/adr/0003)

spar becomes an **agent-operated engine**: the human grills requirements with
their host agent (Claude Code / Codex), the agent drives spar. Grill-in-spar
and TUI are DROPPED.

1. **Agent mode — IMPLEMENTED** (`1746825..24a6137`, plan
   `docs/superpowers/plans/2026-07-09-agent-mode.md`, 3 challenge rounds):
   `--headless` exit-and-resume gates (exit 10 + `pending_gate` in state,
   cleared only at consumption), `--gate accept|abort|extend:<n>|remarks:<f>`
   on `--continue`, `--task-file`, `spar status --json`, protocol in
   `docs/AGENT.md` + Claude Code skill in `skills/spar/SKILL.md`.
   **Task 6 DONE (2026-07-09): live headless smoke test PASSED** — full
   AGENT.md loop on spar_tests (--help feature, brownfield): --task-file
   debate → codex-hang recovery via --continue → consensus pend (10) →
   `--gate remarks:<file>` injected a test= requirement the debate then
   satisfied → accept → exec headless (2 tasks, scope guard caught build
   artifacts) → final_merge pend with summary in status --json → accept →
   done. Black-box 28/28.
   Backlog minors (final review, non-blocking): advanced-target headless
   merge-conflict pends mid-conflict and its resume path is untested (likely
   exit 4 — consider surfacing 4 directly); resume+failing-per-task-test
   combination uncovered; `_resume_review_task` rebuilds the worktree before
   the abort check (stray worktree on abort); `--gate` against a done state
   returns 0 (done short-circuit precedes validation).
2. 2-way concurrency (sequential-first by design; `docs/adr/0002`).

## spar gui (2026-07-10, `26541d7..bb7d379`)

PySide6 dashboard-pilot (mockup variant A; plan
`docs/superpowers/plans/2026-07-10-spar-gui.md`, 13 challenge findings over
6 rounds, final review 9/9 invariants PASS): optional `[gui]` extra,
`spar gui [--dir PATH]` — live stream pane (tailer over live.log, filters,
follow, search), task board, options-driven gate panel (consensus Accept
auto-starts exec; final_merge always manual; remarks textarea via
runner-owned temp file), toolbar lifecycle with a full exit-code state
machine (incl. LOCKED read-only on a foreign flock), Plan/Diff viewers
(branches from the new additive `status --json` `branches` field).
Engine untouched beyond that additive field. Suite 519 passed (GUI tests
skip without PySide6). **Manual smoke at the GUI pending (user-driven).**

## Fresh-project E2E PASSED (2026-07-10, spar_test_2)

Greenfield run from an EMPTY directory, fully GUI-piloted: create-repo
dialog (with .spar/ gitignored from the initial commit), starter config
auto-created, debate on opus + gpt-5.6-sol, exec with a LIVE
review_rounds gate (user extended +2 → converged), 3 tasks merged,
stats.py + 12/12 pytest, int/float output formatting byte-exact to spec.
The whole "project from zero" flow (repo guard → config bootstrap →
debate → exec → merge) is validated.

## External review received (2026-07-10)

`docs/reviews/2026-07-10-external-review.md` — independent assessment
(foundation ~8.5/10). Key accepted takeaways, in order: (1) formalize the
product boundary with a new ADR 0004 (GUI = dashboard-pilot; grill as an
optional module; amend ADR 0003) + doc-drift cleanup (PLAN.md historical,
roadmap split); (2) recovery tranche: interruption matrix (fault injection),
state↔git invariant validator, known HANDOFF minors; (3) review/test↔commit
fingerprint audit; (4) SECURITY_MODEL.md before any public alpha. Deferred
by design: CI (user decision), refactor of large modules (only opportunistic
extraction), token counters, concurrency.

## GUI-piloted full run PASSED + engine hardening (2026-07-10, `4884677..ae29cc1`)

Third live GUI session drove a complete run (--range feature, 3 tasks,
final merge, black-box 69/69) after two engine fixes found live:
- **Review dispute escalation** (`4884677` + `2b82941`): a justified
  rejection loop (reviewer re-raises, implementer re-rejects, no changes)
  now escalates to the review_rounds USER gate (accept-as-is/extend/abort)
  with the disputed remarks surfaced, instead of ReviewAbort exit 4. True
  no-op spin still aborts. Reviewer prompt: no verbatim re-raising of a
  rejected remark. (Live trigger: task text vs plan Decision contradiction —
  cross-review caught a real requirements conflict.)
- **`[execution] scope_ignore`** (`ae29cc1`): build-artifact patterns written
  to `.git/info/exclude` (local, uncommitted) so compiled binaries stop
  tripping the scope guard (live: `make test` → `factorial` binary → double
  violation → abort loop). Lab config sets factorial/*.o/build/.
- GUI display fix (`77494a1`): humanized debate prefixes show debate_model.

**Next (user-approved direction, analysis stage):** grill-with-docs INSIDE
the GUI as a chat pane over the existing claude adapter session loop
(question rendered, answer box, options as buttons; ends by writing
`.spar/requirements.md` → pre-fills Nowa debata). Embedded terminal deferred
(macOS requirement rules out XEmbed; pyte+pty spike only if the chat pane
proves insufficient). Also queued: token counters per vendor; abandoned-run
branch cleanup on Nowa debata.

## GUI smoke rounds 1-2 (2026-07-10, `414bdce..256e74d`)

Live-GUI feedback closed in two batches: engine cleans `spar/integration`
after done (unblocks Accept→auto-exec), dialog side-checkboxes + first-combo,
start notices in the stream + double-start guard, humanized prefixes with
models (`claude · sonnet · runda 1`), collapsible task.md panel, gate-panel
layout rework, strict filter-chip derivation, stream line-wrap + side-pane
min width, Side column with models, debate placeholder in the task board,
claude tool lines carry the target (`tool: Read src/main.cpp`), per-side
`debate_model` (planning on top models) + `review_models` floor (haiku banned
from reviewing in the lab config), README screenshots (offscreen script).

**Roadmap next (user-confirmed):** embedded terminal in the GUI (mockup
variant B exists) — then the grill_with_docs flow can run INSIDE spar gui:
grill the task in the embedded agent terminal → hand off to debate.
Also queued: token-usage counters per vendor (data present in both CLIs'
event streams; needs engine collection + status field + GUI tile).

## Live observability (2026-07-10, `94469a4..9b84ae2`)

Implemented and live-smoke-tested: adapters stream events live (claude
`stream-json --include-partial-messages`, codex JSONL; Popen + reader threads,
stderr drain, stdin semantics, timeout parity); `StreamSink` fans display
lines to stdout (default full; `--quiet` for agents) and an always-on
`.spar/live.log` with `[side task role]` prefixes; `spar watch` (colorized
follower + gate banner) and `spar ui` (viewer-window spawn cascade);
AGENT.md/SKILL.md updated (agent runs `spar ui` once, then `--quiet`).
Smoke (--version feature, Warp split with watch): full two-vendor stream
visible live, agent context stayed at spar-log level, claude transcripts now
raw JSONL, gates relayed normally. Live finding fixed same-day: review-turn
prefix named the task owner's side instead of the reviewer's (`9b84ae2`).
Known v1 trade-offs: each CLI invocation truncates live.log; codex `exec:`
display shapes inferred (display-only); Warp auto-spawn falls back to a
printed instruction.


## Stalled per-task-test loop → user gate (2026-07-11, `b001147`)
Live finding (spar_test_2 t1): plan generated `test=python -m py_compile` but
system only has `python3` → exit 127 forever; implementer correctly answered
"Unchanged", reviewer DONE'd, and `_test_and_merge_task` looped test→implement
with NO cap. Fix: no-progress guard mirroring the review anti-spin — a failing
iteration counts as progress only if the implementer made changes or the task
branch tip moved; after 2 consecutive no-change failing iterations the run
escalates to the existing review_rounds user gate (accept = merge DESPITE the
failing test, loud log; extend:N = fresh budget; abort = exit 5). Gate context
carries the failing test output (truncated 2000 chars) interactively and in
the headless pending-gate. Suite 699 passed. The stuck run was unblocked by
rewriting `python `→`python3 ` in `.spar/exec.json` task tests + artifact.md.
Backlog minor: headless resume-accept on THIS stall pends via task status
`review`, so accept re-runs the (still failing) test instead of merging on
override — full override needs a small ExecState flag.

## Broken-test-command resilience (2026-07-11, `651d2b9..5f51a72`)
Follow-up to the python/python3 incident — three layers, all user-approved:
(1) `spar/envprobe.py` probes local tooling (shutil.which + versions) and the
report is injected into the debate `## Tasks` contract with an explicit "only
use available tools" instruction (`651d2b9`); (2) mid-run: test exit 126/127
escalates IMMEDIATELY to the user gate (no wasted implementer turns) and the
gate family gained a `fix:<command>` decision — console, `--gate fix:...`
(first-colon split), and a GUI "Popraw komendę…" QInputDialog button
(`24d063c`); (3) fresh-start preflight validates the first token of every
task `test=` via shutil.which before ANY git side effect, exit 2, with a
python→python3 suggestion; skips `$(`/backtick/`$` commands rather than guess
(`c68a1a5`). Opus cross-review of the three commits found 2 CONFIRMED
defects, both fixed: headless resume `accept` on a test escalation re-ran the
failing test and re-pended forever — pend reason now persisted in the
pending-gate context, resume-accept merges on override, extend:N re-enters
the test loop (`f77410d`); preflight false-positived on POSIX builtins like
`. venv/bin/activate && …` — allowlist widened (`5f51a72`). Suite 750 passed.

## Orchestrator chat + tool-window rails (2026-07-11, `c3a7710..9428be8`)
ADR 0005 implemented across six commits. (1) `GrillSession` refactored into a
shared `ConversationSession` (adapter-backed multi-turn chat: resume, option
parsing, generation-token signal suppression, session-lost recovery,
abandoned-thread retention); grill is a thin subclass and its suite stayed
green untouched (`c3a7710`). (2) Icon rails on both window edges with the
collapse state machine: right rail Taski/Czat plus a Bramka icon with a
painted attention dot that force-opens Taski while a gate pends (never
resolving/hiding it); left rail is a disabled Pliki placeholder; collapse
state in QSettings, all-collapsed → full-width stream (`a4da5de`). (3) The
docked orchestrator chat — a read-only advisor with bubbles, dim `tool:`
lines, lettered-option buttons, free-text, and a "run w toku — tylko odczyt"
banner for RUNNING/LOCKED (`4f1c0be`, null-session-id hardening `706f021`).
(4) Session persistence in `.spar/chat.json` with resume on restart,
corrupt/missing → fresh, loss → banner + fresh next send (`ff277cc`).
(5) Silent pending-gate context injection (full payload fingerprint, once
per gate, re-injects on changed output) so the advisor can answer "co byś
wybrał" (`a5b5637`). (6) ` ```zadanie ` task-draft handoff: green "Nowa
debata z tym szkicem" button, engine-free gating via the toolbar signal,
prefilled NewDebateDialog (`9428be8`). Read-only boundary: the chat adapter
is constructed with `readonly=True` (allowedTools = Read only), the opening
prompt states the read-only contract, and chat exposes NO gate actions —
GatePanel remains the only decision pilot. Deferred (ADR 0005 consequences):
left-rail Pliki/editor and git tranches. Suite: 823 passed, 2 skipped.

## Orchestrator chat — live smoke PASSED (2026-07-11, `2316211..6fe69ae`)
Post-merge smoke found and fixed: (1) no height adjustment between Taski and
chat → vertical QSplitter with a visible 6px handle, sizes in QSettings
`rails/right_split`; (2) opening prompt produced grill-style A/B/C menus and
proactive repo analysis on a bare greeting → prompt rewritten (natural
conversation, options only for genuine choices, no unprompted tools/analysis);
(3) old persisted sessions kept the old prompt forever → "Wyczyść" button
(fresh session on demand) + prompt-hash invalidation in chat.json (any
OPENING_PROMPT change auto-invalidates persisted sessions). Suite 847 passed.
Final smoke: greeting → one line; "czym się zajmujesz?" → concise answer, no
tools; config question → read-only Bash/Read then accurate answer incl. the
empty test_command catch. User verdict: "świetnie wygląda ok".
Remaining: README screenshot TODO (docs/img/gui-chat.png) after user captures.
