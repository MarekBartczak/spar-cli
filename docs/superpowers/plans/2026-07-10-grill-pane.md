# GrillPane — requirements grilling inside the GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In `spar gui`, the user grills a task with the model in a chat window (question shown, answer box below, option buttons for A/B/C choices), driven by a real claude session running the user's grill-with-docs skill; the session ends by writing `.spar/requirements.md`, whose content pre-fills the new-debate task field. Per ADR 0004 (GUI module; engine untouched).

**Feasibility retired by live spike (2026-07-10, spar_test_2):** the skill loads in `-p` mode via our `ClaudeAdapter`; the session holds context across `--resume` turns; questions arrive with lettered options + a recommendation; the final turn writes a well-structured `.spar/requirements.md`. The plan composes existing bricks only.

**Architecture:** New `spar/gui/grill.py`: a `GrillSession(QObject)` GUI-thread FACADE whose private `_GrillWorker` (moved to a QThread) owns one `ClaudeAdapter` conversation (model = claude side's `debate_model or model or default_model`, cwd = project_dir, events to `.spar/transcript/grill-*`), each turn executed OFF the GUI thread (a `QThread`-hosted worker; `run_turn` blocks for up to minutes) with the adapter's `on_event` streamed into the chat live; a pure `parse_options(reply) -> list[Option]` extracting lettered choices for buttons; finish = `.spar/requirements.md` created/updated by the model (CONTENT-HASH comparison against a session-start snapshot, checked after each turn — see Task 1) → "Użyj w debacie" button. UI = modal `GrillDialog` opened from the new-debate dialog: the user's draft task ("szkic") seeds the opening prompt; on finish the requirements content replaces the task field.

**Flow:** Nowa debata → wpisz szkic → przycisk **„Grilluj z modelem…"** → GrillDialog (chat) → pytania/odpowiedzi (przyciski opcji lub własny tekst) → model zapisuje requirements.md → „Użyj w debacie" → powrót do formularza z wypełnionym zadaniem → OK → debata.

**Tech Stack:** Python 3.11+, PySide6 (existing `gui` extra), pytest-qt. Engine untouched (uses the public `Adapter` contract only).

## Global Constraints

- Engine untouched: everything under `spar/gui/`; the adapter is consumed via its existing public `run_turn(prompt, session_id, timeout_sec, on_event)` contract.
- GUI thread never blocks: every `run_turn` executes in the worker thread; all UI mutation happens via queued signal connections.
- The grill session is best-effort: closing the dialog mid-grill abandons the session cleanly (thread stopped after the in-flight turn; no engine state involved). Timeout per turn = the project's `[debate] turn_timeout_sec`.
- A turn error (AdapterError/timeout) renders as a chat notice with a "Ponów" button — never a crash, never a silent hang.
- TDD for the pure pieces (option parser, prompt template, finish detection); dialog wiring tested with a scripted fake adapter (pytest-qt, offscreen).
- Conventional commits; NO Co-Authored-By / AI-attribution trailers (hard rule).
- Suite green after every task: `.venv/bin/python -m pytest tests/ -q` (649 passed, 2 skipped before this plan) and `python3 -m pytest tests/ -q` (GUI skipped).
- README GUI section gains the grill flow in the same plan (memory rule).

---

### Task 1: `GrillSession` facade + `_GrillWorker` + prompt template + option parser (Opus)

**Files:**
- Create: `spar/gui/grill.py`
- Test: `tests/test_gui_grill.py` (new; `pytest.importorskip("PySide6")` for the Qt parts, parser tests pure)

**Interfaces:**
- `OPENING_PROMPT_TEMPLATE` — module constant, verbatim:

  ```
  Użyj skilla grill-with-docs dla tego projektu. Zadanie do wygrillowania:
  "{draft}".
  Zadawaj pytania POJEDYNCZO, każde z opcjami oznaczonymi LITERAMI (A., B., C., ...)
  i Twoją rekomendacją — ja odpowiadam w kolejnych wiadomościach. Gdy uznasz
  wymagania za kompletne, zapisz finalne wymagania do .spar/requirements.md
  (pełna treść zadania dla dwustronnej debaty, zakończona wymaganiem sekcji
  ## Tasks) i napisz GOTOWE.
  ```

- `@dataclass(frozen=True) Option: letter: str; label: str` (label = FULL text; any truncation happens dialog-side only — review #4) and pure `parse_options(reply_text: str) -> list[Option]` — BLOCK-based (review #3): scan line-by-line with `^[-*\s]*\**([A-H])[.)]\**\s*(.+)$`; consecutive matching lines (allowing single blank/continuation lines between them) form a BLOCK; collect all blocks, keep the LAST block whose letters form a contiguous run from 'A' (an earlier block's stale `C` can never leak into a later `A/B` block). Label cleanup strips ALL `**` markers anywhere in the captured text (mid-line closers included: `Implicit fallback** — ...` → `Implicit fallback — ...`) and trims. Returns `[]` when no valid block.
- `GrillSession(QObject)` — the GUI-THREAD FACADE (review #6): it owns a private `_GrillWorker(QObject)` moved to a persistent `QThread`; `start/answer/stop` only emit private queued signals to the worker; the worker performs `run_turn` and reports back via queued signals; NO shared mutable state is touched from both threads (stop-suppression is FACADE-side via a generation token: the facade stamps each dispatched turn with `self._generation`; `stop()` (GUI thread) increments the generation; EVERY worker→facade signal — turn results, failures AND `stream_chunk` lines — carries the stamp internally and the facade DROPS anything stale before re-emitting on its public signals — this works even while the worker is blocked inside `run_turn`, where a queued worker-side slot could not run (review #10)). Constructor `(project_dir: Path, side_cfg: SideConfig, timeout_sec: int, adapter_factory=None)` (factory injectable for tests; default builds `ClaudeAdapter(command=side_cfg.command, model=side_cfg.debate_model or side_cfg.model or side_cfg.default_model, cwd=project_dir, events_dir=project_dir/'.spar'/'transcript', side_name='grill')` — `command` MUST be passed; a custom claude binary must grill through the same binary it debates with, review #1). API:
  - `start(draft: str)` — first turn with the template;
  - `answer(text: str)` — next turn via `--resume` (stored session id);
  - `stop()` — abandon (worker thread quits after any in-flight turn; further signals suppressed).
  Signals: `stream_chunk(str)`; `turn_finished(str reply_text, list options)`; `requirements_ready(str content)` — detection is CONTENT-based (review #5): at session start snapshot `(exists, st_mtime_ns, size, sha256)` of `.spar/requirements.md`; after EVERY completed turn read the file and compare the content hash against the snapshot — created OR changed content emits the signal with the new content (no wall-clock comparisons; robust on coarse-mtime filesystems and against a pre-existing file); `turn_failed(str message)` (AdapterError/timeout — session alive, the same answer may be retried via "Ponów"); `session_lost()` (review #2 — `ClaudeAdapter.run_turn` raises `SessionLost`, not AdapterError, when a RESUME fails: the stored session id is dead, plain retry would fail again; the dialog must offer "Restart grilla" — a fresh `start(draft)` with a new session — instead of Ponów).
- [ ] **Step 1: failing parser tests** — the spike's real turn-1 shapes incl. the mid-line bold closer (`- **B. Implicit fallback** — ...` → label `Implicit fallback — ...` without `**`), plain `A. foo` lines, no-options reply → `[]`, non-contiguous letters ignored, TWO blocks in one reply → only the LAST block's options returned (stale-letter leak regression).
- [ ] **Step 2: failing session tests** (fake adapter: scripted replies, records prompts/session ids/on_event): `start` sends the template with the draft embedded and emits `turn_finished` with parsed options; `answer` resumes with the stored session id; a scripted fake writing `requirements.md` before replying triggers `requirements_ready` with the file content; `turn_failed` on an AdapterError-raising fake, then `answer` retry works; `session_lost` on a SessionLost-raising fake (resume path) and a subsequent `start(draft)` opens a FRESH session (fake records session_id=None); `stop()` prevents further PUBLIC signal emission even when called mid-turn — including late `stream_chunk` lines emitted by the in-flight turn after the stop (generation-token drop; the fake adapter blocks on an event, emits on_event chunks after release, and the test asserts no public stream_chunk/turn_finished fires post-stop); requirements detection: pre-existing requirements.md unchanged → NO signal; content changed → signal (content-hash test). Use `qtbot.waitSignal`.
- [ ] **Step 3: implement; PASS both interpreters.**
- [ ] **Step 4: commit** — `feat(gui): GrillSession — threaded grill-with-docs conversation over the claude adapter`

---

### Task 2: `GrillDialog` chat UI + new-debate integration (Sonnet)

**Files:**
- Modify: `spar/gui/grill.py` (dialog part) or create `spar/gui/grill_dialog.py` (implementer's choice; keep one module if < ~400 lines)
- Modify: `spar/gui/toolbar.py` (`NewDebateDialog` gains the „Grilluj z modelem…" button)
- Test: `tests/test_gui_grill.py`

**Interfaces:**
- `GrillDialog(project_dir, side_cfg, timeout_sec, draft, parent=None)` — modal, `resize(860, 640)`, dark theme via existing QSS. Layout: chat transcript (read-only `QTextBrowser`-style view: user turns right-aligned/accent, model turns left; the in-flight model turn streams live via `stream_chunk` into the current bubble), below: dynamic **option buttons row** (from `turn_finished` options; clicking sends `answer("<letter>")` and disables the row) + multiline input + „Wyślij" (Ctrl+Enter) + „Ponów" (visible only after `turn_failed`); after `session_lost` instead: banner „sesja utracona" + „Restart grilla" button (fresh `start(draft)`). Status strip: „model myśli…" spinner while a turn is in flight (send controls disabled). On `requirements_ready`: banner „Wymagania gotowe" + primary button **„Użyj w debacie"** → `accept()`; `result_requirements: str | None` property carries the content. Close/Anuluj mid-grill → `session.stop()`, reject.
- `NewDebateDialog`: button „Grilluj z modelem…" next to the task field; on click constructs `GrillDialog(..., draft=<current task text>)`; on accept REPLACES the task field with `result_requirements`. Config/side resolution: claude side from `load_config(project_dir)`; timeout from `config.debate.turn_timeout_sec`. Button disabled with a tooltip when config LOADING fails or the claude side's adapter is not `claude` (review #7 — a missing side is unreachable: defaults always seed claude/codex; test the disable via monkeypatched load_config).
- [ ] **Step 1: failing tests** (fake session injected): options render as buttons and clicking emits `answer("B")`; free-text send path; streaming chunks append to the live bubble (assert document text grows); `requirements_ready` → „Użyj w debacie" enabled → accept carries content; mid-grill close calls `stop()`; NewDebateDialog integration: grill accept replaces the task text (GrillDialog mocked).
- [ ] **Step 2: implement; PASS.**
- [ ] **Step 3: commit** — `feat(gui): GrillDialog chat + new-debate integration (grill-with-docs in the window)`

---

### Task 3: docs + manual smoke (Sonnet + user)

- [ ] **Step 1:** README `spar gui` section: the grill flow (one short paragraph + the flow line); note that grilling requires the user's claude CLI with the grill-with-docs skill installed (`~/.claude/skills`); HANDOFF updated.
- [ ] **Step 2:** verify docs commands/paths; suite green.
- [ ] **Step 3: commit** — `docs: grill-with-docs flow in spar gui`
- [ ] **Step 4 (manual, user):** live smoke in spar_test_2: Nowa debata → szkic → Grilluj → odpowiedz 2-3 pytania (przyciski + własny tekst) → Użyj w debacie → debata startuje z wygrillowaną treścią. Record findings.

---

## Self-Review Notes

- ADR 0004 honored: GUI module only, engine contract untouched, host agent's skill does the thinking.
- The riskiest piece (threaded adapter turns + signal marshalling) is Task 1 on Opus with an injectable adapter factory so tests never spawn a real CLI.
- Spike artifacts (real turn shapes) drive the parser tests — fixtures from reality, per the external review's guidance.
- Turn cost is visible to the user implicitly (each answer = one CLI turn); token counters remain a separate backlog item.

## Review history

- **Round 1** (codex gpt-5.5): Verdict CONTINUE. All seven **accepted**:
  #1 [MUST] adapter factory now passes `command=side_cfg.command` (custom
  claude binaries grill through the same binary they debate with).
  #2 [MUST] `SessionLost` (resume failure) gets its own `session_lost` signal
  and a "Restart grilla" path — plain retry would re-fail on a dead session.
  #3 [MUST] option parser is block-based (last contiguous-from-A block wins;
  no stale-letter leaks) and strips mid-line `**` closers.
  #4 [MUST] `Option.label` holds the full text; truncation is dialog-side.
  #5 [MUST] requirements detection is content-hash based against a
  session-start snapshot (no wall-clock/mtime races; pre-existing file safe).
  #6 [MUST] threading contract sharpened: GUI-thread facade + private worker
  moved to QThread, all interaction via queued signals.
  #7 [NICE] disable condition redefined to config-load failure / non-claude
  adapter (missing side unreachable with current defaults).
- **Round 2** (codex gpt-5.5): Verdict CONTINUE. #8 [MUST] **accepted** —
  the architecture summary still said "mtime watch"; aligned with Task 1's
  content-hash detection.
- **Round 3** (codex gpt-5.5): Verdict CONTINUE. #9 [MUST] **accepted** —
  the architecture summary still called GrillSession a "worker"; aligned
  with #6's facade/worker split.
- **Round 4** (codex gpt-5.5): Verdict CONTINUE. #10 [MUST] **accepted** —
  a queued worker-side stop flag cannot run while the worker is blocked in
  `run_turn`; replaced with facade-side generation-token suppression (stale
  results dropped in the GUI thread) + a blocking-fake test. #11 [NICE]
  **accepted** — Task 1 heading updated to the facade/worker split.
- **Round 5** (codex gpt-5.5): Verdict CONTINUE. #12 [MUST] **accepted** —
  the opening prompt now REQUIRES lettered options (A., B., C., ...), matching
  the parser/dialog contract (numbered options would silently disable the
  button flow). #13 [MUST] **accepted** — the generation stamp explicitly
  covers EVERY worker→facade signal including stream_chunk; the stop test
  asserts no public signal (chunks included) fires post-stop.
- **Round 6** (codex gpt-5.5, verification): Verdict **AGREE**. Confirmed
  #12/#13 and all prior fixes consistent document-wide.
