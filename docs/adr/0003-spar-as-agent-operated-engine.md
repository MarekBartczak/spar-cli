# 0003. spar is an agent-operated engine, not a standalone assistant

Date: 2026-07-09

## Status

Accepted

## Context

The original roadmap (post-v2) planned two user-facing features inside spar:
a **grill phase** (spar interrogates the user to refine requirements before
the debate) and a **TUI** front-end. Both exist to give a human a
conversational interface to spar.

Meanwhile spar's users already work inside conversational agents (Claude
Code, Codex CLI) that natively provide: requirement grilling (skills like
grill-with-docs), repo context, and a human-friendly interface. Building an
interrogation UX and a TUI into spar duplicates — worse — what the host
agent already does.

## Decision

spar targets a **host agent as its primary operator**. The human grills the
task with their agent; the agent invokes spar with refined requirements; spar
runs the two-vendor debate → task split → implementation → cross-review →
tests → gated merge; the agent relays gates and results to the human.

Consequences for the roadmap:
- **Dropped**: grill phase inside spar; TUI.
- **Added (agent mode)**: headless gates (gate = persist state + exit with a
  machine-readable status; decision returns via a flag on resume), a
  file-based task input (`--task-file`), machine-readable status output, and
  a host-agent wrapper (skill/slash command).

Interactive stdin gates remain for direct human use — agent mode is additive.

## Consequences

- spar stays focused on what the host agent cannot do: deterministic
  two-vendor orchestration with objective test gates and crash-safe state.
- The human-interface surface (conversation, rendering, questions) is
  delegated to the host agent — no UI code to maintain.
- spar must guarantee non-interactive operability end-to-end: every decision
  point needs a flag/exit path, every state a machine-readable readout.
- Cost profile acknowledged: spar is for tasks where error cost outweighs
  token cost; trivial edits stay with the plain host agent.
