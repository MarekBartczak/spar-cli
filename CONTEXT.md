# spar — Domain Glossary

Canonical terms for the spar debate/execution engine. Glossary only — no
implementation details.

## Phase
A distinct stage of a spar run with its own inputs, outputs, and completion
gate. Phases are sequential and, by default, isolated — each runs in fresh
agent Sessions:
- **Debate** — two Sides argue over and edit a text Artifact until Consensus.
  Output: an agreed Plan.
- **Execution** — Sides implement the agreed Plan as code. Output: implemented,
  cross-reviewed changes.
- **Test** — the final, comprehensive test run: after every Task has merged
  into the Integration branch, the orchestrator runs the full configured test
  command against the *whole* integrated change set. Passing gates the final
  merge to the user's branch. (This is separate from per-Task testing, which
  gates each Task's own merge — see Task.)

## Plan
The Artifact produced by a successful Debate phase and consumed by Execution.
The Plan is the sole handoff between phases: Execution needs the Plan, not the
Debate that produced it.

## Session
An agent CLI session (claude / codex). A Session is a token-cost optimization
only, never a source of truth — any turn must be reproducible from spar's own
persisted state.

Two levels:
- **Within a Phase**: each Side keeps a single continuous Session across all its
  turns (via CLI resume). A Phase is not chopped into per-turn Sessions; the
  Debate is one Session per Side.
- **At a Phase boundary** (default): the next Phase starts fresh Sessions rather
  than resuming the previous Phase's, so the Debate transcript does not pollute
  Execution context. The Plan carries all needed state across the boundary. So a
  default run uses two Sessions per Phase (one per Side).

`--merge-sessions` (opt-in): each Side keeps ONE Session spanning every Phase —
claude one Session from Plan through Execution and Test, codex likewise. Two
CLIs never share a Session; "merged" means per-Side continuity across Phase
boundaries. Intended for small tasks where the full split-phase handoff costs
more than the work itself.

## Side
One of the two agents (e.g. claude, codex), symmetric in role, that debate the
Plan and implement Tasks.

## Task
A unit of implementation work derived from the Plan, carrying an Assignment,
dependencies on other Tasks, and a **file scope** (the files/modules it may
touch). A Task moves through: pending → ready (deps merged) → implementing →
review (Cross-review loop) → testing → merged. A Task merges into the
Integration branch only after its Cross-review reaches DONE *and* its per-Task
tests pass; a test failure sends it back to implementing. Per-Task testing is
distinct from the final comprehensive Test phase.

The file scope + dependencies make the Task List **parallelism-aware**:
concurrent Tasks are planned to be file-disjoint so their merges into the
Integration branch don't conflict, and dependent Tasks are serialized by deps.

## Task List
The breakdown of the Plan into Tasks, agreed by Consensus during the Debate
phase (proposed by one Side, accepted/amended by the other). Machine-parsable —
a `## Tasks` section of the Plan with one entry per Task (Assignment + deps),
closed mechanically like a verdict rather than read from prose.

## Assignment
The models bound to a Task, chosen during planning from both Model catalogs:
- **implementing Side** and its **implementation model** — who builds it;
- **review model** — the model the *other* Side runs to review it.
Both models are negotiated per Task (a trivial Task may be reviewed by a mid
model like sonnet / gpt-5.4; only hard Tasks need a top model). The reviewing
Side is always the one that did not implement (see Cross-review).

## Model catalog
The set of models a Side can run (claude: opus / sonnet / haiku …; codex:
gpt-5.5 / gpt-5.4 …). Declared in config, not discovered at runtime, and
injected into the planning prompt so Assignments can be chosen across both
Sides' catalogs.

## Cross-review
Review of a Task's implementation by the Side that did NOT implement it: claude
implements, codex reviews, and vice versa. Asymmetric: the reviewer only judges
(raises MUST/NICE remarks via a CONTINUE verdict); only the implementing Side
edits the code. The implementer addresses remarks, the reviewer re-reviews, and
the loop ends when the reviewer emits DONE (no blocking remarks). This reuses
the verdict protocol but with a single editing Side, unlike the symmetric Debate.

## Integration branch
The single accumulator branch (`spar/integration`), created from the user's
target branch. Reviewed-and-tested Tasks are merged into it one at a time. After
every Task has landed and the final Test phase passes, the orchestrator presents
a summary and the user green-lights merging the Integration branch into their
target branch (`--auto-integration-merge` skips the gate). This final merge is
the only outward, user-gated action of Execution.

## Task branch
A short-lived branch per Task (`spar/<id>-<side>`, e.g. `spar/t1-claude`),
created from the current
Integration branch (so it already contains the Tasks it depends on). The Task is
implemented, cross-reviewed, and tested on this branch, then merged into the
Integration branch and deleted.

## Worktree
A physical git worktree per Side (`.spar/worktrees/<side>`) in which that Side's
agent works. Because a Side is a single agent doing one Task at a time, the
worktree is reused sequentially across that Side's Tasks — it checks out the
current Task branch for that Side.
