# HANDOFF — live C++ execution test PASSED (2026-07-09)

**The v2 goal is achieved:** a full real-agent run `plan (debate, --tasks) →
implement → cross-review → per-task test → final Test → user-gated merge`
SUCCEEDED end-to-end on a C++ app in `/home/marek/P_PROJ/spar_tests`
(factorial CLI: 4 tasks, all merged, final test green, black-box suite 13/13,
merged into the target master as `b5e3850`).

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
   **Remaining: Task 6 — live headless smoke test in spar_tests.**
   Backlog minors (final review, non-blocking): advanced-target headless
   merge-conflict pends mid-conflict and its resume path is untested (likely
   exit 4 — consider surfacing 4 directly); resume+failing-per-task-test
   combination uncovered; `_resume_review_task` rebuilds the worktree before
   the abort check (stray worktree on abort); `--gate` against a done state
   returns 0 (done short-circuit precedes validation).
2. 2-way concurrency (sequential-first by design; `docs/adr/0002`).
