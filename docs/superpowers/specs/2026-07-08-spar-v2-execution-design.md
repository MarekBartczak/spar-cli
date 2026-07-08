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

### 4.1 Task List grammar

The `## Tasks` section is a flat list, one Task per line, closed mechanically
(parse errors abort `spar exec` with the offending line). Grammar:

```
## Tasks
- [<id>] <description> | side=<side> | model=<impl-model> | review=<review-model> | deps=<id,id|-> | files=<glob,glob> [ | test=<cmd>]
```

Fields are `key=value` separated by ` | `. All are required except a trailing
optional `test=<cmd>`; when present it must be last and its value runs to
end-of-line (so the command may contain any character except a newline).

Example:

```
## Tasks
- [t1] config: add [execution] section + parser | side=claude | model=sonnet | review=gpt-5.4 | deps=- | files=spar/config.py,tests/test_config.py
- [t2] task-list parser | side=codex | model=gpt-5.5 | review=opus | deps=t1 | files=spar/exec/tasklist.py,tests/test_tasklist.py
- [t3] task FSM + orchestration | side=claude | model=opus | review=gpt-5.5 | deps=t1,t2 | files=spar/exec/loop.py,tests/test_exec_loop.py
```

Validation (all fatal at parse time):
- `id` unique, matches `t\d+`; `deps` reference existing ids; no dependency cycle.
- `side` is a configured Side; `model` ∈ that Side's catalog; `review` ∈ the
  *other* Side's catalog (the reviewer is the non-implementing Side).
- `files` is a non-empty comma list of globs (the Task's file scope; guard
  enforces it during implementation).
- **File-scope overlap**: two Tasks that could run concurrently (neither depends
  on the other, transitively) whose `files` globs intersect → parse error in the
  concurrency phase; in the sequential-first slice it is a warning (sequential
  merges can't conflict). Overlap between dependency-ordered Tasks is fine.
- Optional per-Task `test=<cmd>` overrides the configured `test_command`.

**Model catalog**: the models each Side can run, declared in config (not
discovered at runtime), injected into the planning prompt so Assignments span
both catalogs. Example:

```toml
[sides.claude]
models = ["opus", "sonnet", "haiku"]
default_model = "sonnet"        # fallback for generated (fix) Tasks
[sides.codex]
models = ["gpt-5.5", "gpt-5.4"]
default_model = "gpt-5.4"

[execution]
test_command = "pytest -q"      # final Test phase; per-Task override in Task List
max_review_rounds = 0            # 0 = unlimited (loop to DONE); see §8
```

## 5. Isolation

- **Target branch** — the user's branch at `spar exec` start. Its name and base
  commit OID are recorded in state (§11). `spar exec` requires a clean target
  worktree at start (refuse otherwise). The final merge (§9) reconciles against
  this recorded base.
- **Integration branch** `spar/integration` — created from the recorded target
  base; the single accumulator. Merged Tasks land here one at a time.
- **Task branch** `spar/<id>-<side>` (e.g. `spar/t1-claude`; `<id>` already
  includes the `t` prefix) — short-lived, branched from the *current*
  Integration branch (so it already contains merged deps). Implemented,
  reviewed, tested here, then merged into Integration and deleted.
- **Worktree** `.spar/worktrees/<side>` — one physical git worktree per Side,
  reused sequentially across that Side's Tasks (a Side is one agent doing one
  Task at a time). A Side's own worktree checks out that Side's current Task
  branch.

**Review reads, never checks out** (git forbids the same branch in two
worktrees): the reviewing Side never checks the Task branch into its own
worktree. Cross-review runs the reviewer against a **read-only view** of the
implementer's worktree — the reviewer reads the files there plus the Task's diff
against its Integration base (`git diff <base>..<task-branch>`), and returns a
verdict. It never edits. The implementer's worktree is the single checkout of
the Task branch.

