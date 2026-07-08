# spar v2 — Execution mode (design)

Date: 2026-07-08
Status: Draft (pre-challenge)

Builds on v1 (debate engine): adapters (claude/codex), verdict protocol
(AGREE / CONTINUE / DONE), guard, `.spar/` state. Terminology: see `CONTEXT.md`.
Decisions of record: `docs/adr/0001`, `docs/adr/0002`.

## 1. Goal

After a Debate reaches consensus on a Plan, actually implement it: split the
Plan into Tasks, have the two Sides implement them (each in isolation),
cross-review each other's work, test, and land the result — arbitrated by the
user at the final merge.

## 2. Phases

Sequential, isolated at their boundaries (ADR 0001):

1. **Debate** (v1, extended) — produces the Plan, which now also contains an
   agreed, machine-parsable Task List (§4).
2. **Execution** — Sides implement Tasks through the Task FSM (§6).
3. **Test** — final comprehensive test run over the whole Integration branch
   (§7), gating the final merge (§9).

By default each Phase uses fresh Sessions (two per Phase, one per Side).
`--merge-sessions` (opt-in) makes each Side keep one Session across all Phases;
orthogonal to the pipeline (only Session lifetime changes). Sessions are a
token optimization only — every turn is reproducible from `.spar/` state.

## 3. Trigger

`spar exec` (run in the repo). Reads `.spar/artifact.md`, parses its `## Tasks`
section (produced by the Debate phase). No `## Tasks` → error: "run a debate to
consensus over the plan and its tasks first." `spar exec --continue` resumes an
interrupted Execution from `.spar/` state.

## 4. Task List (parallelism-aware)

The Debate's consensus covers the Plan **and** its Task breakdown — the Debate
ends (both DONE) only when both are agreed. The task split is negotiated as part
of the plan consensus (one Side proposes, the other accepts/amends via the
verdict protocol), not a separate round. The orchestrator parses a `## Tasks`
section; entries are closed mechanically like a verdict, not read from prose.

Each Task carries:
- **id** and description
- **Assignment**: implementing Side + implementation model; plus the **review
  model** the *other* Side runs to review it. Both models negotiated per Task
  from the two Model catalogs (a trivial Task may be reviewed by a mid model).
- **deps**: other Task ids that must be merged first.
- **file scope**: files/modules the Task may touch, so concurrent Tasks are kept
  file-disjoint (ADR 0002).

**Model catalog**: the models each Side can run, declared in config (not
discovered at runtime), injected into the planning prompt so Assignments span
both catalogs. Example:

```toml
[sides.claude]
models = ["opus", "sonnet", "haiku"]
[sides.codex]
models = ["gpt-5.5", "gpt-5.4"]

[execution]
test_command = "pytest -q"      # final Test phase; per-Task override in Task List
max_review_rounds = 0            # 0 = unlimited (loop to DONE); see §8
```

## 5. Isolation

- **Integration branch** `spar/integration` — created from the user's target
  branch; the single accumulator. Merged Tasks land here one at a time.
- **Task branch** `spar/t<id>-<side>` — short-lived, branched from the *current*
  Integration branch (so it already contains merged deps). Implemented,
  reviewed, tested here, then merged into Integration and deleted.
- **Worktree** `.spar/worktrees/<side>` — one physical git worktree per Side,
  reused sequentially across that Side's Tasks (a Side is one agent doing one
  Task at a time).

## 6. Task FSM

```
pending → ready → implementing → review → testing → merged
                       ↑            │         │
                       │  reviewer  │  test   │
                       └─ remarks ──┘  fail ──┘
```

- **pending**: created; not all deps merged.
- **ready**: all deps merged; may start.
- **implementing**: the implementing Side edits code on the Task branch (guard
  restricts edits to the Task's file scope, as v1's guard restricts the
  artifact).
- **review**: **Cross-review** — asymmetric. The reviewing Side (the one that
  did *not* implement) only judges: raises MUST/NICE remarks via a CONTINUE
  verdict. The implementer addresses them (edits code); the reviewer re-reviews.
  The loop ends when the reviewer emits DONE (no blocking remarks). Only the
  implementer edits code. Reuses the verdict protocol with a single editing Side.
