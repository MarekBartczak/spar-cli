# Live Observability (streaming, live.log, watch, ui) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything the models say during a spar run streams to the screen live (like watching claude/codex directly), to a stable `.spar/live.log`, and — condensed — to a host agent running with `--quiet`; a `spar watch` viewer and a `spar ui` window-spawner give the human a live side panel while an agent drives spar.

**Architecture (grill decisions):** One event stream, three sinks. Adapters stop buffering (`subprocess.run`) and stream the CLI's stdout line-by-line (claude: `--output-format stream-json --verbose --include-partial-messages`; codex: already JSONL). Each adapter parses its own event schema into human-readable display lines and hands them to an injected `on_event(str)` callback; the raw stream still lands in the per-turn transcript file exactly as today. A new `StreamSink` fans display lines out to: (1) stdout — full by default, suppressed by `--quiet`; (2) `.spar/live.log` — ALWAYS, truncated at run start, line-buffered; spar's own log lines also go through the sink so live.log is the complete picture. Lines are prefixed `[side task role]` (debate: `[side r<round>]`). `spar watch` is a stdlib ANSI colorized follower of live.log (tail -f semantics + pending-gate banner). `spar ui` spawns a viewer window via a detection cascade (tmux → Warp launch-config → known terminal emulators → printed instruction). The host-agent skill switches to `--quiet` + `spar ui` up front.

**Tech Stack:** Python 3.11+ stdlib only (threads for the reader; NO new dependencies — watch uses raw ANSI, not rich). pytest.

## Global Constraints

