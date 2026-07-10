---
name: spar
description: Drive a two-vendor spar run (plan debate + execution + tests) headlessly for a grilled task; use when the user wants spar to implement something
---

# spar

Drive `spar` — a two-vendor debate + execution engine (Claude Code + Codex
CLI, or whichever sides are configured) — headlessly on behalf of the
human, relaying every gate decision. Full protocol reference:
`docs/AGENT.md` in the spar-cli repo (commands, exit codes, gate matrix,
`status --json` schema). This skill is the operational summary; read
`docs/AGENT.md` if anything below is ambiguous.

## Prerequisites

Before starting a run, verify:

1. `spar` is reachable: `spar --list-commands` (exit 0 and it prints the
   resolved command per side) or check the venv's `bin/spar`. A non-zero
   exit here means fix configuration before doing anything else.
2. `.spar/config.toml` exists in the target repo (project-level config —
   model catalogs, `impl_models`, `test_command`, round/fix-task caps), or
   the global `~/.config/spar/config.toml` has both sides configured. If
   neither is set up, stop and help the human configure it first — do not
   guess at `test_command` or model names.

If either check fails, stop and report the gap; do not attempt to run spar
against an unconfigured or unreachable setup.

## Live output

Before starting a run, once per session: run `spar ui`. It best-effort
opens a terminal (tmux split if already inside tmux, else a detected
terminal emulator) running `spar watch` — a live colorized tail of
`.spar/live.log`. It always exits 0, even if no terminal could be spawned
(it then just prints a manual instruction). Tell the human what the window
is for. From this point on, **always pass `--quiet` to every `spar`/`spar
exec` command** below — it suppresses the verbose per-turn model chatter on
this session's stdout (spar's own status/gate/error lines still print, and
`.spar/live.log` still gets everything regardless). Transcripts under
`.spar/transcript/` remain the authoritative record of a run; `live.log`
and `spar watch` are a convenience live view only.

## The loop

1. **Grill requirements with the human.** Turn their request into a
   concrete, unambiguous task description and write it to a file, e.g.
   `requirements.md`.
2. **Start the debate:**
   ```bash
   spar --task-file requirements.md --sides claude,codex --first claude --tasks --headless --quiet
   ```
   `--tasks` is required — it is the bridge into `spar exec`. Expect exit
   **10** at the first gate.
3. **On exit 10:** run `spar status --json`, read `pending_gate.name` and
   `pending_gate.options`, decide (see Gate-relay etiquette), then resume:
   ```bash
   spar --continue --headless --quiet --gate accept        # or remarks:<file> / abort
   ```
   Repeat step 2–3 until the debate exits 0 (Plan agreed) or a terminal
   failure code (2/4/5) — see Failure surfacing.
4. **Start execution once the Plan is agreed:**
   ```bash
   spar exec --headless --sides claude,codex --first claude --quiet
   ```
5. **On each exit 10:** `spar status --json` → decide/relay → resume:
   ```bash
   spar exec --continue --headless --quiet --gate accept   # or extend:<n> / abort
   ```
6. **On exit 0:** the run merged into the caller's branch. Report the
   final-merge summary (test results, tasks, any open `[NICE]` backlog) to
   the human. **On exit 2/4/5:** stop and surface the failure verbatim —
   see Failure surfacing. **On exit 130:** the run was interrupted; resume
   with `spar --continue --headless` (or `spar exec --continue --headless`
   depending on which phase was running) rather than starting over.

Only pass `--gate` values that appear in `pending_gate.options` for the
gate you are actually at — an out-of-menu value is a usage error (exit 2).

## Gate-relay etiquette

- **`final_merge` always needs the human, no exceptions.** Before ever
  issuing `--gate accept` on a `final_merge` gate, show the human the merge
  summary and get their explicit approval — this is the point where code
  lands in their branch.
- **`consensus` and `review_rounds` may be auto-decided only if the human
  pre-authorized it for this run** (e.g. they said up front "auto-accept
  consensus, always ask before merging"). Absent that authorization, relay
  every gate — including these — to the human and wait for their decision.
- Never fabricate a decision on the human's behalf; when in doubt, ask.

## Failure surfacing

Never bury a non-zero exit. For any exit code other than 0 (done) or 10
(gate pending, expected mid-loop), stop and show the human:

- the exact command that was run,
- the exit code,
- the relevant stderr/output or `spar status --json` state.

Do not silently retry, paraphrase away, or hide a 2 (config/usage error), 3
(lock/state error), 4 (protocol/adapter abort), or 5 (user abort) exit —
these are substantive failures, not transient noise. 130 (interrupted) is
the one exception: resume with `--continue` rather than treating it as a
failure to report and stop on.
