# 1. Fresh agent sessions per phase; the Plan is the only handoff

Date: 2026-07-08
Status: Accepted

## Context

A spar run has sequential Phases (Debate → Execution → Test). Each Side drives
an agent CLI (claude / codex) that supports resuming a prior Session. We must
decide whether Execution reuses the Debate's Sessions (carrying that context
forward) or starts fresh.

v1 already establishes that a Session is a token-cost optimization only, never a
source of truth: every turn must be reproducible from spar's own persisted
state. The Debate produces a Plan — a complete, consensus-approved artifact.

## Decision

Phases are isolated at their boundaries. Within a Phase each Side keeps one
continuous Session (via resume); at a Phase boundary the next Phase starts
**fresh** Sessions. The **Plan is the sole handoff** between Debate and
Execution — Execution is seeded with the Plan, not the Debate transcript.

## Consequences

- Cheaper: Execution context excludes the Debate's many turns of argument.
- Cleaner reasoning: the implementer reasons over the agreed Plan, not the
  rejected options and arguments that produced it.
- Forces the Plan to be self-sufficient — which the Debate's consensus + guard
  already aim to guarantee. A Plan that cannot drive Execution is a Debate
  defect, surfaced early.
- Resumability: Execution gets its own `--continue` state, independent of the
  Debate loop.

## Opt-in alternative: `--merge-sessions`

For small tasks the split-phase handoff (structured Plan → fresh session
re-reading it → worktree) can cost more than the task itself. `--merge-sessions`
lets each Side keep ONE continuous Session spanning every Phase (claude one,
codex one — two CLIs never share a Session). It is orthogonal: only Session
lifetime changes, the pipeline (task split, worktrees, cross-review, FSM) is
unchanged. Default is off (fresh Sessions per Phase).
