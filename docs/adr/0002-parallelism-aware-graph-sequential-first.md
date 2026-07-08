# 2. Parallelism-aware Task graph now; sequential execution first, concurrency later

Date: 2026-07-08
Status: Accepted

## Context

The Execution phase can run the two Sides' work concurrently (each in its own
worktree) or one Task at a time. True concurrency promises a wall-clock speedup
but adds a class of complexity: a task scheduler, serialized merges into the
Integration branch, sync points when a reviewer is busy, and merge-conflict
handling between concurrently-built branches.

Two observations bound the payoff:
- Concurrency is at most 2-way (two Sides), and dependencies serialize dependent
  Tasks. Review, per-Task testing, and merges are sync points. Realistic speedup
  is roughly 1.3–1.7x, not a large fan-out.
- Conflicts arise only at *merge* time, and only when concurrent Tasks touch the
  same files — a function of how Tasks are partitioned, not of concurrency
  itself.

## Decision

Split the concern in two:

1. **Planning is parallelism-aware from day one.** Each Task in the Task List
   carries explicit dependencies and a file scope. The planning negotiation
   partitions work so concurrent Tasks are file-disjoint and dependent Tasks are
   ordered by deps. This is designed now regardless of how Execution runs.

2. **Execution is sequential first (one ready Task at a time), concurrency
   second.** The sequential engine and a later 2-way concurrent engine consume
   the *same* parallelism-aware Task graph. Turning on concurrency is an
   orchestrator capability (scheduler + a merge lock) layered over the existing
   Task model — no rework of the graph.

## Consequences

- Correctness before concurrency: the FSM, asymmetric Cross-review, per-Task
  testing, and merge logic are built and tested on the simpler blocking engine
  (reusing v1's sequential subprocess model).
- No design is lost by deferring concurrency: the graph is already
  conflict-avoiding, so enabling parallel execution later requires no changes to
  how Tasks are described or assigned.
- The bounded, honestly-modest speedup is deferred until the core is proven —
  matching its modest size.

## Alternatives considered

- **True concurrency from the first version**: rejected for the first slice.
  Concurrent subprocess orchestration, merge races, and reviewer-busy sync
  points are a distinct class of bugs; building them on an unproven FSM/merge
  core multiplies risk for a ~1.5x payoff.
- **Ignore parallelism in planning, add it purely in execution later**:
  rejected. Retrofitting file-scope/deps into an already-agreed Task List format
  is a breaking change; cheap to include up front.