- **testing**: the orchestrator runs the Task's test command (per-Task override
  or the configured default) in the Task branch. Exit code gates. Pass →
  merged; fail → implementing (loop).
- **merged**: Task branch merged into Integration, deleted; dependents may
  become ready.

Merge into Integration happens only after review DONE **and** per-Task tests
pass.

## 7. Testing

Two levels, both run by the orchestrator (objective exit-code gate, no agent
self-report):
- **Per-Task**: gates each Task's merge (§6 testing state).
- **Final Test phase**: after every Task is merged, run the full configured
  `test_command` over the whole Integration branch. Gates the final merge.

## 8. Failure handling

There is no "task failure" state. A test failure simply returns the Task to
implementing; a review loops until the reviewer is satisfied. Tasks iterate to
green.

Runaway protection, not failure:
- **Ctrl+C** — the human escape. Saves state; `spar exec --continue` resumes.
- **Per-turn timeout** (v1) — guards a single hung subprocess turn.
- **No loop caps by default.** An optional soft cap (config, default off) may
  pause and ask "N attempts — keep going?"; deferred (YAGNI) — a future TUI
  gives live monitoring/stop, and unattended headless loops are not a v2
  use-case.

## 9. Final merge

After the Test phase passes, the orchestrator presents a summary (Tasks,
diffstat, test result) and the user green-lights merging `spar/integration` into
the target branch. `--auto-integration-merge` skips the gate. This is the only
outward, user-gated action of Execution (mirrors v1's consensus-accept gate).

## 10. Concurrency (phasing — ADR 0002)

- **Planning is parallelism-aware now**: deps + file scope per Task.
- **Execution is sequential first** (one ready Task at a time: impl → review →
  test → merge → next). Reuses v1's blocking subprocess model; deterministic,
  testable. In this mode merges are trivial (each Task branches from the latest
  Integration), so merge conflicts do not arise.
- **2-way concurrency later**: a scheduler runs both Sides on independent ready
  Tasks at once, over the *same* Task graph. Adds a merge lock (serialized
  merges into Integration) and reviewer-busy sync points.
- **Merge-conflict policy** (concurrency phase only): before merging, rebase the
  Task branch onto the current Integration; clean → merge; conflict → an
  implementer turn resolves it; fallback → user gate. Designed with the
  concurrency phase, not the first slice.

## 11. State & persistence

Extends `.spar/` (atomic writes, resumable, single-writer flock as v1):
- `session.json` (or a sibling `exec.json`) records: current Phase, per-Task FSM
  state, Assignments, deps, per-Side per-Phase session ids, branch names,
  `turn_in_progress` for recovery.
- `transcript/` gains per-Task, per-turn records (impl + review turns).
- Exact schema to be fixed in the implementation plan; it mirrors v1's structural
  state (source of truth) rather than summarizing sessions.

## 12. CLI surface

- `spar exec` — start Execution from the consensus Plan's Task List.
- `spar exec --continue` — resume.
- `--merge-sessions` — one Session per Side across Phases (§2).
- `--auto-integration-merge` — skip the final merge gate (§9).
- Model catalogs and `test_command` from config (§4).

## 13. Out of scope / deferred

- 2-way concurrent execution and its merge-conflict policy (design captured §10;
  built after the sequential slice).
- `--merge-sessions` implementation (design fixed; may ship after default split).
- Soft loop caps (§8).
- TUI (separate future work).
- N-way fan-out (spar is two Sides, two processes; not Claude Code's internal
  subagents).

## 14. First implementation slice

Sequential Execution end-to-end on the parallelism-aware graph: parse Task List
→ Task FSM (implement → asymmetric cross-review to DONE → per-Task test → merge
to Integration) → final Test phase → user-gated final merge. Fresh sessions per
phase. No concurrency, no conflict handling (not needed sequentially). This
proves the core; concurrency layers on afterward.
