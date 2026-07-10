# GrillPane — requirements grilling inside the GUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** In `spar gui`, the user grills a task with the model in a chat window (question shown, answer box below, option buttons for A/B/C choices), driven by a real claude session running the user's grill-with-docs skill; the session ends by writing `.spar/requirements.md`, whose content pre-fills the new-debate task field. Per ADR 0004 (GUI module; engine untouched).

**Feasibility retired by live spike (2026-07-10, spar_test_2):** the skill loads in `-p` mode via our `ClaudeAdapter`; the session holds context across `--resume` turns; questions arrive with lettered options + a recommendation; the final turn writes a well-structured `.spar/requirements.md`. The plan composes existing bricks only.

**Architecture:** New `spar/gui/grill.py`: a `GrillSession(QObject)` worker owning one `ClaudeAdapter` conversation (model = claude side's `debate_model or model or default_model`, cwd = project_dir, events to `.spar/transcript/grill-*`), each turn executed OFF the GUI thread (a `QThread`-hosted worker; `run_turn` blocks for up to minutes) with the adapter's `on_event` streamed into the chat live; a pure `parse_options(reply) -> list[Option]` extracting lettered choices for buttons; finish = `.spar/requirements.md` created/updated by the model (mtime watch after each turn) → "Użyj w debacie" button. UI = modal `GrillDialog` opened from the new-debate dialog: the user's draft task ("szkic") seeds the opening prompt; on finish the requirements content replaces the task field.

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

### Task 1: `GrillSession` worker + prompt template + option parser (Opus)

**Files:**
- Create: `spar/gui/grill.py`
- Test: `tests/test_gui_grill.py` (new; `pytest.importorskip("PySide6")` for the Qt parts, parser tests pure)

**Interfaces:**
- `OPENING_PROMPT_TEMPLATE` — module constant, verbatim:

  ```
  Użyj skilla grill-with-docs dla tego projektu. Zadanie do wygrillowania:
  "{draft}".
  Zadawaj pytania POJEDYNCZO, każde z ponumerowanymi/oliterowanymi opcjami
  i Twoją rekomendacją — ja odpowiadam w kolejnych wiadomościach. Gdy uznasz
  wymagania za kompletne, zapisz finalne wymagania do .spar/requirements.md
  (pełna treść zadania dla dwustronnej debaty, zakończona wymaganiem sekcji
  ## Tasks) i napisz GOTOWE.
  ```

- `@dataclass(frozen=True) Option: letter: str; label: str` and pure `parse_options(reply_text: str) -> list[Option]`: extracts lettered options from the LAST question block — accepted shapes (each at line start, optional list dash/bold): `- **A. label**`, `- A. label`, `**A.** label`, `A. label`, `- **A) label**` etc. — regex on `^[-*\s]*\**([A-H])[.)]\**\s*(.+)$` per line, de-duplicated by letter keeping the last occurrence set that forms a contiguous run from 'A'; labels stripped of trailing `**`, truncated to 80 chars for button text (full label kept on the Option). Returns `[]` when no options (free-text-only question).
- `GrillSession(QObject)` — constructor `(project_dir: Path, side_cfg: SideConfig, timeout_sec: int, adapter_factory=None)` (factory injectable for tests; default builds `ClaudeAdapter(model=side_cfg.debate_model or side_cfg.model or side_cfg.default_model, cwd=project_dir, events_dir=project_dir/'.spar'/'transcript', side_name='grill')`). API:
  - `start(draft: str)` — first turn with the template;
  - `answer(text: str)` — next turn via `--resume` (stored session id);
  - `stop()` — abandon (worker thread quits after any in-flight turn; further signals suppressed).
  Signals: `stream_chunk(str)` (live on_event lines), `turn_finished(str reply_text, list options)` (options = `parse_options(reply)`), `requirements_ready(str content)` (emitted when `.spar/requirements.md` exists and its mtime changed since the session started — checked after every turn), `turn_failed(str message)` (AdapterError/timeout — the session stays alive; the same answer may be retried).
  Threading: one persistent `QThread`; turns dispatched to the worker via queued signal; results marshalled back via queued signals (document the pattern; NO direct cross-thread widget access).
- [ ] **Step 1: failing parser tests** — the spike's real turn-1 shapes (`- **A. Explicit ...**`, `- **B. Implicit fallback** — ...`, `- **C. Always merge** ...` → A/B/C with labels), plain `A. foo` lines, no-options reply → `[]`, non-contiguous letters ignored, 80-char truncation.
- [ ] **Step 2: failing session tests** (fake adapter: scripted replies, records prompts/session ids/on_event): `start` sends the template with the draft embedded and emits `turn_finished` with parsed options; `answer` resumes with the stored session id; a scripted fake writing `requirements.md` before replying triggers `requirements_ready` with the file content; `turn_failed` on a raising fake, then `answer` retry works; `stop()` prevents further signal emission. Use `qtbot.waitSignal`.
- [ ] **Step 3: implement; PASS both interpreters.**
- [ ] **Step 4: commit** — `feat(gui): GrillSession — threaded grill-with-docs conversation over the claude adapter`

---

### Task 2: `GrillDialog` chat UI + new-debate integration (Sonnet)

**Files:**
- Modify: `spar/gui/grill.py` (dialog part) or create `spar/gui/grill_dialog.py` (implementer's choice; keep one module if < ~400 lines)
- Modify: `spar/gui/toolbar.py` (`NewDebateDialog` gains the „Grilluj z modelem…" button)
- Test: `tests/test_gui_grill.py`

**Interfaces:**
- `GrillDialog(project_dir, side_cfg, timeout_sec, draft, parent=None)` — modal, `resize(860, 640)`, dark theme via existing QSS. Layout: chat transcript (read-only `QTextBrowser`-style view: user turns right-aligned/accent, model turns left; the in-flight model turn streams live via `stream_chunk` into the current bubble), below: dynamic **option buttons row** (from `turn_finished` options; clicking sends `answer("<letter>")` and disables the row) + multiline input + „Wyślij" (Ctrl+Enter) + „Ponów" (visible only after `turn_failed`). Status strip: „model myśli…" spinner while a turn is in flight (send controls disabled). On `requirements_ready`: banner „Wymagania gotowe" + primary button **„Użyj w debacie"** → `accept()`; `result_requirements: str | None` property carries the content. Close/Anuluj mid-grill → `session.stop()`, reject.
- `NewDebateDialog`: button „Grilluj z modelem…" next to the task field; on click constructs `GrillDialog(..., draft=<current task text>)`; on accept REPLACES the task field with `result_requirements`. Config/side resolution: claude side from `load_config(project_dir)`; timeout from `config.debate.turn_timeout_sec`. When the claude side is missing from config → button disabled with a tooltip.
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