**Name/collision policy**: at start `spar exec` refuses if `spar/integration`,
any `spar/t*` branch, or `.spar/worktrees/*` already exist *without* matching
`.spar/` execution state; with matching state it resumes; otherwise it prompts
the user to clean up. (Was review remark #7.)

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
- **review**: **Cross-review** — asymmetric, with a per-Task remark ledger
  (open/resolved) mirroring v1's `pending_remarks`/`resolved_remarks`:
  - **Reviewer turn** (non-implementing Side): reads the code (read-only view,
    above) and emits a verdict — `CONTINUE` with new `[MUST]`/`[NICE]` remarks
    (each gets a ledger id), or `DONE` when it has no open `[MUST]`. Only the
    reviewer issues `DONE`; the reviewer never edits code.
  - **Implementer turn**: edits code to address open remarks and emits a verdict
    that only `resolves` remark ids (`#n accepted` = fixed, `#n rejected: why`);
    it never issues `DONE` and its own new remarks are ignored (the implementer
    does not judge its own work). A rejected remark, like v1, must carry a
    justification and stays closed unless the reviewer re-raises it.
  - The loop alternates reviewer→implementer→reviewer… and ends when the reviewer
    emits `DONE`. Open `[NICE]` remarks may remain at `DONE` (they become a
    Task-level backlog, non-blocking, surfaced in the final summary); an open
    `[MUST]` blocks `DONE` exactly as in v1. `AGREE` is not used in execution
    review — only the reviewer's `DONE` is terminal.
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

Running tests spawns no Side Sessions — it is a plain subprocess. Sessions are
created only if a final-Test failure opens a fix Task (below).

**Final Test failure → integration-fix Task.** A per-Task test may be narrower
than `test_command`, so the whole-suite run can fail even though every Task
passed. Consistent with "no task-failure state" (§8), the orchestrator does not
stop: it opens a new **integration-fix Task** and runs it through the *normal*
FSM (§6) — a Task branch off the current Integration, implemented in the Side's
worktree, cross-reviewed by diff, tested, then merged back into Integration.
There is no special "edit Integration directly" path. The fix Task is a
fully-formed Task (§4.1 fields), generated as:
- **id / branch**: the next id in sequence `t<next-number>` (satisfying the
  §4.1 `t\d+` rule) → branch `spar/<id>-<side>`.
- **description**: "make `test_command` pass on the integrated branch" plus the
  captured failing output.
- **side**: defaults to the Side that implemented the Task owning the most
  failing files (ties → the first Side in the run's side order, i.e. `--first`
  then the rest of `--sides`, recorded in state); the user may override at a gate
  if attended.
- **model / review**: taken from that dominating Task *only when the fix Side
  equals its implementing Side*; otherwise (no dominating Task, or the user
  overrode the Side) recomputed from `default_model` — impl = the fix Side's
  `default_model`, review = the other Side's `default_model`. This guarantees
  `model` ∈ fix Side's catalog and `review` ∈ the other Side's catalog (§4.1).
- **deps**: none — all feature Tasks are already merged; fix Tasks are
  serialized last, one at a time.
- **file scope**: the files implicated by the failing tests (from the failing
  output and the integrated diff). If the output does not identify files
  clearly, the scope defaults to the changed files in the integrated diff (all
  Tasks' work); the guard enforces it as for any Task.

After the fix Task merges, the Final Test phase re-runs; loop until the whole
suite passes (or Ctrl+C, §8).

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

**Target moved during Execution**: before the final merge the orchestrator
compares the target branch's current tip against the recorded base OID (§5). If
unchanged → merge (fast-forward or a merge commit). If the target advanced,
`spar` does not silently merge onto an unexpected base: it rebases/merges
Integration onto the new target tip, re-runs the final Test phase, and if that
produces conflicts it surfaces them at the user gate (accept manual resolution /
abort). Never merge past a target the run never saw.

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
- `session.json` (or a sibling `exec.json`) records: current Phase, target
  branch name + base OID, per-Task FSM state and remark ledger, Assignments,
  deps, per-Side per-Phase session ids, branch names, and `turn_in_progress`.
- `transcript/` gains per-Task, per-turn records (impl + review turns).
- Exact schema fixed in the implementation plan; it mirrors v1's structural
  state (source of truth) rather than summarizing sessions.

### 11.1 Recovery is idempotent per git side effect

Execution performs non-atomic git operations (create branch/worktree, run tests,
merge into Integration, delete Task branch, final merge). State cannot be
updated atomically with git, so resume **reconciles recorded FSM state against
actual git state** rather than trusting either alone. Rules, per interrupted
point:

- **mid-implement / mid-review turn**: `turn_in_progress` + artifact-hash compare
  as in v1 — repeat the turn (agent turns are the reproducible unit).
- **mid-test**: tests are idempotent — re-run.
- **crash after merge, before state save**: on resume, check whether the Task
  branch is an ancestor of Integration (`git merge-base --is-ancestor`). If yes,
  the merge happened → mark the Task `merged` (and delete a lingering branch).
- **state says merged but branch still exists**: deleting the branch is
  idempotent → delete and continue.
- **interrupted final merge**: check whether Integration is an ancestor of the
  target branch. If yes → done; if no → redo the final merge (after the §9
  target-moved check).

Every git mutation is bracketed by a state write recording its *intent* before
and *completion* after, so resume always knows the last attempted operation and
can verify it against git.

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

## Review history

_Adversarial review (challenge skill), reviewer: codex gpt-5.5._

### Round 1 — Verdict: CONTINUE

- **#1 [MUST] accepted** — reviewer-worktree checkout is invalid (git forbids
  one branch in two worktrees). Fixed §5: review reads a read-only view of the
  implementer's worktree + diff, never checks out the Task branch.
- **#2 [MUST] accepted** — final merge assumed a stable target. Fixed §5/§9:
  record target branch + base OID, require clean target, reconcile if target
  advanced (rebase + re-test + user gate on conflict).
- **#3 [MUST] accepted** — Task List not parsable enough. Fixed §4.1: explicit
  grammar, example, and fatal validation rules.
- **#4 [MUST] accepted** — asymmetric review underspecified. Fixed §6: per-Task
  remark ledger; reviewer issues CONTINUE/DONE, implementer only resolves ids
  and never judges; NICE may stay open at DONE, MUST blocks; AGREE unused.
- **#5 [MUST] accepted** — recovery vague for git side effects. Fixed §11.1:
  idempotent per-operation reconciliation of FSM vs actual git (ancestor checks
  for merge/final-merge, repeat-turn for mid-turn, re-run for mid-test).
- **#6 [MUST] REJECTED** — "unlimited loops unsafe as default headless." This
  conflicts with an explicit user decision (no caps; Ctrl+C + per-turn timeout;
  cap deferred, YAGNI). The intended v2 use is an *attended* terminal (Ctrl+C
  available); *unattended* headless (cron) is explicitly out of scope — the
  reviewer conflates "headless CLI" with "unattended." Flagged to the user for
  arbitration; they may overturn.
- **#7 [NICE] accepted** — branch/worktree collision policy. Added §5.

### Round 2 — Verdict: CONTINUE

Reviewer verified #1, #2, #4, #5, #7 addressed in the body and accepted the #6
rejection. Remaining/new:
- **#3 [MUST] (continuing) accepted** — grammar omitted the optional `test=`
  field it later referenced. Fixed §4.1: `test=` shown in the grammar as the
  optional trailing field, value runs to end-of-line.
- **#8 [MUST] accepted** — final Test failure had no remediation path. Fixed §7:
  a final-suite failure opens an integration-fix Task (default Side = owner of
  most failing files, user may override), runs the normal FSM, then re-runs the
  Final Test — loop until green. Consistent with "no task-failure state."
- **#9 [NICE] accepted** — clarified §7: the Test phase spawns no Sessions;
  Sessions appear only if a fix Task opens.
- **#10 [NICE] accepted** — normalized Task branch name to `spar/<id>-<side>`
  (id already carries the `t`) in §5 and CONTEXT.md.

### Round 3 — Verdict: CONTINUE

Reviewer verified #3, #9, #10 in the body. One continuing blocker:
- **#8 [MUST] (continuing) accepted** — the integration-fix Task said "touch
  Integration directly," contradicting the Task-branch model (§5/§6), and left
  its generated fields undefined. Fixed §7: the fix Task is a fully-formed Task
  run through the normal FSM (branch `spar/tfix<k>-<side>` off Integration,
  implement → cross-review by diff → test → merge back), with defined id/branch,
  description, side default, model/review inheritance (or per-Side
  `default_model`, added to the §4 config), empty deps, and failing-file scope.

### Round 4 — Verdict: CONTINUE (final loop round; fixes below unverified)

All remaining items were small consistency nits on the generated fix Task; all
accepted and fixed in §7:
- **#11 [MUST]** — fix id `tfix<k>` violated the `t\d+` rule → use the next
  `t<number>`.
- **#12 [MUST]** — tie-break referenced an undefined config `order` → defined as
  the run's side order (`--first` then `--sides`, from state).
- **#13 [MUST]** — a user Side override could make the inherited model violate
  the catalog rule → model/review inherited only when the fix Side matches the
  dominating Task's Side, else recomputed from each Side's `default_model`.
- **#14 [NICE]** — fallback file scope = changed files in the integrated diff
  when failing output doesn't identify files.

**Loop status**: 4 rounds reached (skill cap). No fundamental disputes remain;
Round-4 fixes are localized to §7's fix-Task and are **unverified** by the
reviewer. Standing dispute: **#6** (loop caps) rejected, aligned with the user's
explicit decision — the user may overturn. A +1 verification round is offered.
