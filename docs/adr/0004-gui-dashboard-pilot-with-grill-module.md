# 0004. The GUI is a dashboard-pilot; requirements grilling joins it as a module

Date: 2026-07-10

## Status

Accepted. Amends [0003](0003-spar-as-agent-operated-engine.md) (which stays
in force for the ENGINE: spar's core remains agent-operable and UI-free).

Implementation: dashboard-pilot GUI implemented 2026-07-10
(`26541d7..bb7d379`), grill module (GrillPane) 2026-07-10
(`ff65e18..f35fb8b`); validated live by the fresh-project E2E and the
GUI-piloted full runs of 2026-07-10 (see docs/HANDOFF.md). Amended by
[0005](0005-conversation-modules-and-tool-window-rails.md) (rails +
orchestrator chat).

## Context

ADR 0003 dropped the TUI and in-engine grill on the grounds that the host
agent (Claude Code / Codex) already provides conversation and requirement
refinement. Since then:

- `spar gui` was built and battle-tested as a **dashboard-pilot**: live
  stream, task board, gate panel, run lifecycle — a thin fourth client of
  the same agent-mode protocol, zero engine coupling beyond additive status
  fields.
- Live usage showed the missing piece of the "project from zero" flow: the
  user wants to refine the task (grill-with-docs) INSIDE the same window
  that starts the debate, without hand-running a separate agent session and
  copy-pasting requirements.
- An external review (docs/reviews/2026-07-10-external-review.md) correctly
  flagged the drift between ADR 0003 and the GUI roadmap and asked for a
  formal boundary decision.

## Decision

The product boundary is fixed as follows:

1. **The engine stays per ADR 0003**: headless-operable, UI-free, protocol
   first. Nothing in `spar/` outside `spar/gui/` may depend on the GUI.
2. **The GUI is a dashboard-pilot, not an agent IDE.** It renders engine
   state and drives the engine through the same commands an agent would.
3. **Requirements grilling becomes a GUI module** (the "GrillPane"): a chat
   panel that drives a real host-agent CLI session (the existing claude
   adapter's multi-turn `-p --resume` mechanism) running the user's
   grill-with-docs skill, and ends by writing `.spar/requirements.md`,
   which pre-fills the new-debate form. The GUI hosts the CONVERSATION
   VIEW; the intelligence remains the host agent's.
4. **No embedded terminal in the product core.** The grill chat pane
   removes the main terminal use-case; a general-purpose embedded terminal
   (pty + VT emulation) is out of scope unless the chat pane proves
   insufficient in practice — and then only after a costed research spike.

## Consequences

- ADR 0003's "TUI dropped" clause is superseded by the dashboard-pilot GUI;
  its "grill-in-spar dropped" clause is refined: grilling is NOT in the
  engine — it is a GUI module delegating to the host agent CLI.
- GUI work must never outpace engine hardening: recovery/invariant work
  (external review P0) takes priority over new GUI surface.
- The grill module's only engine-visible contract is the requirements file
  handoff; no new engine states or gates.