- Zero new runtime dependencies.
- Transcript files in `.spar/transcript/` keep EXACTLY today's content (raw stream per turn) — streaming must not change what is persisted there.
- Timeout semantics unchanged: a turn that exceeds `timeout_sec` still raises `AdapterError(f"timeout after {timeout_sec}s")` with the partial stream saved to the events file.
- `TurnResult` contract unchanged (session_id, reply_text, events_path, exit_code); all existing adapter/orchestrator/executor tests must stay green (fakes don't stream — `on_event` must be optional everywhere).
- `--quiet` only silences the stdout sink; live.log and spar's log lines to the agent (turn-complete, gates, exits) are unaffected.
- TDD; conventional commits; NO Co-Authored-By / AI-attribution trailers (hard rule).
- Suite green after every task: `python3 -m pytest tests/ -q` (386 passed, 2 skipped before this plan).

---

### Task 1: streaming `run_cli` + adapter event parsing (Opus)

**Files:**
- Modify: `spar/adapters/base.py` (`run_cli` grows an `on_line` callback + streaming implementation)
- Modify: `spar/adapters/claude.py` (stream-json argv; per-event display formatting; result extraction from the event stream)
- Modify: `spar/adapters/codex.py` (per-event display formatting; JSONL already streams)
- Test: `tests/test_adapter_base_streaming.py` (new), `tests/test_adapter_claude.py`, `tests/test_adapter_codex.py`, `tests/fakes/fake_claude.py`, `tests/fakes/fake_codex.py`

**Interfaces:**
- Consumes: current `run_cli(cmd, timeout_sec, events_path, stdin_text=None, cwd=None)` and both adapters' `run_turn`.
- Produces:
  - `run_cli(..., on_line: Callable[[str], None] | None = None)` — same return type (`CompletedProcess[str]` with the FULL stdout in `.stdout`), but implemented with `subprocess.Popen`: a reader thread consumes stdout line-by-line, appends each line to an incrementally-written events file (opened once, `flush()` per line) and invokes `on_line(line)`; the main thread enforces the deadline (`proc.wait(timeout=...)`; on `TimeoutExpired` kill the process, join the reader, raise `AdapterError` — partial stream is already on disk). stderr captured separately as today. `on_line=None` keeps behavior identical for callers that don't stream. Callback exceptions must NEVER kill the turn (wrap in try/except, drop).
  - `Adapter.run_turn(prompt, session_id, timeout_sec, on_event: Callable[[str], None] | None = None)` — protocol gains the optional param. `on_event` receives HUMAN-READABLE display lines (not raw JSON), already newline-free.
  - claude adapter: non-readonly AND readonly argv switch `--output-format json` → `--output-format stream-json --verbose --include-partial-messages`; `run_turn` collects the stream, extracts `session_id` and the final reply from the terminal `{"type":"result", ...}` event (field `result`), and formats display lines from events: assistant text deltas (`{"type":"stream_event"...}` partial chunks → emit as-is, coalescing is the sink's problem — v1 emits chunk text when non-empty), tool use starts (`tool: <Name> <first 80 chars of input>`), and the result line (`done (<duration>s)` if available). Malformed/unknown JSON lines → skip silently for display, still persisted raw.
  - codex adapter: display lines from its JSONL: `item.completed` agent messages (`text`), command/tool events (emit `exec: <cmd>` style), `turn.completed` → `done`. Unknown lines skipped for display.
  - Both adapters call `run_cli(..., on_line=<parse+forward>)` only when `on_event` is not None; otherwise pass `on_line=None` (zero overhead, today's path).
- Fakes: extend `tests/fakes/fake_claude.py` to support `--output-format stream-json` (emit N JSONL lines: an assistant chunk, a tool_use event, a result event with `session_id`+`result` — driven by the existing script-dir files) and keep the plain `json` mode for old tests. Same idea for `fake_codex.py` (it already emits JSONL — just verify).

- [ ] **Step 1: failing test — `run_cli` streams lines live.** New `tests/test_adapter_base_streaming.py`: run a small inline `python3 -c` producer that prints 3 lines with `sleep 0.1` between them; collect `on_line` calls with timestamps; assert 3 calls arrive, events file contains all 3 lines, and `.stdout` equals the full output. Also: timeout test — producer sleeps 10s, `timeout_sec=1` → `AdapterError`, partial line(s) present in the events file. Also: `on_line` raising must not break the run.
- [ ] **Step 2: run — FAIL** (no `on_line` param).
- [ ] **Step 3: implement streaming `run_cli`** (Popen + reader thread per the interface). Run new tests + full suite — PASS (old callers unaffected).
- [ ] **Step 4: failing tests — claude stream argv + parsing.** Update the FOUR argv-contract tests: expected `--output-format stream-json --verbose --include-partial-messages` (in the same position `json` sat). New test: with the streaming fake, `run_turn(..., on_event=collect)` yields display lines including the tool line and text chunk, and `TurnResult.reply_text`/`session_id` come from the result event. Readonly argv test updated the same way.
- [ ] **Step 5: implement claude adapter + fake; run — PASS.**
- [ ] **Step 6: failing tests — codex display parsing.** `run_turn(..., on_event=collect)` over the existing JSONL fake yields the agent-message text line and `done`; reply_text/session_id unchanged (last-message file + thread id logic untouched).
- [ ] **Step 7: implement codex adapter; run — PASS.**
- [ ] **Step 8: full suite; commit** — `feat(adapters): stream CLI events live (claude stream-json, codex jsonl) with on_event callback`

---

### Task 2: `StreamSink`, prefixes, `--quiet`, `.spar/live.log` (Sonnet)

**Files:**
- Create: `spar/stream.py`
- Modify: `spar/orchestrator.py` (thread a sink-bound `on_event` into `_invoke`'s adapter call; route `self.log` through the sink)
- Modify: `spar/exec/loop.py` + `spar/exec/review.py` (same for exec `_invoke`; prefix carries task id + role)
- Modify: `spar/cli.py` (`--quiet` on both parsers; sink construction; wire `log=`)
- Test: `tests/test_stream.py` (new), `tests/test_exec_loop.py`, `tests/test_orchestrator.py`, `tests/test_cli.py`

**Interfaces:**
- Produces `spar/stream.py`:

  ```python
  class StreamSink:
      """Fan-out for display lines: stdout (unless quiet) + live.log (always)."""

      def __init__(self, spar_dir: Path, quiet: bool = False, stdout=sys.stdout) -> None:
          # truncate <spar_dir>/live.log; keep an open, line-buffered handle
      def event(self, prefix: str, line: str) -> None:
          # "[<prefix>] <line>" -> stdout (if not quiet) + live.log
      def log(self, message: str) -> None:
          # spar's own log lines: ALWAYS stdout (even when quiet) + live.log
      def close(self) -> None: ...
  ```

  `log()` is what replaces the bare `print` currently passed as `log=`; `event()` is what adapter `on_event` lines go through. Quiet mode: `event` skips stdout, `log` never does (the agent needs spar's protocol lines).
- Orchestrator: `Orchestrator.__init__` accepts optional `sink: StreamSink | None`; `_invoke` builds `on_event=lambda ln: sink.event(f"{side} r{state.round}", ln)` when a sink is present and passes it to `adapter.run_turn`. `self.log` routed to `sink.log` when sink present (CLI keeps working without a sink — tests pass `log=`).
- Executor: same pattern; prefix `f"{side} {task.id} {role}"` where role is `impl`/`review` — the call sites live in `spar/exec/review.py::_invoke` (which already receives `role`) — thread the callback down via `run_cross_review(..., on_event=...)`-style plumbing mirroring how `log` travels today (executor builds per-task callbacks; review passes them into `_invoke`'s `adapter.run_turn`).
- CLI: `--quiet` flag on main + exec parsers ("suppress live model output on stdout; spar's own logs and .spar/live.log are unaffected"); build `StreamSink(Path('.spar'), quiet=args.quiet)` and hand it to the orchestrator/executor; `spar status`/`watch` unaffected.

- [ ] **Step 1: failing sink tests** (`tests/test_stream.py`): event writes prefixed line to fake stdout + live.log; quiet suppresses stdout for `event` but not `log`; live.log truncated on construction; close flushes.
- [ ] **Step 2–3: implement sink; PASS.**
- [ ] **Step 4: failing wiring tests.** Exec: FakeAdapter gains an optional scripted `emit` (call `on_event("model says hi")` inside `run_turn` when provided) — assert the line lands in live.log with prefix `[A t1 impl]` and on captured stdout; with `quiet=True` only live.log. Debate: same idea via test_orchestrator fakes with prefix `[claude r0]`. CLI: `--quiet` parses on both parsers and reaches the sink (monkeypatch builder).
- [ ] **Step 5: implement wiring; full suite PASS.**
- [ ] **Step 6: commit** — `feat(stream): StreamSink — stdout + always-on .spar/live.log, --quiet, [side task role] prefixes`

---

### Task 3: `spar watch` (Sonnet)

**Files:**
- Create: `spar/watch.py`
- Modify: `spar/cli.py` (subcommand routing `watch`, parser `--from-start`)
- Test: `tests/test_watch.py` (new), `tests/test_cli.py`

**Interfaces:**
- `spar watch [--from-start]` — follows `.spar/live.log` (default: seek to end, print new lines as they appear; `--from-start` prints the whole file first). Stdlib only. Core is a testable generator:

  ```python
  def follow(path: Path, from_start: bool = False, poll_sec: float = 0.25,
             stop: Callable[[], bool] = lambda: False) -> Iterator[str]:
      """Yield lines appended to path; tolerate the file not existing yet
      (wait for it) and truncation (reopen from 0 — a new run started)."""
  ```

  plus a pure `colorize(line) -> str`: ANSI-colors the `[prefix]` (stable color per side hash), highlights `spar exec:`/`spar:` log lines, and renders a bright banner for lines containing `gate '<name>' pending`. `main_watch(argv)` loops `follow` → `print(colorize(line))`, exits cleanly on Ctrl+C (rc 0).
- CLI routing: leading token `watch` (like `exec`/`status`), no config needed.

- [ ] **Step 1: failing tests**: `follow` yields appended lines (writer thread appends during iteration; `stop` callable ends it), handles pre-existing content with/without `--from-start`, survives truncation; `colorize` marks a gate-pending line and prefixes; CLI routes `spar watch --help` (SystemExit 0).
- [ ] **Step 2–3: implement; PASS.**
- [ ] **Step 4: commit** — `feat(cli): spar watch — live colorized viewer over .spar/live.log`

---

### Task 4: `spar ui` spawn cascade + skill/AGENT.md updates (Sonnet)

**Files:**
- Create: `spar/ui.py`
- Modify: `spar/cli.py` (routing `ui`)
- Modify: `docs/AGENT.md`, `skills/spar/SKILL.md`
- Test: `tests/test_ui.py` (new)

**Interfaces:**
- `spar ui` — opens a live viewer beside the user's session. Pure decision function + thin spawner:

  ```python
  def pick_spawn_argv(env: dict, which: Callable[[str], str | None]) -> list[str] | None:
      """Detection cascade, first hit wins:
      1. env has TMUX          -> ["tmux", "split-window", "-h", "spar watch"]
      2. which("warp-terminal") -> ["warp-terminal", ...]  # new window; watch
                                   # command via a launch-config written once to
                                   # ~/.local/share/warp-terminal/launch_configurations/spar-watch.yaml
                                   # (best-effort; fall through on any doubt)
      3. first of x-terminal-emulator / gnome-terminal / konsole / xterm:
         gnome-terminal -> ["gnome-terminal", "--", "spar", "watch"]
         others         -> [term, "-e", "spar watch"]
      4. None -> caller prints the manual instruction
      """
  ```

  `main_ui()` runs the argv detached (`subprocess.Popen`, no wait) or prints: `Open a split/terminal and run: spar watch`. Always exit 0 (spawning a viewer must never fail a pipeline). Warp branch: keep MINIMAL — if writing/using the launch config is at all uncertain at implementation time, print the instruction instead (the cascade's step 2 may legitimately degrade to step 4; note it in the code).
- Docs: AGENT.md gets a "Live output" section — agent flow: run `spar ui` once at session start (tell the human what the window is), then ALWAYS pass `--quiet` to spar/spar exec; live.log & watch explained; note that transcripts remain authoritative. SKILL.md prerequisites + loop updated the same way.

- [ ] **Step 1: failing tests** for `pick_spawn_argv` (tmux env → tmux argv; no tmux + gnome-terminal available → its argv; nothing → None) and `spar ui` routing.
- [ ] **Step 2–3: implement; PASS.**
- [ ] **Step 4: verify docs commands** against `spar --help`/`spar watch --help`/`spar ui --help` (run them).
- [ ] **Step 5: commit** — `feat(cli): spar ui — spawn a live viewer window; agent docs use --quiet + watch`

---

### Task 5: live smoke test (manual, user-driven — no model)

- [ ] In `/home/marek/P_PROJ/spar_tests`: human runs `spar watch` in a Warp split; agent (Claude Code) runs the AGENT.md loop with `--quiet` on a small brownfield task. Verify: full model output visible live in the watch pane (both vendors, prefixes), agent context stays lean, gates still relay via status/--gate, transcript files unchanged in format, Ctrl+C in watch harmless. Record outcome in HANDOFF + auto-memory.

---

## Self-Review Notes

- Grill decisions all encoded: full stream by default (user's explicit call), `--quiet` for agents, always-on live.log, `[side task role]` prefixes, stdlib watch, `spar ui` cascade with Warp fallback-to-instruction, skill updated.
- Riskiest piece is Task 1 (Popen + reader thread + timeout semantics + claude stream-json migration) — assigned Opus; the events-file and TurnResult contracts are pinned by existing tests.
- Fakes must gain stream-json support without breaking the four argv-contract tests being updated in the same task — watch the ordering inside Task 1's steps.
- `--quiet` deliberately does NOT silence `sink.log` — the agent's whole protocol depends on those lines.
