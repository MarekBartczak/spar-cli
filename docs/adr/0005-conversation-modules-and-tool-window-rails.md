# 0005. Conversation modules + tool-window rails; orchestrator chat replaces the terminal for good

Date: 2026-07-11

## Status

Accepted. Amends [0004](0004-gui-dashboard-pilot-with-grill-module.md)
(dashboard-pilot boundary stays in force; this ADR generalises its grill
module and settles the main-window layout).

Implementation: implemented 2026-07-11 (`c3a7710..9428be8`) in
`spar/gui/{conversation,orchestrator,rails,chat_store}.py`. The left-rail
Pliki module shipped via [0006](0006-files-module-editor-and-search.md)
(tranches A+B, 2026-07-11); the git module remains a future tranche.

## Context

The GrillPane shipped as a modal chat driving a host-agent CLI session
(claude adapter, `-p --resume`, lettered options rendered as buttons) and
proved the pattern live. Immediately after, the user asked for the same
interaction as a PERSISTENT surface: a chat with an "orchestrator" in the
main window, for analysis, questions during runs, and preparing follow-up
work — explicitly removing the last argument for an embedded terminal.
Mockups (v3/v4, 2026-07-11) were accepted with a JetBrains-style layout:
vertical icon rails on both window edges toggling collapsible tool panels.

## Decision

1. **Conversation modules.** `GrillSession` is refactored into a shared
   `ConversationSession` (adapter-backed multi-turn chat: session resume,
   option parsing, generation-token signal suppression, session-lost
   recovery, abandoned-thread retention). Grill (modal) and the orchestrator
   chat (docked) are two thin instances with different opening prompts and
   lifecycles. Future conversation surfaces reuse the same class.
2. **Tool-window rails.** The main window gets vertical icon rails on BOTH
   edges (right from v1: Taski, Czat, Bramka; left reserved — future Pliki,
   Git). Icons toggle panel visibility; collapse state persists in
   QSettings. Everything collapsed → stream takes the full width. The gate
   panel gets a rail icon with an attention dot and force-opens when a gate
   pends; collapsing it never discards the pending decision.
3. **The orchestrator chat is a read-only advisor.** It reads the repo and
   `.spar/` state, answers questions (including gate context), and drafts
   task descriptions ("Nowa debata z tym szkicem" handoff). It never edits
   files, never holds the engine lock, and NEVER makes gate decisions — the
   GatePanel remains the only decision pilot. During a live run the chat
   shows a warning banner and stays available for questions.
4. **Persistent session.** The chat's session id (and minimal metadata)
   lives in `.spar/chat.json`; restarting the GUI resumes the conversation.
5. **The embedded terminal is dead.** ADR 0004's "unless the chat pane
   proves insufficient" escape hatch is closed: the docked orchestrator
   chat is the general conversation surface. Any future need routes through
   conversation modules or new tool-window panels, not a pty.

## Consequences

- ADR 0004's grill-module clause is generalised: the GUI hosts conversation
  VIEWS; intelligence stays in host-agent CLI sessions. No new engine
  states, gates, or protocol — the engine remains per ADR 0003.
- Accepted future tranches (separate plans, in order, AFTER the chat):
  Pliki/editor module on the left rail (tree + tabbed centre with a pinned
  Strumień tab; view-only while RUNNING/GATE/LOCKED, editable when the
  engine is free; explicitly NO LSP/completion/refactoring), then a git
  module (commit/push) on the left rail.
- The read-only-advisor boundary is testable: the chat adapter session runs
  with read-only tooling and the GUI exposes no gate actions through chat.
