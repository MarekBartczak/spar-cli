# HANDOFF — finish the live C++ execution test (2026-07-08 → next session)

**Goal for next session:** get a full real-agent run `plan → implement → cross-review → per-task test → compile → merge` to SUCCEED on a simple C++ app. Two design gaps (A, B below) block it today; fix them, then re-run the test.

## Where things are

- Repo: `github.com/MarekBartczak/spar-cli`, branch **master** (everything below is merged + pushed).
- Latest: `7a73e1d`. Suite: **285 passed, 2 skipped** (`python3 -m pytest -q`). The 2 skips are opt-in real-CLI contract tests.
- venv with `spar` installed editable: `/home/marek/P_PROJ/ai_fight/.venv/bin/spar` (picks up source edits live).
- SDD progress ledger (full task-by-task history of the v2 build): `.superpowers/sdd/progress.md` (gitignored).
- Read these memories first (auto-loaded via MEMORY.md): `weak-models-hallucinate-in-exec`, `exec-review-open-design-gaps`, `spar-is-grill-challenge-productized`, `no-co-authored-by-trailer`.

## What already works (validated live this session)

- **Debate (v1)** to DONE consensus; **`--tasks`** bridge makes the debate emit a parseable `## Tasks` section (gated on parse validity).
- **Execution engine (v2)**: Task FSM (implement → asymmetric cross-review → per-task test → merge to `spar/integration`), final Test phase, fix-Task, user-gated final merge, recovery (`--continue`).
- Guards proven live: empty-implementation abort, anti-spin (no-change), collision-leftover refusal, weak-model hallucination → loud abort, verdict `#N accepted: <note>` tolerated.
- **Model fit:** claude **sonnet** implements files reliably; **haiku hallucinates "done" without writing** — do not assign haiku to implementation tasks.

## The TWO blockers to fix (then the test passes)

### A. Per-task isolation breaks for interdependent build/scaffold tasks
Each task is reviewed/tested on its own branch off `spar/integration` (only earlier-merged deps present). The planned `CMakeLists.txt` task (t1, `deps=-`) references `src/*.cpp` / `tests/*.cpp` created by other tasks — absent on its branch — so the reviewer raises an unsatisfiable MUST ("sources missing, configure fails") and no per-task build can pass.
**Fix directions (pick one, brainstorm→plan→challenge first):**
- planning guidance: a build-config task must `deps=` its source tasks (or be merged with them); OR
- per-task test for a scaffold task = "my file parses/lints", not "the whole project builds"; OR
- skip per-task tests for scaffold tasks; rely on the final Test phase for the whole build.
Files: `spar/orchestrator.py` (debate `--tasks` prompt / task-split guidance), `spar/exec/loop.py` (per-task test policy), `spar/exec/tasklist.py`.

### B. No round cap on a non-converging review (churn)
`run_cross_review`'s anti-spin only catches NO-change turns. If the implementer keeps making real edits but the reviewer never DONEs (e.g. the unsatisfiable MUST from A), the loop churns indefinitely — no review-round budget → user escalation (unlike the debate's rounds-exhausted gate).
**Fix:** add a review-round cap (config, e.g. `[execution] max_review_rounds`, already parsed) → on exceed, escalate to the user gate (accept-as-is / extend +N / abort), mirroring `_handle_rounds_exhausted`. Files: `spar/exec/review.py` (`run_cross_review`), `spar/exec/loop.py` (gate wiring), `spar/config.py` (`max_review_rounds` exists), reuse `ExecGate`/`GateDecision`.

Suggested order: **B first** (mechanical, bounds the churn so runs fail fast at a user gate), then **A** (design: decomposition/test policy).

## Test harness (ready to use)

Test repo: **`/home/marek/P_PROJ/spar_tests`** (git repo, `.spar/` gitignored).
- `.spar/config.toml`: model catalogs (`claude`: opus/sonnet/haiku, default sonnet; `codex`: gpt-5.5/gpt-5.4, default gpt-5.5) + `[execution] test_command = "g++ -std=c++17 *.cpp -o /tmp/spar_cpp_app 2>&1 && /tmp/spar_cpp_app"`. **Adjust model names to what the local CLIs accept.**
- `.spar/artifact.md`: an agreed plan with a `## Tasks` section already exists (factorial CLI, 4 tasks; impl models bumped off haiku). You can reuse it (skip re-running the debate) or regenerate.

**Reset exec state (keep the plan):**
```bash
cd /home/marek/P_PROJ/spar_tests && git checkout master 2>/dev/null; git worktree prune; for b in $(git branch --format='%(refname:short)' | grep '^spar/'); do git branch -D "$b"; done; rm -f CMakeLists.txt *.cpp; rm -rf src tests build .spar/exec.json .spar/worktrees .spar/lock
```
**Regenerate the plan from scratch (optional):**
```bash
cd /home/marek/P_PROJ/spar_tests && /home/marek/P_PROJ/ai_fight/.venv/bin/spar "Napisz mały program C++: CLI liczący silnię liczby z argv, z walidacją i obsługą błędu. Zakończ plan sekcją ## Tasks dzielącą pracę między strony." --sides claude,codex --first claude --tasks
```
**Run execution:**
```bash
cd /home/marek/P_PROJ/spar_tests && /home/marek/P_PROJ/ai_fight/.venv/bin/spar exec
```
Gates are interactive (consensus `a`; final merge `a`). Ctrl+C saves state; resume with `--continue`. NOTE: exec has no graceful SIGINT handler yet (raw traceback on Ctrl+C, but state IS saved) — a nice-to-have follow-up.

## Method reminder (this project's rigor)
Design change → brainstorm/`grill-with-docs` → plan (`writing-plans`, model per task) → `challenge` (adversarial review by codex) → subagent-driven-development (fresh subagent per task, Opus reviews the hard ones). Commits: NO `Co-Authored-By`/AI-attribution trailer (hard rule). Default branch is **master** (oldschool). Merge flow: test → merge to master → push (no PR).

## Roadmap after the C++ test passes
1. **Grill phase** (deferred): 1 model interrogates the user (grill-with-docs flow) → refined requirements → feeds the debate. The full vision: assumptions → grill → plan(2 models) → exec(2 models) → output.
2. **TUI** front-end.
3. 2-way concurrency (execution is sequential-first by design; see `docs/adr/0002`).
