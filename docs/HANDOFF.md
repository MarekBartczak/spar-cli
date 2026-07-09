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

Still open:
- Final-merge-gate abort leaves the repo checked out on `spar/integration`.
- Turn timeout hardcoded (`_DEFAULT_TIMEOUT_SEC = 900`), not in config.
- Claude implementer has no Bash/Grep/Glob (cannot compile-check own work
  before the per-task test).
- Permanent missing-file rule mentions the foreign section even when absent
  (benign prompt-coherence nit).
- Implementer sometimes "resolves" phantom remark ids (t3: #7/#9) — ignored
  correctly, harmless.

## Roadmap (unchanged)

1. **Grill phase**: one model interrogates the user → refined requirements →
   feeds the debate. Full vision: assumptions → grill → plan(2 models) →
   exec(2 models) → output.
2. **TUI** front-end.
3. 2-way concurrency (sequential-first by design; `docs/adr/0002`).
