# Orchestrator chat + tool-window rails Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every task title carries a model assignment per the user's global rule — do not execute a task on a weaker model than assigned.

**Goal:** Deliver ADR 0005 in `spar gui`: (1) extract a shared `ConversationSession` from `GrillSession` so grill and a new docked orchestrator chat are two thin instances of the same adapter-backed multi-turn session; (2) add JetBrains-style vertical icon rails on both window edges that toggle collapsible right-column tool panels (Taski / Czat + a Bramka gate icon), with collapse state persisted in QSettings and "everything collapsed → stream full width"; (3) build the orchestrator chat as a **read-only advisor** panel (bubbles, tool lines, lettered-option buttons, free-text send), persistent across GUI restarts via `.spar/chat.json` (session resume), silently fed gate context when a gate pends, and able to hand a drafted task off to a prefilled `NewDebateDialog`. Engine untouched (ADR 0003/0004 boundary); everything under `spar/gui/`.

**Architecture:**
- **`spar/gui/conversation.py` (new).** Houses the Qt-free `Option` / `parse_options` (moved verbatim from `grill.py`, re-exported there for back-compat) plus a shared Qt layer: `_ConversationWorker(QObject)` (owns one `ClaudeAdapter` conversation on a persistent `QThread`; runs one blocking `run_turn` per dispatch; emits generation-stamped `chunk`/`finished`/`failed`/`lost`; a `_post_turn(result)` hook returns a subclass-specific "extra" payload computed on the worker thread) and `ConversationSession(QObject)` (GUI-thread facade: generation-token stop-suppression of ALL signals incl. `stream_chunk`, `_ABANDONED_THREADS` retention, session-resume via a stored session id, `send(text, reset)` public dispatch, `session_id` property). The module-level `_ABANDONED_THREADS` set lives here.
- **`spar/gui/grill.py` (refactored).** `GrillSession(ConversationSession)` — a thin subclass: overrides `_make_worker` (→ `_GrillWorker` with requirements.md content-hash detection), `_default_adapter_factory` (`side_name="grill"`), `_handle_extra` (emits `requirements_ready`), and keeps `start(draft)` / `answer(text)` wrappers over `send`. `OPENING_PROMPT_TEMPLATE`, `Option`, `parse_options`, `_ABANDONED_THREADS` stay importable from `grill` (re-exports) so existing tests are untouched.
- **`spar/gui/orchestrator.py` (new).** `OrchestratorSession(ConversationSession)` (read-only claude adapter, `side_name="orchestrator"`, `readonly=True`; `initial_session_id` for resume) and `OrchestratorChatPanel(QWidget)` — the docked chat view (bubbles, `tool:` lines rendered dim/monospace inside bot bubbles, lettered-option buttons reusing the grill vertical pattern, free-text input always available, header line, RUNNING read-only banner, gate-context injection, handoff button). Pure helpers here too: `OPENING_PROMPT`, `parse_task_draft`, `build_gate_context`.
- **`spar/gui/chat_store.py` (new).** Pure `.spar/chat.json` load/save (`ChatMeta` dataclass; corrupt/missing → `None`). No Qt.
- **`spar/gui/rails.py` (new).** `IconRail(QWidget)` — a vertical strip of `QToolButton`s; pure `RailLayoutState` helper deciding right-column/splitter visibility from the toggle booleans. QSettings-namespaced keys (`rails/tasks_visible`, `rails/chat_visible`).
- **`spar/gui/app.py` (refactored layout).** Central widget becomes `[left IconRail][ QSplitter(stream | RightColumn) ][right IconRail]`. `RightColumn(QWidget)` stacks `SidePane` (Taski + GatePanel) over `OrchestratorChatPanel`. Rails toggle panel visibility; all-right-collapsed hides the column so the stream stretches full width; the 1.7:1 splitter ratio is restored whenever the column reappears.

**Flow (orchestrator chat):** GUI opens → right rail shows Taski (on) + Czat (on/off per QSettings) → user types a question → OrchestratorSession resumes the persisted claude session (`.spar/chat.json`) and answers with bubbles/options → during a live run a yellow "run w toku — tylko odczyt" banner shows but the chat stays usable → when a gate pends the next question silently carries the gate context so the user can ask "co byś wybrał" (the Bramka rail icon lights with an attention dot; the actual decision is only ever taken in the GatePanel) → if the model emits a ` ```zadanie … ``` ` block a green "Nowa debata z tym szkicem" button opens a prefilled `NewDebateDialog` (only while the engine is free).

**Tech Stack:** Python 3.12, PySide6 (existing `gui` extra), pytest-qt (offscreen). Engine consumed only via the public `Adapter.run_turn(prompt, session_id, timeout_sec, on_event)` contract and `spar.status.build_status`. No engine/protocol changes.

## Global Constraints

- **Everything under `spar/gui/`.** Nothing in `spar/` outside `spar/gui/` may change. No new engine states, gates, or protocol (ADR 0003/0004/0005).
- **The orchestrator chat is a read-only advisor.** Its adapter runs with `readonly=True` (only the `Read` tool). The opening prompt explicitly forbids file edits and gate decisions. The GUI exposes NO gate action through the chat — the GatePanel stays the only decision pilot.
- **GUI thread never blocks.** Every `run_turn` runs on the worker `QThread`; all UI mutation happens via queued signal connections. Closing a panel/window mid-turn abandons the thread cleanly via `_ABANDONED_THREADS`.
- **Grill stays green through the refactor.** After Task 1, `tests/test_gui_grill.py` passes unchanged — `GrillSession`, `Option`, `parse_options`, `OPENING_PROMPT_TEMPLATE`, and `_ABANDONED_THREADS` remain importable from `spar.gui.grill` with identical public behavior.
- **Suite green at every task boundary:** `.venv/bin/python -m pytest tests/ -q` (baseline 750 passed, 2 skipped) AND `python3 -m pytest tests/ -q` (GUI tests skipped — pure pieces must pass with no PySide6). GUI tests run offscreen (`QT_QPA_PLATFORM=offscreen`, provided by the existing `qtbot`/conftest setup).
- **TDD:** failing test first for every pure helper and every wiring path; scripted fake adapters/sessions for Qt (never a real claude subprocess in tests).
- **Conventional commits; NO Co-Authored-By / AI-attribution trailers (hard rule).**
- **README updated in the same change set as any user-visible GUI surface change** (rails, chat, handoff) — covered by the final docs task, which also adds a HANDOFF entry and a screenshots note and flips ADR 0005's implementation status.

---

### Task 1: Extract `ConversationSession` from `GrillSession` (Opus)

The hardest task: a behavior-preserving refactor of a threaded, generation-token, abandoned-thread state machine, under an existing test suite that must stay 100% green.

**Files:**
- Create: `spar/gui/conversation.py`
- Modify: `spar/gui/grill.py` (becomes a thin subclass + re-exports)
- Create: `tests/test_gui_conversation.py`
- Unchanged (must stay green): `tests/test_gui_grill.py`

**Interfaces:**

Produces (`spar/gui/conversation.py`):
- `@dataclass(frozen=True) Option(letter: str, label: str)` — moved verbatim from `grill.py`.
- `parse_options(reply_text: str) -> list[Option]` — moved verbatim (block-based, contiguous-from-A, `_OPTION_RE`, `_clean_label`).
- `_ABANDONED_THREADS: set[QThread]` — module-level retain set.
- `_ConversationWorker(QObject)`:
  - signals `chunk = Signal(int, str)`, `finished = Signal(int, str, str, object)` (gen, reply_text, session_id, extra), `failed = Signal(int, str)`, `lost = Signal(int)`.
  - `__init__(self, adapter_factory: Callable[[], object], project_dir: Path, timeout_sec: int, initial_session_id: str | None = None)`.
  - `run_turn(self, generation: int, prompt: str, reset_session: bool) -> None` — slot; on success emits `finished(generation, result.reply_text, result.session_id, self._post_turn(result))`.
  - `_post_turn(self, result) -> object` — hook, base returns `None`.
- `ConversationSession(QObject)`:
  - signals `stream_chunk = Signal(str)`, `turn_finished = Signal(str, list)`, `turn_failed = Signal(str)`, `session_lost = Signal()`, private `_dispatch = Signal(int, str, bool)`.
  - `__init__(self, project_dir, side_cfg, timeout_sec, adapter_factory=None, initial_session_id=None, parent=None)`.
  - `send(self, text: str, reset: bool = False) -> None` — dispatch a turn (`reset=True` starts fresh).
  - `stop(self) -> None` — generation++ + abandoned-thread handoff (moved verbatim).
  - `session_id` property (last committed id; seeded from `initial_session_id`).
  - hooks: `_make_worker(self, adapter_factory, project_dir, timeout_sec, initial_session_id) -> _ConversationWorker` (base returns `_ConversationWorker(...)`); `_default_adapter_factory(self)` (base raises `NotImplementedError`); `_handle_extra(self, extra) -> None` (base no-op).

Produces (`spar/gui/grill.py`, re-exported): `OPENING_PROMPT_TEMPLATE`, `Option`, `parse_options`, `_ABANDONED_THREADS`, `GrillSession`.

- [ ] **Step 1: failing test — base session round-trips via a concrete subclass.** Add `tests/test_gui_conversation.py`. Mirror `test_gui_grill.py`'s FakeAdapter (`run_turn(prompt, session_id, timeout_sec, on_event)`, scripted steps) and `make_session`-style fixture, but build a minimal concrete subclass in the test:

  ```python
  """Tests for the shared spar.gui.conversation layer.

  Pure parse_options coverage lives in test_gui_grill.py (re-exported symbol);
  here we test the base ConversationSession via a minimal concrete subclass and
  a scripted fake adapter (no real claude subprocess).
  """
  from __future__ import annotations

  from pathlib import Path
  from types import SimpleNamespace

  import pytest

  try:
      import PySide6  # noqa: F401

      from spar.adapters.base import AdapterError, SessionLost, TurnResult
      from spar.config import SideConfig
      from spar.gui.conversation import ConversationSession, Option

      _HAS_QT = True
  except ImportError:  # pragma: no cover
      _HAS_QT = False


  def _reply(text, session_id="sess-1", chunks=None):
      def _step(prompt, sid, on_event):
          for c in chunks or []:
              if on_event:
                  on_event(c)
          return TurnResult(
              session_id=session_id, reply_text=text,
              events_path=Path("events.json"), exit_code=0,
          )
      return _step


  def _raise(exc):
      def _step(prompt, sid, on_event):
          raise exc
      return _step


  if _HAS_QT:

      class FakeAdapter:
          name = "claude"

          def __init__(self, steps):
              self.steps = list(steps)
              self.calls = []
              self._idx = 0

          def run_turn(self, prompt, session_id, timeout_sec, on_event=None):
              self.calls.append(
                  SimpleNamespace(prompt=prompt, session_id=session_id,
                                  timeout_sec=timeout_sec, on_event=on_event)
              )
              step = self.steps[self._idx]
              self._idx += 1
              return step(prompt, session_id, on_event)

      class _ProbeSession(ConversationSession):
          """Minimal concrete subclass: records extras it was handed."""

          def __init__(self, *a, **k):
              self.extras = []
              super().__init__(*a, **k)

          def _default_adapter_factory(self):  # pragma: no cover - unused (factory injected)
              raise AssertionError("test always injects an adapter_factory")

          def _handle_extra(self, extra):
              self.extras.append(extra)


  @pytest.fixture
  def make_probe(qtbot):
      created = []

      def _make(project_dir, adapter, timeout_sec=60, initial_session_id=None):
          sess = _ProbeSession(
              Path(project_dir), SideConfig(adapter="claude", command="claude"),
              timeout_sec, adapter_factory=lambda: adapter,
              initial_session_id=initial_session_id,
          )
          created.append(sess)
          return sess

      yield _make
      for sess in created:
          sess.stop()
          try:
              sess._thread.wait(3000)
          except RuntimeError:
              pass


  @pytest.mark.skipif(not _HAS_QT, reason="requires PySide6")
  class TestConversationSession:
      def test_send_fresh_then_resume_tracks_session_id(self, tmp_path, qtbot, make_probe):
          adapter = FakeAdapter([_reply("A. a\nB. b", session_id="s1"),
                                 _reply("ok", session_id="s2")])
          sess = make_probe(tmp_path, adapter)
          with qtbot.waitSignal(sess.turn_finished, timeout=3000) as b:
              sess.send("hello", reset=True)
          assert b.args[1] == [Option("A", "a"), Option("B", "b")]
          assert adapter.calls[0].session_id is None
          assert sess.session_id == "s1"
          with qtbot.waitSignal(sess.turn_finished, timeout=3000):
              sess.send("more")
          assert adapter.calls[1].session_id == "s1"
          assert sess.session_id == "s2"

      def test_initial_session_id_resumes_on_first_send(self, tmp_path, qtbot, make_probe):
          adapter = FakeAdapter([_reply("ok", session_id="s9")])
          sess = make_probe(tmp_path, adapter, initial_session_id="restored")
          with qtbot.waitSignal(sess.turn_finished, timeout=3000):
              sess.send("q")  # reset defaults False -> resumes the restored id
          assert adapter.calls[0].session_id == "restored"

      def test_stream_chunks_reach_public_signal(self, tmp_path, qtbot, make_probe):
          adapter = FakeAdapter([_reply("x", chunks=["a", "b"])])
          sess = make_probe(tmp_path, adapter)
          got = []
          sess.stream_chunk.connect(got.append)
          with qtbot.waitSignal(sess.turn_finished, timeout=3000):
              sess.send("q", reset=True)
          assert got == ["a", "b"]

      def test_session_lost_signal(self, tmp_path, qtbot, make_probe):
          adapter = FakeAdapter([_reply("x", session_id="s1"),
                                 _raise(SessionLost("dead"))])
          sess = make_probe(tmp_path, adapter)
          with qtbot.waitSignal(sess.turn_finished, timeout=3000):
              sess.send("q", reset=True)
          with qtbot.waitSignal(sess.session_lost, timeout=3000):
              sess.send("q2")

      def test_turn_failed_signal(self, tmp_path, qtbot, make_probe):
          adapter = FakeAdapter([_raise(AdapterError("boom"))])
          sess = make_probe(tmp_path, adapter)
          with qtbot.waitSignal(sess.turn_failed, timeout=3000) as f:
              sess.send("q", reset=True)
          assert "boom" in f.args[0]

      def test_stop_from_turn_finished_subscriber_suppresses_extra(
          self, tmp_path, qtbot, make_probe
      ):
          # Review #9 re-entrancy hole: a subscriber that calls stop() from
          # inside turn_finished must NOT then receive _handle_extra for the
          # abandoned generation. The _ProbeSession records every extra it is
          # handed; after a stop() re-entrant on turn_finished it must record
          # none.
          adapter = FakeAdapter([_reply("done", session_id="s1")])
          sess = make_probe(tmp_path, adapter)
          sess.turn_finished.connect(lambda *_: sess.stop())
          with qtbot.waitSignal(sess.turn_finished, timeout=3000):
              sess.send("q", reset=True)
          qtbot.wait(50)
          assert sess.extras == []  # extra suppressed after the re-entrant stop()
  ```

  Run: `.venv/bin/python -m pytest tests/test_gui_conversation.py -q` → **FAILS** (`spar.gui.conversation` does not exist).

- [ ] **Step 2: implement `spar/gui/conversation.py`.** Move the Qt-free `Option`/`parse_options` here verbatim, then the shared Qt layer:

  ```python
  """Shared adapter-backed multi-turn chat session (grill + orchestrator).

  Qt-free pieces (Option / parse_options) live at the top so they import on any
  interpreter. The Qt layer below is a GUI-thread FACADE (ConversationSession)
  whose private _ConversationWorker is moved onto a persistent QThread and owns
  the one ClaudeAdapter conversation. Stop-suppression is FACADE-side via a
  generation token: the facade stamps every dispatched turn with the current
  generation, stop() (GUI thread) increments it, and EVERY worker->facade signal
  carries that stamp so the facade drops anything stale before re-emitting. Grill
  and the orchestrator chat are thin subclasses differing only in adapter,
  post-turn payload, and opening prompt.
  """
  from __future__ import annotations

  import re
  from dataclasses import dataclass
  from pathlib import Path
  from typing import Callable, Optional  # noqa: F401


  @dataclass(frozen=True)
  class Option:
      """A single lettered answer choice extracted from a model reply.

      ``label`` holds the FULL text of the choice — any truncation for display
      is a view-side concern, never applied here.
      """

      letter: str
      label: str


  _OPTION_RE = re.compile(r"^[-*\s]*\**([A-H])[.)]\**\s*(.+)$")


  def _clean_label(text: str) -> str:
      """Strip ALL ``**`` markers (mid-line closers included) and trim."""
      return text.replace("**", "").strip()


  def parse_options(reply_text: str) -> list[Option]:
      """Extract the active lettered-option block from a model reply.

      Block-based: option lines that are consecutive — allowing at most one
      intervening blank/continuation line — form a BLOCK. Two or more non-option
      lines in a row break the block. Among all blocks, the LAST one whose
      letters form a contiguous run from ``A`` wins. Returns ``[]`` when none.
      """
      blocks: list[list[Option]] = []
      current: list[Option] = []
      gap = 0
      for raw in reply_text.splitlines():
          m = _OPTION_RE.match(raw)
          if m:
              if current and gap > 1:
                  blocks.append(current)
                  current = []
              current.append(Option(letter=m.group(1), label=_clean_label(m.group(2))))
              gap = 0
          else:
              gap += 1
      if current:
          blocks.append(current)

      def contiguous_from_a(block: list[Option]) -> bool:
          return all(opt.letter == chr(ord("A") + i) for i, opt in enumerate(block))

      for block in reversed(blocks):
          if contiguous_from_a(block):
              return block
      return []


  try:  # pragma: no cover - exercised via the two interpreters
      from PySide6.QtCore import QObject, QThread, Qt, Signal

      _HAS_QT = True
  except ImportError:  # pragma: no cover
      _HAS_QT = False


  if _HAS_QT:
      from spar.adapters.base import AdapterError, SessionLost

      # Threads abandoned by ConversationSession.stop() while a turn is still
      # blocked inside run_turn. A strong module-level reference keeps the
      # QThread (and worker) alive until run_turn returns; a finished-connected
      # callback then tears it down. (Destroying a running QThread => qFatal.)
      _ABANDONED_THREADS: set[QThread] = set()

      class _ConversationWorker(QObject):
          """Owns the single adapter conversation; lives on a worker QThread.

          Every outgoing signal carries the generation stamp it was dispatched
          with so the facade can drop stale results after a ``stop()``.
          """

          chunk = Signal(int, str)                 # gen, text
          finished = Signal(int, str, str, object)  # gen, reply_text, session_id, extra
          failed = Signal(int, str)                # gen, message
          lost = Signal(int)                       # gen

          def __init__(
              self,
              adapter_factory: Callable[[], object],
              project_dir: Path,
              timeout_sec: int,
              initial_session_id: str | None = None,
          ) -> None:
              super().__init__()
              self._adapter = adapter_factory()
              self._project_dir = Path(project_dir)
              self._timeout_sec = timeout_sec
              self._session_id: str | None = initial_session_id

          def run_turn(self, generation: int, prompt: str, reset_session: bool) -> None:
              """Slot: execute one turn (blocks the worker's event loop)."""
              if reset_session:
                  self._session_id = None

              def on_event(line: str) -> None:
                  self.chunk.emit(generation, line)

              try:
                  result = self._adapter.run_turn(
                      prompt, self._session_id, self._timeout_sec, on_event
                  )
              except SessionLost:
                  self._session_id = None
                  self.lost.emit(generation)
                  return
              except AdapterError as exc:
                  self.failed.emit(generation, str(exc))
                  return
              except Exception as exc:  # defensive: never crash the worker thread
                  self.failed.emit(generation, str(exc))
                  return

              self._session_id = result.session_id
              extra = self._post_turn(result)
              self.finished.emit(
                  generation, result.reply_text, result.session_id or "", extra
              )

          def _post_turn(self, result) -> object:
              """Hook: subclass-computed extra payload (worker thread). Base: None."""
              return None

      class ConversationSession(QObject):
          """GUI-thread facade driving one adapter-backed multi-turn conversation."""

          stream_chunk = Signal(str)
          turn_finished = Signal(str, list)  # reply_text, options
          turn_failed = Signal(str)          # message (retryable)
          session_lost = Signal()            # resume died; caller must send(reset=True)

          _dispatch = Signal(int, str, bool)  # gen, prompt, reset_session

          def __init__(
              self,
              project_dir: "str | Path",
              side_cfg,
              timeout_sec: int,
              adapter_factory: Callable[[], object] | None = None,
              initial_session_id: str | None = None,
              parent: QObject | None = None,
          ) -> None:
              super().__init__(parent)
              self._project_dir = Path(project_dir)
              self._side_cfg = side_cfg
              self._timeout_sec = timeout_sec
              self._generation = 0
              self._session_id: str | None = initial_session_id

              if adapter_factory is None:
                  adapter_factory = self._default_adapter_factory

              self._thread = QThread()
              self._worker = self._make_worker(
                  adapter_factory, self._project_dir, timeout_sec, initial_session_id
              )
              self._worker.moveToThread(self._thread)

              self._dispatch.connect(
                  self._worker.run_turn, Qt.ConnectionType.QueuedConnection
              )
              self._worker.chunk.connect(self._on_chunk, Qt.ConnectionType.QueuedConnection)
              self._worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
              self._worker.failed.connect(self._on_failed, Qt.ConnectionType.QueuedConnection)
              self._worker.lost.connect(self._on_lost, Qt.ConnectionType.QueuedConnection)
              self._thread.finished.connect(self._worker.deleteLater)
              self._thread.start()

          # -- hooks (override in subclasses) -------------------------------
          def _make_worker(self, adapter_factory, project_dir, timeout_sec, initial_session_id):
              return _ConversationWorker(
                  adapter_factory, project_dir, timeout_sec, initial_session_id
              )

          def _default_adapter_factory(self) -> object:
              raise NotImplementedError("subclass must supply a default adapter factory")

          def _handle_extra(self, extra) -> None:
              """Hook: react to the worker's per-turn extra payload. Base: no-op."""

          # -- public API (GUI thread) --------------------------------------
          @property
          def session_id(self) -> str | None:
              return self._session_id

          def send(self, text: str, reset: bool = False) -> None:
              """Dispatch one turn; ``reset=True`` abandons any stored session id."""
              self._dispatch.emit(self._generation, text, reset)

          def stop(self) -> None:
              """Abandon the session: suppress further public signals and quit.

              Incrementing the generation on the GUI thread drops any in-flight
              turn's late signals. ``quit()`` cannot take effect while the worker
              is blocked inside ``run_turn``; the thread (and worker) are handed
              to ``_ABANDONED_THREADS`` and released on the thread's ``finished``.
              """
              self._generation += 1
              thread = self._thread
              try:
                  if thread.isRunning():
                      _ABANDONED_THREADS.add(thread)

                      def _release(thread=thread) -> None:
                          thread.deleteLater()
                          _ABANDONED_THREADS.discard(thread)

                      thread.finished.connect(_release)
                  thread.quit()
              except RuntimeError:
                  pass

          # -- worker -> facade (GUI thread; generation-filtered) ------------
          def _on_chunk(self, generation: int, text: str) -> None:
              if generation != self._generation:
                  return
              self.stream_chunk.emit(text)

          def _on_finished(self, generation: int, reply_text: str, session_id: str, extra) -> None:
              if generation != self._generation:
                  return
              self._session_id = session_id or None
              self.turn_finished.emit(reply_text, parse_options(reply_text))
              # Re-entrancy guard (review #9): a SYNCHRONOUS turn_finished
              # subscriber may have called stop() (which bumps _generation). The
              # extra payload belongs to the now-abandoned generation, so
              # re-check before handling it — otherwise e.g. requirements_ready
              # would still fire after stop().
              if generation != self._generation:
                  return
              self._handle_extra(extra)

          def _on_failed(self, generation: int, message: str) -> None:
              if generation != self._generation:
                  return
              self.turn_failed.emit(message)

          def _on_lost(self, generation: int) -> None:
              if generation != self._generation:
                  return
              self._session_id = None
              self.session_lost.emit()
  ```

  Run: `.venv/bin/python -m pytest tests/test_gui_conversation.py -q` → **PASSES**.

- [ ] **Step 3: refactor `spar/gui/grill.py` to subclass.** Replace the whole file with the thin subclass. Keep `OPENING_PROMPT_TEMPLATE` verbatim; re-export `Option`, `parse_options`, `_ABANDONED_THREADS`; `_GrillWorker` overrides `_post_turn`; `GrillSession` overrides `_make_worker`/`_default_adapter_factory`/`_handle_extra` and keeps `start`/`answer`.

  ```python
  """Grill-with-docs conversation — a thin ConversationSession subclass.

  The shared threaded machinery (worker QThread, generation-token stop
  suppression, session resume, abandoned-thread retention) lives in
  spar.gui.conversation. This module keeps only the grill-specific bits: the
  opening prompt, the requirements.md content-hash detection, and the
  start()/answer() wrappers. Option/parse_options/_ABANDONED_THREADS are
  re-exported so existing importers (and tests) are unaffected.
  """
  from __future__ import annotations

  import hashlib
  from pathlib import Path
  from typing import Optional

  from spar.gui.conversation import Option, parse_options  # re-exported

  __all__ = [
      "OPENING_PROMPT_TEMPLATE",
      "Option",
      "parse_options",
      "GrillSession",
  ]

  OPENING_PROMPT_TEMPLATE = """Użyj skilla grill-with-docs dla tego projektu. Zadanie do wygrillowania:
  "{draft}".
  Zadawaj pytania POJEDYNCZO, każde z opcjami oznaczonymi LITERAMI (A., B., C., ...)
  i Twoją rekomendacją — ja odpowiadam w kolejnych wiadomościach. Gdy uznasz
  wymagania za kompletne, zapisz finalne wymagania do .spar/requirements.md
  (pełna treść zadania dla dwustronnej debaty, zakończona wymaganiem sekcji
  ## Tasks) i napisz GOTOWE."""


  try:  # pragma: no cover - exercised via the two interpreters
      from PySide6.QtCore import QThread  # noqa: F401

      _HAS_QT = True
  except ImportError:  # pragma: no cover
      _HAS_QT = False


  if _HAS_QT:
      from spar.adapters.claude import ClaudeAdapter
      from spar.config import SideConfig  # noqa: F401
      from spar.gui.conversation import (
          _ABANDONED_THREADS,  # noqa: F401  (re-export for tests)
          ConversationSession,
          _ConversationWorker,
      )

      _REQ_RELPATH = Path(".spar") / "requirements.md"

      def _content_hash(path: Path) -> Optional[str]:
          """SHA-256 of ``path``'s bytes, or ``None`` when it does not exist."""
          try:
              data = path.read_bytes()
          except (FileNotFoundError, OSError):
              return None
          return hashlib.sha256(data).hexdigest()

      class _GrillWorker(_ConversationWorker):
          """Adds requirements.md content-hash detection to the base worker."""

          def __init__(self, adapter_factory, project_dir, timeout_sec, initial_session_id=None):
              super().__init__(adapter_factory, project_dir, timeout_sec, initial_session_id)
              self._req_path = self._project_dir / _REQ_RELPATH
              self._req_hash = _content_hash(self._req_path)

          def _post_turn(self, result) -> object:
              """Return the requirements content iff created/changed since start."""
              new_hash = _content_hash(self._req_path)
              if new_hash is None or new_hash == self._req_hash:
                  return None
              self._req_hash = new_hash
              try:
                  return self._req_path.read_text(encoding="utf-8")
              except OSError:
                  return None

      class GrillSession(ConversationSession):
          """GUI-thread facade for a grill-with-docs conversation."""

          # Extra grill-only signal alongside the inherited public signals.
          from PySide6.QtCore import Signal  # noqa: E402  (class-body import for Signal)

          requirements_ready = Signal(str)  # content

          def _make_worker(self, adapter_factory, project_dir, timeout_sec, initial_session_id):
              return _GrillWorker(adapter_factory, project_dir, timeout_sec, initial_session_id)

          def _default_adapter_factory(self) -> object:
              cfg = self._side_cfg
              model = cfg.debate_model or cfg.model or cfg.default_model
              return ClaudeAdapter(
                  command=cfg.command,
                  model=model,
                  cwd=self._project_dir,
                  events_dir=self._project_dir / ".spar" / "transcript",
                  side_name="grill",
              )

          def _handle_extra(self, extra) -> None:
              if isinstance(extra, str):
                  self.requirements_ready.emit(extra)

          # -- grill-specific public wrappers -------------------------------
          def start(self, draft: str) -> None:
              """Begin a FRESH session with the opening template on ``draft``."""
              self.send(OPENING_PROMPT_TEMPLATE.format(draft=draft), reset=True)

          def answer(self, text: str) -> None:
              """Send the next turn, resuming the stored session id."""
              self.send(text, reset=False)
  ```

  Note on the class-body `Signal` import: PySide6 `Signal`s must be declared as class attributes. Import `Signal` at module top instead if the inline import reads awkwardly — put `from PySide6.QtCore import QThread, Signal` in the `if _HAS_QT:` block and declare `requirements_ready = Signal(str)` normally. Prefer the top-of-block import.

  Run BOTH: `.venv/bin/python -m pytest tests/test_gui_grill.py tests/test_gui_conversation.py -q` → **PASSES** (grill tests unchanged), and `python3 -m pytest tests/test_gui_grill.py -q` (pure `parse_options`/`OPENING_PROMPT_TEMPLATE` still import from `grill`).

- [ ] **Step 4: full suite.** `.venv/bin/python -m pytest tests/ -q` → the baseline suite plus the new `test_gui_conversation.py` cases all pass, still 2 skipped, no failures (do NOT hard-code an intermediate total — the new class's exact test count drifts as cases are added); `python3 -m pytest tests/ -q` green (GUI skipped).

- [ ] **Step 5: commit** — `refactor(gui): extract ConversationSession; GrillSession becomes a thin subclass`

---

### Task 2: Tool-window rails + layout state machine (Opus)

The right/left rails and the collapse→full-width layout are a genuine state machine (which panels visible → column visible → splitter ratio, all persisted). Introduces a minimal `OrchestratorChatPanel` shell so the "Czat" toggle has something to show; Task 3 fills it in.

**Files:**
- Create: `spar/gui/rails.py`
- Create: `spar/gui/orchestrator.py` (shell only this task)
- Modify: `spar/gui/app.py` (central layout, rails wiring, QSettings)
- Modify: `spar/gui/theme.py` (rail + attention-dot QSS)
- Create: `tests/test_gui_rails.py`
- Modify: `tests/test_gui_app.py` (layout assertions)

**Interfaces:**

Produces (`spar/gui/rails.py`):
- `@dataclass(frozen=True) RailButtonSpec(key: str, label: str, tooltip: str, icon: str = "", checkable: bool = True, enabled: bool = True)` — `icon` is a unicode glyph rendered as the button face (ADR 0005 JetBrains-style icon rail); `label` is the human name, folded into the tooltip; no binary assets, cross-platform glyphs only.
- `def right_column_visibility(tasks_visible: bool, chat_visible: bool) -> bool` — pure: `tasks_visible or chat_visible` (the right column is shown iff at least one right panel is visible).
- `class IconRail(QWidget)`: vertical strip. `__init__(self, specs: list[RailButtonSpec], parent=None)`. Holds `self.buttons: dict[str, QToolButton]` (each `objectName=f"rail_{key}"`, `setCheckable(spec.checkable)`, `setEnabled(spec.enabled)`, tooltip set). Signal `toggled = Signal(str, bool)` re-emitted from each checkable button; `clicked = Signal(str)` for non-checkable (Bramka). `set_attention(self, key: str, on: bool)` toggles a dynamic property `attention` on the button, re-polishes it (QSS tints the border/text) AND calls `update()` so `_RailButton.paintEvent` draws/clears the yellow overlay dot (review #21 — QSS cannot draw a filled circle). `set_button_visible(self, key: str, visible: bool)`. `set_checked(self, key, checked)` without re-emitting (blockSignals).

Produces (`spar/gui/orchestrator.py`, shell only): `class OrchestratorChatPanel(QWidget)` with `setObjectName("orchestratorPanel")`, a header `QLabel` (`objectName="orchestratorHeader"`), an empty transcript placeholder. Constructor signature the FINAL one Task 3 expects, so app.py wires it once: `__init__(self, project_dir, side_cfg, timeout_sec, parent=None, session=None)`. This task leaves the body a stub (header + "czat pojawi się" placeholder); no session is built yet (guard: only build a session when Task 3 lands — this task passes `session=None` and the shell does not construct one).

Modifies (`spar/gui/app.py`):
- `RightColumn(QWidget)` (nested/module-level): `QVBoxLayout` stacking `side_pane` then `chat_panel`; exposes `set_tasks_visible(bool)` / `set_chat_visible(bool)` toggling child visibility.
- `MainWindow`: central widget replaced by `QWidget` + `QHBoxLayout` = `[left_rail][splitter][right_rail]`. `splitter = QSplitter(stream | right_column)`. Rails: left `IconRail([RailButtonSpec("files", "Pliki", "Pliki (wkrótce)", icon="🗀", checkable=True, enabled=False)])`; right `IconRail([RailButtonSpec("tasks", "Taski", "Panel zadań i bramki", icon="☰"), RailButtonSpec("chat", "Czat", "Czat z orkiestratorem", icon="💬"), RailButtonSpec("gate", "Bramka", "Otwórz oczekującą bramkę", icon="⚠", checkable=False)])` (JetBrains-style glyph faces per ADR 0005; full names carried in the tooltips). QSettings keys `rails/tasks_visible` (default True), `rails/chat_visible` (default True). Methods `_apply_rail_layout()`, `_on_rail_toggled(key, checked)`, `_on_gate_icon_clicked()`.

- [ ] **Step 1: failing pure + rail-widget tests.** Split the two so the pure test is NOT shadowed by a module-level `importorskip` (review #8: a pure helper test under `importorskip("PySide6")` silently SKIPS on plain `python3`, giving false green). The Qt-free helper gets its own PySide6-free module; the widget tests keep the `importorskip`.

  `tests/test_gui_rails_pure.py` (imports on any interpreter — NO `importorskip`, so it actually runs under `python3`):

  ```python
  from __future__ import annotations

  from spar.gui.rails import right_column_visibility


  class TestRightColumnVisibility:
      def test_hidden_only_when_both_collapsed(self):
          assert right_column_visibility(False, False) is False
          assert right_column_visibility(True, False) is True
          assert right_column_visibility(False, True) is True
          assert right_column_visibility(True, True) is True
  ```

  `tests/test_gui_rails.py` (Qt widget tests):

  ```python
  from __future__ import annotations

  import pytest

  pytest.importorskip("PySide6")

  from spar.gui.rails import IconRail, RailButtonSpec


  class TestIconRail:
      def test_builds_named_buttons_with_state(self, qtbot):
          rail = IconRail([
              RailButtonSpec("tasks", "Taski", "tip", icon="☰"),
              RailButtonSpec("files", "Pliki", "tip", icon="🗀", enabled=False),
          ])
          qtbot.addWidget(rail)
          assert rail.buttons["tasks"].isEnabled() is True
          assert rail.buttons["files"].isEnabled() is False
          assert rail.buttons["tasks"].isCheckable() is True

      def test_button_face_is_the_glyph_not_the_label(self, qtbot):
          # Review #20: it must be an ICON rail — the face shows the glyph, the
          # human name lives only in the tooltip; the face is a fixed square.
          rail = IconRail([RailButtonSpec("tasks", "Taski", "Panel zadań", icon="☰")])
          qtbot.addWidget(rail)
          btn = rail.buttons["tasks"]
          assert btn.text() == "☰"
          assert "Taski" not in btn.text()
          assert "Taski" in btn.toolTip()
          assert btn.width() == btn.height()  # fixed square face

      def test_toggle_emits_key_and_state(self, qtbot):
          rail = IconRail([RailButtonSpec("chat", "Czat", "tip")])
          qtbot.addWidget(rail)
          seen = []
          rail.toggled.connect(lambda k, s: seen.append((k, s)))
          rail.buttons["chat"].setChecked(True)
          assert seen == [("chat", True)]

      def test_non_checkable_button_emits_clicked(self, qtbot):
          rail = IconRail([RailButtonSpec("gate", "Bramka", "tip", checkable=False)])
          qtbot.addWidget(rail)
          seen = []
          rail.clicked.connect(seen.append)
          rail.buttons["gate"].click()
          assert seen == ["gate"]

      def test_attention_draws_dot_overlay(self, qtbot):
          # Review #21: the attention flag must actually PAINT a yellow dot (an
          # overlay drawn in paintEvent), not merely recolor the border via QSS.
          # Verify the property AND that the paintEvent path is exercised: the
          # rendered pixels change and a yellow dot appears top-right.
          rail = IconRail([RailButtonSpec("gate", "Bramka", "tip", icon="⚠",
                                          checkable=False)])
          qtbot.addWidget(rail)
          btn = rail.buttons["gate"]
          before = btn.grab().toImage()
          rail.set_attention("gate", True)
          assert btn.property("attention") is True
          after = btn.grab().toImage()
          assert after != before  # paintEvent overlay actually changed the pixels
          # Yellow dot in the top-right corner region (see paintEvent geometry).
          dot = after.pixelColor(btn.width() - 3 - 4, 3 + 4)
          assert dot.red() > 150 and dot.green() > 120 and dot.blue() < 90
          # Clearing the flag repaints without the dot.
          rail.set_attention("gate", False)
          assert btn.property("attention") is False
          assert btn.grab().toImage() != after

      def test_set_button_visible(self, qtbot):
          rail = IconRail([RailButtonSpec("gate", "Bramka", "tip", checkable=False)])
          qtbot.addWidget(rail)
          rail.show()  # unshown widgets report isVisible() False regardless
          assert rail.buttons["gate"].isHidden() is False
          rail.set_button_visible("gate", False)
          assert rail.buttons["gate"].isHidden() is True
          rail.set_button_visible("gate", True)
          assert rail.buttons["gate"].isHidden() is False
  ```

  Run: `.venv/bin/python -m pytest tests/test_gui_rails.py tests/test_gui_rails_pure.py -q` and `python3 -m pytest tests/test_gui_rails_pure.py -q` → **FAIL** (module missing; the pure test fails to import). The pure module must genuinely RUN (not skip) under `python3`.

- [ ] **Step 2: implement `spar/gui/rails.py`.**

  ```python
  """Vertical icon rails for the spar gui main window (ADR 0005)."""
  from __future__ import annotations

  from dataclasses import dataclass


  @dataclass(frozen=True)
  class RailButtonSpec:
      key: str
      label: str
      tooltip: str
      icon: str = ""          # unicode glyph rendered as the button face (ADR 0005)
      checkable: bool = True
      enabled: bool = True


  def right_column_visibility(tasks_visible: bool, chat_visible: bool) -> bool:
      """Pure: the right column is shown iff at least one right panel is visible."""
      return bool(tasks_visible or chat_visible)


  try:  # pragma: no cover
      from PySide6.QtCore import QSize, Qt, Signal
      from PySide6.QtGui import QColor, QPainter
      from PySide6.QtWidgets import QToolButton, QVBoxLayout, QWidget

      _HAS_QT = True
  except ImportError:  # pragma: no cover
      _HAS_QT = False


  if _HAS_QT:

      _RAIL_BUTTON_SIZE = 34   # fixed square face -> a real icon rail, not a text column
      _ATTENTION_DOT = "#e6b800"  # yellow attention dot (mirrors TOKENS['warn'])

      class _RailButton(QToolButton):
          """Square glyph button that paints a yellow attention dot top-right.

          The dot is an OVERLAY drawn in ``paintEvent`` (QSS cannot draw a filled
          circle); it appears iff the dynamic property ``attention`` is truthy, so
          ``set_attention`` only has to flip the property and call ``update()``.
          """

          def paintEvent(self, event) -> None:  # noqa: N802 (Qt override)
              super().paintEvent(event)
              if not self.property("attention"):
                  return
              d = 8  # dot diameter
              m = 3  # margin from the top-right corner
              painter = QPainter(self)
              painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
              painter.setPen(Qt.PenStyle.NoPen)
              painter.setBrush(QColor(_ATTENTION_DOT))
              painter.drawEllipse(self.width() - d - m, m, d, d)
              painter.end()

      class IconRail(QWidget):
          """A vertical strip of toggle/action buttons on a window edge."""

          toggled = Signal(str, bool)  # key, checked (checkable buttons)
          clicked = Signal(str)        # key (non-checkable buttons)

          def __init__(self, specs: "list[RailButtonSpec]", parent: QWidget | None = None):
              super().__init__(parent)
              self.setObjectName("iconRail")
              layout = QVBoxLayout(self)
              layout.setContentsMargins(2, 6, 2, 6)
              layout.setSpacing(6)
              self.buttons: dict[str, _RailButton] = {}
              for spec in specs:
                  btn = _RailButton(self)
                  btn.setObjectName(f"rail_{spec.key}")
                  # Glyph is the icon face; the human name lives in the tooltip.
                  btn.setText(spec.icon or spec.label)
                  btn.setToolTip(f"{spec.label} — {spec.tooltip}")
                  btn.setCheckable(spec.checkable)
                  btn.setEnabled(spec.enabled)
                  btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
                  btn.setFixedSize(QSize(_RAIL_BUTTON_SIZE, _RAIL_BUTTON_SIZE))
                  if spec.checkable:
                      btn.toggled.connect(
                          lambda checked, k=spec.key: self.toggled.emit(k, checked)
                      )
                  else:
                      btn.clicked.connect(lambda _=False, k=spec.key: self.clicked.emit(k))
                  self.buttons[spec.key] = btn
                  layout.addWidget(btn)
              layout.addStretch(1)

          def set_checked(self, key: str, checked: bool) -> None:
              btn = self.buttons.get(key)
              if btn is None:
                  return
              btn.blockSignals(True)
              btn.setChecked(checked)
              btn.blockSignals(False)

          def set_attention(self, key: str, on: bool) -> None:
              btn = self.buttons.get(key)
              if btn is None:
                  return
              btn.setProperty("attention", bool(on))
              # Re-polish so the QSS border/text (below) tracks the flag, then
              # repaint so the overlay dot in paintEvent is drawn/cleared.
              btn.style().unpolish(btn)
              btn.style().polish(btn)
              btn.update()

          def set_button_visible(self, key: str, visible: bool) -> None:
              btn = self.buttons.get(key)
              if btn is not None:
                  btn.setVisible(visible)
  ```

  Run both interpreters → pure test passes on `python3`; Qt tests pass on `.venv`.

- [ ] **Step 3: `spar/gui/orchestrator.py` shell.** Minimal panel so the Czat toggle has a target:

  ```python
  """Docked orchestrator chat panel (ADR 0005). Shell — filled out in a later task."""
  from __future__ import annotations

  try:  # pragma: no cover
      from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

      _HAS_QT = True
  except ImportError:  # pragma: no cover
      _HAS_QT = False


  if _HAS_QT:
      from spar.gui.theme import TOKENS

      class OrchestratorChatPanel(QWidget):
          """Read-only advisor chat, docked at the bottom of the right column."""

          def __init__(self, project_dir, side_cfg, timeout_sec, parent=None, session=None):
              super().__init__(parent)
              self.setObjectName("orchestratorPanel")
              self._project_dir = project_dir
              self._side_cfg = side_cfg
              self._timeout_sec = timeout_sec
              layout = QVBoxLayout(self)
              self.header = QLabel("claude · orkiestrator", self)
              self.header.setObjectName("orchestratorHeader")
              layout.addWidget(self.header)
              self.placeholder = QLabel("czat pojawi się tutaj", self)
              self.placeholder.setStyleSheet(f"color: {TOKENS['muted']};")
              layout.addWidget(self.placeholder)
              layout.addStretch(1)
  ```

- [ ] **Step 4: theme QSS.** In `spar/gui/theme.py`, add rules for `#iconRail` (panel background, no border) and a warn-tinted border/text for a rail button carrying `attention: true` (the actual yellow DOT is painted by `_RailButton.paintEvent`, review #21 — QSS cannot draw a filled circle; the QSS below only complements it), plus the RUNNING banner color used in Task 3. Append inside the `build_qss` f-string, all colors from `TOKENS`:

  ```python
      #iconRail {{
          background-color: {t['panel']};
          border: none;
      }}
      #iconRail QToolButton {{
          color: {t['text']};
          background-color: {t['panel']};
          border: 1px solid {t['line']};
          border-radius: 4px;
          font-size: 18px;   /* large glyph face -> reads as an icon, not a text label */
      }}
      #iconRail QToolButton:checked {{
          background-color: {t['panel-alt']};
          border: 1px solid {t['claude']};
      }}
      #iconRail QToolButton:disabled {{
          color: {t['muted']};
      }}
      #iconRail QToolButton[attention="true"] {{
          border: 1px solid {t['warn']};
          color: {t['warn']};
      }}
  ```

- [ ] **Step 5: failing MainWindow layout tests** in `tests/test_gui_app.py`:

  ```python
  def test_has_left_and_right_rails(self, qtbot, tmp_path):
      from spar.gui.rails import IconRail
      window = MainWindow(tmp_path)
      qtbot.addWidget(window)
      rails = window.findChildren(IconRail)
      assert len(rails) == 2
      assert "files" in window.left_rail.buttons
      assert set(window.right_rail.buttons) >= {"tasks", "chat", "gate"}
      assert window.left_rail.buttons["files"].isEnabled() is False

  def test_collapsing_both_right_panels_hides_column(self, qtbot, tmp_path):
      window = MainWindow(tmp_path)
      qtbot.addWidget(window)
      window.right_rail.buttons["tasks"].setChecked(False)
      window.right_rail.buttons["chat"].setChecked(False)
      # isHidden() reflects the explicit setVisible(False) regardless of whether
      # the (never-shown) window has been shown — unlike isVisible(), which is
      # vacuously False pre-show and would pass even if the code did nothing
      # (review #8).
      assert window.right_column.isHidden() is True
      # Re-open one panel -> column reappears (explicitly not hidden).
      window.right_rail.buttons["tasks"].setChecked(True)
      assert window.right_column.isHidden() is False

  def test_rail_state_persists_via_qsettings(self, qtbot, tmp_path):
      window = MainWindow(tmp_path)
      qtbot.addWidget(window)
      window.right_rail.buttons["chat"].setChecked(False)
      assert window._settings.value("rails/chat_visible") in (False, "false", 0, "0")

  def test_gate_icon_hidden_without_pending_gate(self, qtbot, tmp_path):
      window = MainWindow(tmp_path)
      qtbot.addWidget(window)
      # Fresh dir: no pending gate -> Bramka icon explicitly hidden (isHidden(),
      # not the vacuous pre-show isVisible(); review #8).
      assert window.right_rail.buttons["gate"].isHidden() is True

  def test_new_pending_gate_force_opens_taski(self, qtbot, tmp_path):
      # Review #4: a new pending gate auto-opens Taski without resolving it.
      window = MainWindow(tmp_path)
      qtbot.addWidget(window)
      window.right_rail.set_checked("tasks", False)
      window._on_rail_toggled("tasks", False)
      assert window.right_rail.buttons["tasks"].isChecked() is False
      window._on_status_changed(
          {"pending_gate": {"name": "consensus", "context": {"task_id": "t1"}}}
      )
      assert window.right_rail.buttons["tasks"].isChecked() is True
      assert window.right_rail.buttons["gate"].isHidden() is False
  ```

  Run → **FAIL** (`left_rail`/`right_rail`/`right_column` attrs don't exist).

- [ ] **Step 6: implement the app.py layout.** Add `RightColumn`, build both rails, replace the central widget, wire toggles + QSettings + gate icon. Key edits:
  - **Init ORDER (review #1).** The existing `__init__` calls `self.side_pane.refresh()` (current line ~129) synchronously, which fires `status_changed → _on_status_changed`. Because `_on_status_changed` now touches `self.right_rail` (gate-icon attention, below), the rails, `right_column`, and central widget MUST be constructed and wired BEFORE that `refresh()`. Reorder `__init__` so the sequence is: build `stream_pane` → build `side_pane` (its own internal `refresh()` fires before any connection, harmless) → connect `side_pane.status_changed` to `_on_status_changed` → set `gate_panel.preflight_auto_exec` → build `chat_panel` + `right_column` + both rails + splitter + central widget + wire rail toggles/QSettings/`set_button_visible("gate", False)` and init `self._prev_gate_key = None` / `self._column_shown` → **THEN** call `self.side_pane.refresh()` exactly once as the single explicit initial status synchronization (it now safely drives the gate icon and, when a gate is already pending on startup, force-opens Taski). Do NOT leave the old `self.side_pane.refresh()` at its original position ahead of the rails.
  - Add imports: `from spar.gui.orchestrator import OrchestratorChatPanel`, `from spar.gui.rails import IconRail, RailButtonSpec, right_column_visibility`, and `QToolButton` unused — skip.
  - After building `stream_pane` and `side_pane`, build the chat panel and column:

    ```python
    # Chat panel needs the same claude side-config resolution the grill uses.
    from spar.gui.toolbar import _grill_availability  # reuse the resolver
    chat_side_cfg, chat_timeout, _reason = _grill_availability(self.project_dir)
    self.chat_panel = OrchestratorChatPanel(
        self.project_dir, chat_side_cfg, chat_timeout, session=None
    )
    self.right_column = RightColumn(self.side_pane, self.chat_panel, self)
    ```
  - `RightColumn`:

    ```python
    class RightColumn(QWidget):
        """Right-side tool column: SidePane (Taski + gate) over the chat panel."""

        def __init__(self, side_pane, chat_panel, parent=None):
            super().__init__(parent)
            self.setObjectName("rightColumn")
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(side_pane, stretch=3)
            layout.addWidget(chat_panel, stretch=2)
            self._side_pane = side_pane
            self._chat_panel = chat_panel

        def set_tasks_visible(self, visible: bool) -> None:
            self._side_pane.setVisible(visible)

        def set_chat_visible(self, visible: bool) -> None:
            self._chat_panel.setVisible(visible)
    ```
  - Splitter now holds `stream_pane` + `right_column`; central widget wraps rails around it:

    ```python
    self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
    self.splitter.setObjectName("mainSplitter")
    self.splitter.addWidget(self.stream_pane)
    self.splitter.addWidget(self.right_column)
    self.splitter.resize(sum(_SPLITTER_SIZES), 900)
    self.splitter.setSizes(list(_SPLITTER_SIZES))

    self.left_rail = IconRail(
        [RailButtonSpec("files", "Pliki", "Pliki (wkrótce)", icon="🗀", enabled=False)],
        self,
    )
    self.right_rail = IconRail(
        [
            RailButtonSpec("tasks", "Taski", "Panel zadań i bramki", icon="☰"),
            RailButtonSpec("chat", "Czat", "Czat z orkiestratorem", icon="💬"),
            RailButtonSpec("gate", "Bramka", "Otwórz oczekującą bramkę",
                           icon="⚠", checkable=False),
        ],
        self,
    )
    central = QWidget(self)
    central_layout = QHBoxLayout(central)
    central_layout.setContentsMargins(0, 0, 0, 0)
    central_layout.setSpacing(0)
    central_layout.addWidget(self.left_rail)
    central_layout.addWidget(self.splitter, stretch=1)
    central_layout.addWidget(self.right_rail)
    self.setCentralWidget(central)
    ```
    (Add `QHBoxLayout`, `QWidget` to the imports from `PySide6.QtWidgets`.)
  - QSettings restore + wiring (after `self._settings = QSettings("spar", "gui")`):

    ```python
    self._restore_splitter_state()
    self.splitter.splitterMoved.connect(self._save_splitter_state)

    tasks_visible = self._settings.value("rails/tasks_visible", True, type=bool)
    chat_visible = self._settings.value("rails/chat_visible", True, type=bool)
    self.right_rail.set_checked("tasks", tasks_visible)
    self.right_rail.set_checked("chat", chat_visible)
    self.right_rail.set_button_visible("gate", False)
    self.right_rail.toggled.connect(self._on_rail_toggled)
    self.right_rail.clicked.connect(self._on_rail_clicked)
    # Track the LOGICAL right-column visibility ourselves (review #3): before
    # the top-level window is shown, every widget reports isVisible()==False,
    # so _apply_rail_layout() must NOT read effective visibility to decide
    # whether to restore the splitter ratio — seed the tracker from the
    # restored settings so the first _apply_rail_layout() is a no-op that
    # preserves the QSettings-restored splitter sizes.
    self._column_shown = right_column_visibility(tasks_visible, chat_visible)
    # Force-open bookkeeping for a pending gate (review #4): identity of the
    # gate whose panel we last auto-opened, so only a genuinely NEW gate edge
    # re-opens Taski (and only OPENS it — never resolves/hides it).
    self._prev_gate_key = None
    self._apply_rail_layout()
    ```
  - Methods:

    ```python
    def _rail_state(self) -> tuple[bool, bool]:
        return (
            self.right_rail.buttons["tasks"].isChecked(),
            self.right_rail.buttons["chat"].isChecked(),
        )

    def _apply_rail_layout(self) -> None:
        tasks_visible, chat_visible = self._rail_state()
        self.right_column.set_tasks_visible(tasks_visible)
        self.right_column.set_chat_visible(chat_visible)
        show_column = right_column_visibility(tasks_visible, chat_visible)
        # Review #3: compare against the tracked LOGICAL previous state, NOT
        # self.right_column.isVisible(). Pre-show, isVisible() is always False,
        # so the old check treated normal startup as a hidden→shown transition
        # and clobbered the QSettings-restored splitter sizes with
        # _SPLITTER_SIZES. Restore the 1.7:1 ratio only on a real collapsed→
        # shown edge.
        if show_column and not self._column_shown:
            self.right_column.setVisible(True)
            self.splitter.setSizes(list(_SPLITTER_SIZES))  # restore 1.7:1
        else:
            self.right_column.setVisible(show_column)
        self._column_shown = show_column

    def _on_rail_toggled(self, key: str, checked: bool) -> None:
        self._settings.setValue(f"rails/{key}_visible", checked)
        self._apply_rail_layout()

    def _on_rail_clicked(self, key: str) -> None:
        if key == "gate":
            # Force-open Taski (which hosts the GatePanel); never discards it.
            self.right_rail.set_checked("tasks", True)
            self._on_rail_toggled("tasks", True)
    ```
  - Gate-icon attention **+ force-open (review #4)**: in `_on_status_changed`, drive the Bramka icon from `status["pending_gate"]` AND auto-open the Taski panel (which hosts the GatePanel) when a NEW gate appears. This only ever OPENS Taski — it never resolves, hides, or destroys the gate (the GatePanel decision stays the sole pilot):

    ```python
    pending_gate = status.get("pending_gate")
    pending = bool(pending_gate)
    self.right_rail.set_button_visible("gate", pending)
    self.right_rail.set_attention("gate", pending)
    # ADR 0005: a pending gate force-opens its panel. Fire only on the edge to
    # a genuinely new gate (identity = name + task_id + rounds) so a user who
    # deliberately collapsed Taski for the SAME gate isn't fought on every 2s
    # poll; a brand-new gate still reasserts the panel.
    gate_key = self._gate_identity(pending_gate)
    if pending and gate_key != self._prev_gate_key:
        if not self.right_rail.buttons["tasks"].isChecked():
            self.right_rail.set_checked("tasks", True)
            self._on_rail_toggled("tasks", True)  # persists + re-applies layout
    self._prev_gate_key = gate_key if pending else None
    ```
  - Add the pure identity helper (a new gate for the same task after extend/retry counts as new via `rounds`):

    ```python
    @staticmethod
    def _gate_identity(pending_gate: "dict | None") -> "tuple | None":
        if not pending_gate:
            return None
        ctx = pending_gate.get("context") or {}
        return (pending_gate.get("name"), ctx.get("task_id"), ctx.get("rounds"))
    ```
  - `closeEvent`: splitter/rail state persistence is unchanged here; the chat-session shutdown required by review #2 is added in Task 3 (once the panel actually builds an `OrchestratorSession`).

  Run: `.venv/bin/python -m pytest tests/test_gui_app.py tests/test_gui_rails.py -q` → **PASSES**. Confirm the pre-existing `test_splitter_ratio_is_wider_left_than_right` still holds (splitter still has stream wider than right_column).

- [ ] **Step 7: full suite + commit.** `.venv/bin/python -m pytest tests/ -q` green; `python3 -m pytest tests/ -q` green. Commit — `feat(gui): tool-window rails on both edges with collapsible right column`

---

### Task 3: Orchestrator chat panel UI (Sonnet)

Fill out `OrchestratorChatPanel`: bubbles, `tool:` lines inside bot bubbles, lettered-option buttons, always-available free-text send, header line, RUNNING read-only banner, wired to an `OrchestratorSession`. Reuses the grill dialog's bubble/option patterns.

**Files:**
- Modify: `spar/gui/orchestrator.py` (`OrchestratorSession` + full `OrchestratorChatPanel`)
- Modify: `spar/gui/app.py` (build the session; wire `runner.state_changed` → panel banner; `closeEvent` → `stop_session`)
- Create: `tests/test_gui_orchestrator.py`
- Modify: `tests/test_gui_app.py` (real `MainWindow.close()` → `stop_session` wiring test, review #11)

**Interfaces:**

Produces:
- `OPENING_PROMPT` — module-top constant, Qt-free (defined above the `if _HAS_QT:` guard alongside `build_gate_context`/`parse_task_draft` so the module imports on a plain interpreter), verbatim (read-only advisor contract). **Review #31: the task-draft format example is shown in the exact MULTILINE form `parse_task_draft` accepts** — opening fence line, content lines, closing fence line on its own (Task 6: open matches `^```zadanie\s*$`, close `^```\s*$`). An earlier draft showed an inline one-line ` ```zadanie ... ``` ` marker, which the parser rejects — the prompt would have taught the model an unparseable format. The prompt/parser contract is pinned by a pure test (Task 6 Step 1) that runs `parse_task_draft` over `OPENING_PROMPT` itself:

  ````
  Jesteś orkiestratorem-DORADCĄ dla tego projektu spar. Pracujesz w trybie
  TYLKO-DO-ODCZYTU: analizujesz repozytorium i stan w .spar/, odpowiadasz na
  pytania i pomagasz planować kolejną pracę. NIE edytujesz plików, NIE
  uruchamiasz narzędzi zmieniających repo, i NIGDY nie podejmujesz decyzji
  bramek — decyzje bramek podejmuje wyłącznie panel Bramki w GUI. Gdy
  proponujesz opcje, oznaczaj je LITERAMI (A., B., C., ...) z rekomendacją.
  Gdy przygotujesz szkic zadania do nowej debaty, umieść go w bloku
  ogrodzonym DOKŁADNIE w tym wielowierszowym formacie (linia otwierająca
  ```zadanie, treść zadania w kolejnych liniach, osobna linia zamykająca ```):

  ```zadanie
  <treść szkicu zadania>
  ```

  aby GUI mogło go przejąć.
  ````
- `OrchestratorSession(ConversationSession)`: `_default_adapter_factory` → `ClaudeAdapter(command=cfg.command, model=cfg.debate_model or cfg.model or cfg.default_model, cwd=project_dir, events_dir=project_dir/'.spar'/'transcript', side_name="orchestrator", readonly=True)`; no extra worker/hook needed (base worker is enough), and NO extra send helper. Review #14: there is deliberately no `send_opening()` on the session — the opening prompt is composed and prepended EXCLUSIVELY by the panel's single `_dispatch_user_text` path (which owns `_opening_sent`), so the session exposes only the inherited `send(text, reset)`. (An earlier draft's `send_opening()` referenced an undefined `first_user_text` and duplicated the panel path — removed.) **Review #27: the `readonly=True` + `side_name="orchestrator"` construction is the ADR 0005 safety boundary and MUST be pinned by a test** (`TestOrchestratorSessionAdapter` in Step 1 monkeypatches `spar.gui.orchestrator.ClaudeAdapter` and asserts the constructor kwargs) — without it, dropping `readonly=True` would regress silently while every panel test (which injects a fake session) stayed green.
- `OrchestratorChatPanel(QWidget)` — final:
  - Consumes: `project_dir`, `side_cfg` (may be `None` → panel shows a disabled "czat niedostępny" state), `timeout_sec`, optional injected `session` (tests pass a fake exposing `send`, `stream_chunk`, `turn_finished`, `turn_failed`, `session_lost`, `session_id`).
  - Widgets (objectNames for tests): `transcript` (`QTextBrowser`), `header` (`QLabel` "orchestratorHeader"), `banner` (`QLabel` "orchestratorBanner", hidden unless a run is LIVE — RunnerState.RUNNING **or** LOCKED, review #28), `options_row`/`options_layout` (vertical, reused grill pattern), `input_edit` (`_InputEdit`, Ctrl+Enter), `send_button` ("orchestratorSend").
  - Bubbles: user right/accent, bot left/claude — reuse the grill `_bubble_html` escaping. A `tool:`-prefixed streamed line is rendered on its own line inside the current bot bubble in a dim monospace span (`color: TOKENS['muted']; font-family: monospace`). Implement by tracking the in-flight bot bubble as a list of segments (text vs tool) rather than one string.
  - Options: `turn_finished(reply, options)` → vertical option buttons (`objectName=f"option_{letter}"`, letter prefix, `_truncate` display, full tooltip, `Expanding/Fixed`). **Review #15: an option click MUST go through the single `_dispatch_user_text(letter)` path — never `session.send(letter)` directly.** Routing the letter through the ONE send path is what renders the user bubble (`letter`), disables input/send while the turn is in flight, clears the option row, and — critically — folds in a pending gate context or a re-armed opening contract exactly as a typed message would. A raw `session.send(letter)` skips all of that (no user bubble, input never disabled, options never cleared, gate context lost), so options and free text must share the identical dispatch.
  - Free-text: input + send always enabled EXCEPT while a turn is in flight (`send_button`/`input_edit` disabled, header shows "…myśli"). Never disabled by RUNNING — the chat stays usable during a live run (only the banner shows).
  - **Turn failure (review #13).** `session.turn_failed(message)` MUST have a handler — an `AdapterError` (e.g. claude subprocess crash / transient failure) is retryable, but without a handler the in-flight disable is never lifted and the chat is permanently unusable. `_on_turn_failed(self, message)`: drop any in-flight streaming state (clear `self._streaming_segments`, discard the half-built bot bubble), append a dim error notice to the transcript (`f"⚠ tura nie powiodła się: {message}"`), re-enable `input_edit`/`send_button`, restore the header (`set_header(...)`), and restore the correct banner via `set_running(self._is_running)`. It does NOT bump the turn count and does NOT persist `chat.json`. The session id is untouched (a plain `send` can retry the same resume). **Review #17: it also clears the pending opening/gate fields (`self._pending_opening = False`, `self._pending_gate_key = None`) WITHOUT promoting them** — so `_opening_sent`/`_injected_gate_key` stay as they were before the failed turn, and the retry re-includes the opening contract and any not-yet-delivered gate context.
  - Header: `set_header(model, turn_count)` → `f"claude · {model} · tura {turn_count} · sesja trwała"` (model falls back to "?" when unknown).
  - Banner: `set_running(is_running: bool)` → shows/hides yellow "run w toku — tylko odczyt". The panel API stays a plain bool; **which** `RunnerState`s count as "running" is decided in `app.py`'s `_on_state_changed` (review #28: RUNNING **and** LOCKED — a live SIBLING spar process surfaces as `RunnerState.LOCKED`, not RUNNING, and the read-only warning must show for it too; see Step 3).
  - **Single dispatch helper `_dispatch_user_text(user_text)`** (the ONE send path; option clicks, free-text send, and the Task 4 & 5 extensions all funnel through it). Logic:
    - `needs_opening = not self._opening_sent` — a fresh session (or one just lost, see Task 4) has not yet run its read-only opening contract.
    - Build the outgoing prompt as, in order: `OPENING_PROMPT` (only when `needs_opening`), then the gate-context block (Task 5, only when a gate pends and its fingerprint differs from `self._injected_gate_key`), then `user_text`. Join non-empty parts with `"\n\n"`.
    - `self._session.send(prompt, reset=needs_opening)` — a fresh/lost session must `reset=True` so the resumed opening contract actually starts a NEW claude session.
    - **Review #17: commit the opening / gate-injection flags ONLY on a successful turn — never eagerly here.** Instead record what THIS in-flight turn carried in pending fields: `self._pending_opening = needs_opening`, and `self._pending_gate_key = <the injected fingerprint>` when a gate context was actually included this turn else `None` (a "nothing to promote" sentinel). Leave `self._opening_sent` and `self._injected_gate_key` UNTOUCHED at dispatch. `_on_turn_finished` promotes them (`if self._pending_opening: self._opening_sent = True`; `if self._pending_gate_key is not None: self._injected_gate_key = self._pending_gate_key`), then clears the pending fields. **Review #30: promotion happens ONLY when `session.session_id` is truthy after the turn.** The adapter contract permits a successful `TurnResult` with `session_id = None`; such a turn is NON-RESUMABLE (the worker holds no id and will start a fresh claude session on the next `run_turn`), so `_on_turn_finished` must then clear the pending fields WITHOUT promoting — exactly like a failure/loss — leaving `_opening_sent` re-armed and any injected gate context undelivered; the next `_dispatch_user_text` sees `needs_opening=True` and sends opening prompt (+ gate context) + user text with `reset=True`, matching the worker's fresh start. (Task 4 additionally skips `chat.json` persistence in this case — never persist a null id — AND deletes any existing `chat.json`, review #34: skipping the save is not enough when a RESUMED session's turn comes back id-less, because the stale file from the previous launch would make the next GUI start resume a dead id and treat the opening as already delivered.) `_on_turn_failed` and `_on_session_lost` clear the pending fields WITHOUT promoting. Consequences this fixes: after a first-turn `AdapterError` the retry still carries the opening contract (because `_opening_sent` was never set); after any failure an undelivered gate context re-injects (because its fingerprint was never recorded as delivered); and a `SessionLost` cannot leave a stale delivered-gate key (see the loss reset in Task 4).
    - The user's visible bubble appends ONLY `user_text` — never the opening prompt or gate context.
    - Initialize on construct: `self._opening_sent = False` for a fresh session (Task 4 sets it `True` when resuming a persisted session — opening already ran — and back to `False` on `session_lost`); `self._injected_gate_key = None`; `self._pending_opening = False`; `self._pending_gate_key = None`.

- [ ] **Step 1: failing tests** (`tests/test_gui_orchestrator.py`), fake session mirroring the grill `FakeGrillSession` (QObject with the four public signals + `send`/`session_id`):

  ```python
  from __future__ import annotations

  import pytest

  pytest.importorskip("PySide6")

  from PySide6.QtCore import QObject, Signal
  from PySide6.QtWidgets import QPushButton

  from spar.gui.conversation import Option
  from spar.gui.orchestrator import OPENING_PROMPT, OrchestratorChatPanel


  class FakeSession(QObject):
      stream_chunk = Signal(str)
      turn_finished = Signal(str, list)
      turn_failed = Signal(str)
      session_lost = Signal()

      def __init__(self):
          super().__init__()
          self.sends = []
          self.session_id = None

      def send(self, text, reset=False):
          self.sends.append((text, reset))

      def stop(self):
          pass


  def _panel(qtbot, tmp_path, session):
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=session)
      qtbot.addWidget(panel)
      return panel


  class TestOrchestratorChatPanel:
      def test_first_send_prepends_opening_prompt_and_resets(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("co robisz?")
          panel.send_button.click()
          text, reset = fake.sends[0]
          assert reset is True
          assert OPENING_PROMPT.split("\n")[0] in text
          assert "co robisz?" in text

      def test_second_send_is_plain_resume(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("pierwsze")
          panel.send_button.click()
          fake.session_id = "sess-1"  # review #33: truthy id -> resumable branch promotes _opening_sent
          fake.turn_finished.emit("ok", [])
          panel.input_edit.setPlainText("drugie")
          panel.send_button.click()
          assert fake.sends[1] == ("drugie", False)

      def test_option_click_routes_through_single_dispatch_path(self, qtbot, tmp_path):
          # Review #15: an option click must go through _dispatch_user_text, so it
          # has ALL the effects of a typed message — user bubble, in-flight
          # disable, options cleared — not a bare session.send(letter).
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("start")  # first send consumes the opening
          panel.send_button.click()
          fake.session_id = "sess-1"  # review #33: resumable turn, opening gets committed
          fake.turn_finished.emit("A. tak\nB. nie", [Option("A", "tak"), Option("B", "nie")])
          btn = panel.findChild(QPushButton, "option_B")
          assert btn is not None
          btn.click()
          # Dispatched as a plain resume turn (opening already committed).
          assert fake.sends[-1] == ("B", False)
          # Effects of the ONE send path (would all be MISSING with session.send):
          assert "B" in panel.transcript.toPlainText()          # user bubble rendered
          assert panel.input_edit.isEnabled() is False           # in-flight disable
          assert panel.send_button.isEnabled() is False
          assert panel.findChild(QPushButton, "option_B") is None  # option row cleared

      def test_tool_line_rendered_dim_in_bot_bubble(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("q")
          panel.send_button.click()
          fake.stream_chunk.emit("tool: Read .spar/state.json")
          html = panel.transcript.toHtml()
          assert "tool: Read" in panel.transcript.toPlainText()
          assert "monospace" in html  # dim monospace styling applied
          # Review #18: the tool line must SURVIVE turn completion — reply_text
          # carries no tool events, so committing only reply_text would drop it.
          fake.turn_finished.emit("gotowe", [])
          assert "tool: Read" in panel.transcript.toPlainText()
          assert "gotowe" in panel.transcript.toPlainText()

      def test_commit_prose_before_tool_preserves_order_no_dup(self, qtbot, tmp_path):
          # Review #23: streamed prose arrives BEFORE a tool line. Both survive in
          # arrival order; reply_text (which repeats the prose) is IGNORED, so the
          # prose is NOT duplicated.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("q"); panel.send_button.click()
          fake.stream_chunk.emit("myślę nad tym")
          fake.stream_chunk.emit("tool: Read .spar/state.json")
          fake.turn_finished.emit("myślę nad tym", [])  # reply echoes the prose
          text = panel.transcript.toPlainText()
          assert text.count("myślę nad tym") == 1        # prose not duplicated
          assert "tool: Read" in text                    # tool line kept
          assert text.index("myślę nad tym") < text.index("tool: Read")  # order

      def test_commit_prose_after_tool_preserves_order(self, qtbot, tmp_path):
          # Review #23: prose arrives AFTER the tool line -> arrival order kept.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("q"); panel.send_button.click()
          fake.stream_chunk.emit("tool: Grep foo")
          fake.stream_chunk.emit("oto odpowiedź")
          fake.turn_finished.emit("oto odpowiedź", [])
          text = panel.transcript.toPlainText()
          assert text.count("oto odpowiedź") == 1
          assert text.index("tool: Grep") < text.index("oto odpowiedź")  # order

      def test_commit_no_prose_only_tools_falls_back_to_reply_text(self, qtbot, tmp_path):
          # Review #23: no prose streamed (only tool lines) -> reply_text supplies
          # the prose (fallback) WHILE the streamed tool lines are still kept.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("q"); panel.send_button.click()
          fake.stream_chunk.emit("tool: Read a.py")
          fake.turn_finished.emit("finalna teza", [])
          text = panel.transcript.toPlainText()
          assert "tool: Read a.py" in text   # tool line survives
          assert "finalna teza" in text      # reply_text used as the prose fallback

      def test_commit_pure_prose_no_tools(self, qtbot, tmp_path):
          # Review #23: pure prose, no tools -> streamed prose committed once, no dup.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("q"); panel.send_button.click()
          fake.stream_chunk.emit("pełna odpowiedź")
          fake.turn_finished.emit("pełna odpowiedź", [])
          assert panel.transcript.toPlainText().count("pełna odpowiedź") == 1

      def test_commit_tools_then_terminal_done_keeps_reply_fallback(self, qtbot, tmp_path):
          # Review #24: only tool lines streamed, then the adapter's terminal
          # "done (…s)" status line. It must NOT count as prose — otherwise
          # has_prose=True suppresses reply_text and literal "done" renders
          # inside the answer bubble.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("q"); panel.send_button.click()
          fake.stream_chunk.emit("tool: Read a.py")
          fake.stream_chunk.emit("done (12.3s)")
          fake.turn_finished.emit("finalna teza", [])
          text = panel.transcript.toPlainText()
          assert "tool: Read a.py" in text   # tool line survives
          assert "finalna teza" in text      # reply_text fallback NOT suppressed
          assert "done (12.3s)" not in text  # terminal status line filtered

      def test_commit_prose_then_terminal_done_filtered(self, qtbot, tmp_path):
          # Review #24: real streamed prose followed by the bare "done" terminal
          # line — prose commits once, "done" never renders in the bubble.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("q"); panel.send_button.click()
          fake.stream_chunk.emit("oto odpowiedź")
          fake.stream_chunk.emit("done")
          fake.turn_finished.emit("oto odpowiedź", [])
          text = panel.transcript.toPlainText()
          assert text.count("oto odpowiedź") == 1
          assert "done" not in text          # terminal line dropped at arrival

      def test_running_banner_toggles_but_input_stays_enabled(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.show()  # review #8: isVisible() is vacuously False on an unshown widget
          panel.set_running(True)
          assert panel.banner.isVisible() is True
          assert panel.input_edit.isEnabled() is True
          panel.set_running(False)
          assert panel.banner.isVisible() is False

      def test_header_shows_model_and_turn(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.set_header("opus", 3)
          assert "opus" in panel.header.text()
          assert "tura 3" in panel.header.text()

      def test_turn_failed_reenables_input_and_shows_error(self, qtbot, tmp_path):
          # Review #13: an AdapterError must not brick the chat. Sending disables
          # input+send; turn_failed has to clear that disable, surface the error,
          # and leave the chat usable for a retry.
          from spar.gui.orchestrator import OPENING_PROMPT
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.input_edit.setPlainText("q")
          panel.send_button.click()
          assert panel.input_edit.isEnabled() is False  # disabled while in flight
          fake.turn_failed.emit("adapter boom")
          assert panel.input_edit.isEnabled() is True
          assert panel.send_button.isEnabled() is True
          assert "adapter boom" in panel.transcript.toPlainText()
          # Retry works: a second send is dispatched (chat not bricked).
          panel.input_edit.setPlainText("znowu")
          panel.send_button.click()
          # Review #17: the FIRST turn failed, so its opening contract was never
          # committed — the retry must re-carry OPENING_PROMPT and reset=True, not
          # a bare resume that would strand the new session without the read-only
          # advisor contract.
          retry_text, retry_reset = fake.sends[-1]
          assert "znowu" in retry_text
          assert OPENING_PROMPT.split("\n")[0] in retry_text
          assert retry_reset is True


  class TestOrchestratorSessionAdapter:
      def test_adapter_constructed_readonly_with_orchestrator_side_name(
          self, qtbot, tmp_path, monkeypatch
      ):
          # Review #27: the central ADR 0005 safety boundary — the advisor's
          # ClaudeAdapter MUST be constructed with readonly=True and
          # side_name="orchestrator". Every panel test injects a fake session,
          # so without this constructor-capture test the boundary could regress
          # silently. The base worker constructs the adapter eagerly in its
          # __init__ (conversation.py), so building the session is enough — no
          # turn needs to be dispatched.
          from types import SimpleNamespace

          import spar.gui.orchestrator as orch_mod

          captured = {}

          class FakeAdapter:
              def __init__(self, **kwargs):
                  captured.update(kwargs)

              def run_turn(self, *args, **kwargs):  # pragma: no cover
                  raise AssertionError("no turn dispatched in this test")

          # Patch the name orchestrator.py looks up (imported into its
          # `if _HAS_QT:` block, mirroring grill.py).
          monkeypatch.setattr(orch_mod, "ClaudeAdapter", FakeAdapter)
          cfg = SimpleNamespace(command="claude", model=None,
                                debate_model="opus", default_model="sonnet")
          session = orch_mod.OrchestratorSession(tmp_path, cfg, 60)
          try:
              assert captured["readonly"] is True
              assert captured["side_name"] == "orchestrator"
              assert captured["cwd"] == tmp_path
              assert captured["events_dir"] == tmp_path / ".spar" / "transcript"
              # Model resolution mirrors the engine: debate_model or model or
              # default_model.
              assert captured["model"] == "opus"
          finally:
              session.stop()
  ```

  (Note `panel.banner.isVisible()` requires `panel.show()` in the visibility tests if offscreen visibility is flaky; if so, call `panel.show()` first as the grill tests do for visibility assertions.) Run → **FAIL**.

- [ ] **Step 2: implement.** Flesh out `OrchestratorChatPanel` (reuse `_InputEdit`, `_truncate`, `_bubble_html` — import the helpers from `grill_dialog` or copy the tiny `_truncate`/`_InputEdit`; prefer importing `_InputEdit` and `_truncate` from `spar.gui.grill_dialog` to avoid duplication). Connect the injected/built session's four public signals: `stream_chunk → _on_chunk`, `turn_finished → _on_turn_finished`, `turn_failed → _on_turn_failed` (**review #13** — do NOT omit this connection; it is the only thing that lifts the in-flight input disable after an `AdapterError`), `session_lost → _on_session_lost` (Task 4). Segmented in-flight bubble for tool lines: keep `self._streaming_segments: list[tuple[str, str]]` of `(kind, text)` where kind ∈ {"text", "tool"}; a streamed line starting with `tool:` appends a `("tool", line)` segment, else concatenates onto the trailing text segment. **Review #24:** the ClaudeAdapter stream ends with a terminal status line — `done` or `done (12.3s)` — which is a status event, NOT prose: `_on_chunk` must drop it (never append it as a segment nor concatenate it onto a trailing text segment), and `_commit_bubble_html` filters it defensively too (below). Render text segments in the claude color and tool segments in a dim monospace span. **Review #18: `_on_turn_finished` must COMMIT the completed bot bubble from `self._streaming_segments` (which hold the tool lines), NOT from the bare `reply_text` — `reply_text` carries no tool events, so rendering only it drops every `tool:` line from the transcript the moment the turn ends.**

  **Review #23 — the merge algorithm is SPECIFIED (no vague "reconcile"), because the stream carries prose deltas, tool events, and a terminal done line that must not fold into the reply:**

  ```
  _TERMINAL_RE = re.compile(r"^done \(.*\)$")

  def _is_terminal(text) -> bool:
      # Review #24: ClaudeAdapter's terminal status events — "done" / "done (12.3s)".
      s = text.strip()
      return s == "done" or _TERMINAL_RE.match(s) is not None

  def _commit_bubble_html(segments, reply_text) -> str:
      # segments: the arrival-ordered [(kind, text)] where kind ∈ {"text","tool"}.
      # Review #24: filter terminal status lines FIRST — treating "done" as prose
      # would flip has_prose=True, wrongly suppress reply_text, and render a
      # literal "done" inside the answer bubble.
      segments = [(k, t) for k, t in segments if not (k == "text" and _is_terminal(t))]
      has_prose = any(kind == "text" and text.strip() for kind, text in segments)
      if has_prose:
          # 1. The committed bubble IS the streamed segments, VERBATIM and in
          #    arrival order: prose chunks in the claude color, tool lines in the
          #    dim monospace span. reply_text is IGNORED — never concatenated onto
          #    the streamed prose (that would duplicate the model's text).
          parts = [render(kind, text) for kind, text in segments if text.strip()]
      else:
          # 2. FALLBACK: no prose streamed (adapter delivered text only via the
          #    final reply_text). Use reply_text for the prose, but KEEP every
          #    streamed tool line, preserving arrival order (tool lines that
          #    arrived stay; the reply prose renders as a single trailing text
          #    segment). Never fold a terminal status/done line into the reply.
          parts = [render("tool", text) for kind, text in segments if kind == "tool"]
          if reply_text.strip():
              parts.append(render("text", reply_text))
      return "".join(parts)
  ```

  Invariants: (a) prose and `reply_text` are NEVER both rendered — streamed prose wins, `reply_text` is a pure fallback; (b) tool lines always survive (review #18), in arrival order; (c) ordering is arrival order, so prose-before-tool and prose-after-tool both round-trip; (d) terminal status lines (`done` / `done (…s)`) never render in the committed bubble and never count as prose (review #24). So `_on_turn_finished` renders the final bubble via `_commit_bubble_html(self._streaming_segments, reply_text)`, replaces the in-flight bubble with that committed HTML, THEN clears `self._streaming_segments` and bumps the turn count. `_on_turn_failed` (which has no reply) still clears `self._streaming_segments` and drops the half-built bubble (surfacing the error notice instead); only `_on_turn_finished` bumps the turn count. Build the session lazily: if `session is None` and `side_cfg` is truthy, construct `OrchestratorSession(project_dir, side_cfg, timeout_sec)`; if `side_cfg` is falsy, disable input and show "czat niedostępny — brak strony claude". Turn count increments on each `turn_finished`; call `set_header(self._model, self._turn_count)` there (model resolved once from `side_cfg`). Wire `set_running` from `app.py`.

  **Session shutdown ownership (review #2 + #11).** The panel always tears down whatever session it currently holds — there is NO ownership gating of shutdown (an earlier draft claimed `_owns_session` meant injected sessions are "never stopped", which contradicted a `stop_session()` that stops every non-`None` session and a test that asserts an injected fake IS stopped; that flag is dropped). Semantics, stated explicitly: `stop_session(self)` → `if self._session is not None: self._session.stop()`, unconditionally and idempotently. `ConversationSession.stop()` is idempotent (bumps the generation, hands a still-blocked thread to `_ABANDONED_THREADS`, swallows a double-quit `RuntimeError`), so calling it twice — or on an injected fake exposing `stop()` — is safe and intended. `MainWindow.closeEvent` must call `self.chat_panel.stop_session()` so closing the window mid-turn abandons the orchestrator thread cleanly instead of destroying a running `QThread`.

- [ ] **Step 3: wire in `app.py`.** Drive the banner from `_on_state_changed` — NOT from a separate `state_changed` lambda testing only `st == RunnerState.RUNNING` (**review #28**: a live SIBLING spar process — confirmed foreign lock — is derived as `RunnerState.LOCKED`, never RUNNING; the lambda would keep the read-only warning hidden exactly when another process is mutating the repo). Add a module-level set and extend the existing handler:

  ```python
  # A run is LIVE (repo being mutated / read-only advisor caveat applies) in
  # these states. LOCKED = a CONFIRMED foreign spar process holds the lock
  # (review #28). GATE_PENDING is deliberately EXCLUDED: it also covers a
  # DEAD headless process that exited leaving a gate pending (exit 10) — no
  # live run then, and the gate force-open (Task 2) already dominates the UI.
  _CHAT_BANNER_STATES = frozenset({RunnerState.RUNNING, RunnerState.LOCKED})

  def _on_state_changed(self, state: RunnerState) -> None:
      toolbar_mod.apply_state(self.toolbar, state, self._current_status())
      self.chat_panel.set_running(state in _CHAT_BANNER_STATES)
  ```

  Routing through `_on_state_changed` (already connected to `runner.state_changed`, and already invoked once at startup via `_sync_toolbar()` → `_on_state_changed(self.runner.current_state())`) means the banner is also correct IMMEDIATELY at startup — e.g. the GUI opened while a sibling run is live shows the warning without waiting for a state edge. Per the Task 2 init-order block, `self.chat_panel` is built before `_wire_toolbar()`/`_sync_toolbar()` run, so the handler can safely touch it. Remove the `session=None` override so the panel builds its own `OrchestratorSession` from `chat_side_cfg` (unless `None`).

  Add the state-mapping test in `tests/test_gui_app.py` (spy on `set_running` — asserting the mapping, not offscreen widget visibility; the panel-level test already pins `set_running` → banner visibility):

  ```python
  def test_state_changed_drives_chat_banner_for_running_and_locked(self, qtbot, tmp_path):
      # Review #28: LOCKED means a CONFIRMED live sibling spar process — the
      # read-only banner must show for it exactly as for our own RUNNING child,
      # and hide again on non-live states.
      window = MainWindow(tmp_path)
      qtbot.addWidget(window)
      calls = []
      window.chat_panel.set_running = lambda flag: calls.append(flag)
      window._on_state_changed(RunnerState.RUNNING)
      window._on_state_changed(RunnerState.IDLE)
      window._on_state_changed(RunnerState.LOCKED)
      window._on_state_changed(RunnerState.DONE)
      assert calls == [True, False, True, False]
  ```

  **Chat-session shutdown on close (review #2).** Extend `MainWindow.closeEvent` to tear the chat session down BEFORE `super().closeEvent(event)`, mirroring how `GrillDialog.done()` calls `session.stop()`. The persistent orchestrator `QThread` is otherwise never stopped, so closing the window mid-turn could destroy a running `QThread` (Qt qFatal). Add, alongside the existing `self.runner.stop()` line:

  ```python
  self.chat_panel.stop_session()  # idempotent; abandons a mid-turn thread via _ABANDONED_THREADS
  ```

  Add a panel-level idempotency test in `tests/test_gui_orchestrator.py` (pins that `stop_session` stops whatever session the panel holds, twice, safely — review #11: it stops even an injected fake; there is no ownership gate):

  ```python
  def test_stop_session_stops_held_session_idempotently(self, qtbot, tmp_path):
      # Review #2 + #11: stop_session() stops whatever session the panel holds
      # (owned OR injected), and is safe to call twice.
      calls = []

      class BlockedFake(FakeSession):
          def stop(self):
              calls.append("stop")

      fake = BlockedFake()
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      panel.stop_session()
      panel.stop_session()  # idempotent — safe to call twice
      assert calls == ["stop", "stop"]
  ```

  Add the REAL close-wiring test in `tests/test_gui_app.py` (review #11 — the earlier retention test never constructed/closed a `MainWindow`, so deleting the `closeEvent` call would still have passed; this drives `MainWindow.close()` through `closeEvent` and asserts the chat session is stopped):

  ```python
  def test_mainwindow_close_stops_chat_session(self, qtbot, tmp_path):
      window = MainWindow(tmp_path)
      qtbot.addWidget(window)
      calls = []
      real_stop = window.chat_panel.stop_session

      def spy():
          calls.append("stop")
          real_stop()  # still perform the real teardown (abandoned-thread safe)

      window.chat_panel.stop_session = spy
      window.close()  # -> closeEvent -> stop_session()
      assert calls == ["stop"]
  ```

  (Real-thread `_ABANDONED_THREADS` retention for a genuinely blocked turn stays covered for the shared session class by Task 1's `make_probe` teardown / grill's suite — review #11 explicitly allows the shared-session tests to keep that coverage.)

- [ ] **Step 4: suite + commit.** Both interpreters green. Commit — `feat(gui): orchestrator chat panel — read-only advisor with bubbles/options/tool lines`

---

### Task 4: Persistent session via `.spar/chat.json` (Sonnet)

Store the chat session id + metadata; resume on GUI restart; corrupt/missing → fresh; SessionLost mid-conversation → banner + fresh next send.

**Files:**
- Create: `spar/gui/chat_store.py`
- Modify: `spar/gui/orchestrator.py` (load on construct, save after each turn, session-lost recovery)
- Create: `tests/test_gui_chat_store.py`
- Modify: `tests/test_gui_orchestrator.py` (persistence + recovery wiring)

**Interfaces:**

Produces (`spar/gui/chat_store.py`, pure, no Qt):
- `@dataclass(frozen=True) ChatMeta(session_id: str, model: str, turn_count: int)`.
- `def load_chat(path: Path) -> ChatMeta | None` — reads `path` (`.spar/chat.json`); returns `None` on missing / unreadable / malformed / missing `session_id`.
- `def save_chat(path: Path, meta: ChatMeta) -> None` — writes JSON `{"session_id", "model", "turn_count"}`, creating parent dirs; best-effort (swallow `OSError`).

- [ ] **Step 1: failing pure tests** (`tests/test_gui_chat_store.py`, runs on `python3`):

  ```python
  from __future__ import annotations

  import json
  from pathlib import Path

  from spar.gui.chat_store import ChatMeta, load_chat, save_chat


  def test_missing_file_returns_none(tmp_path):
      assert load_chat(tmp_path / ".spar" / "chat.json") is None

  def test_corrupt_file_returns_none(tmp_path):
      p = tmp_path / "chat.json"
      p.write_text("{not json", encoding="utf-8")
      assert load_chat(p) is None

  def test_missing_session_id_returns_none(tmp_path):
      p = tmp_path / "chat.json"
      p.write_text(json.dumps({"model": "opus", "turn_count": 2}), encoding="utf-8")
      assert load_chat(p) is None

  def test_round_trip(tmp_path):
      p = tmp_path / ".spar" / "chat.json"
      save_chat(p, ChatMeta("sess-abc", "opus", 5))
      meta = load_chat(p)
      assert meta == ChatMeta("sess-abc", "opus", 5)
  ```

  Run: `python3 -m pytest tests/test_gui_chat_store.py -q` → **FAIL**.

- [ ] **Step 2: implement `chat_store.py`.**

  ```python
  """Persistence for the orchestrator chat session id + metadata (.spar/chat.json)."""
  from __future__ import annotations

  import json
  from dataclasses import dataclass
  from pathlib import Path


  @dataclass(frozen=True)
  class ChatMeta:
      session_id: str
      model: str
      turn_count: int


  def load_chat(path: "str | Path") -> "ChatMeta | None":
      """Load chat metadata; None on missing/unreadable/malformed/no session_id."""
      try:
          raw = Path(path).read_text(encoding="utf-8")
          obj = json.loads(raw)
      except (OSError, ValueError):
          return None
      if not isinstance(obj, dict):
          return None
      session_id = obj.get("session_id")
      if not isinstance(session_id, str) or not session_id:
          return None
      model = obj.get("model") if isinstance(obj.get("model"), str) else ""
      turn_count = obj.get("turn_count") if isinstance(obj.get("turn_count"), int) else 0
      return ChatMeta(session_id=session_id, model=model, turn_count=turn_count)


  def save_chat(path: "str | Path", meta: ChatMeta) -> None:
      """Best-effort write of chat metadata; swallow filesystem errors."""
      p = Path(path)
      try:
          p.parent.mkdir(parents=True, exist_ok=True)
          p.write_text(
              json.dumps(
                  {"session_id": meta.session_id, "model": meta.model,
                   "turn_count": meta.turn_count}
              ),
              encoding="utf-8",
          )
      except OSError:
          pass


  def discard_chat(path: "str | Path") -> None:
      """Best-effort deletion of chat metadata; swallow filesystem errors.

      Review #35: called from Qt recovery slots (null-session-id turn,
      session_lost) — a raised OSError would abort the slot mid-recovery,
      leaving input disabled and flags stale.
      """
      try:
          Path(path).unlink(missing_ok=True)
      except OSError:
          pass
  ```

  Run → **PASSES**.

- [ ] **Step 3: failing panel persistence tests** in `tests/test_gui_orchestrator.py` (fake session with settable `session_id`):

  ```python
  def test_resumes_persisted_session_and_skips_opening_prompt(self, qtbot, tmp_path):
      from spar.gui.chat_store import ChatMeta, save_chat
      save_chat(tmp_path / ".spar" / "chat.json", ChatMeta("sess-x", "opus", 4))
      fake = FakeSession()
      fake.session_id = "sess-x"  # simulate a session constructed with initial id
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      panel.input_edit.setPlainText("kontynuuj")
      panel.send_button.click()
      # A resumed session skips the opening prompt -> plain resume send.
      assert fake.sends[0] == ("kontynuuj", False)

  def test_turn_finished_persists_chat_json(self, qtbot, tmp_path):
      from spar.gui.chat_store import load_chat
      fake = FakeSession()
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      panel.input_edit.setPlainText("q")
      panel.send_button.click()
      fake.session_id = "sess-new"
      fake.turn_finished.emit("ok", [])
      meta = load_chat(tmp_path / ".spar" / "chat.json")
      assert meta is not None and meta.session_id == "sess-new"
      assert meta.turn_count == 1

  def test_turn_with_none_session_id_rearms_opening_and_skips_persist(self, qtbot, tmp_path):
      # Review #30: the adapter contract permits a successful turn with
      # TurnResult.session_id = None. Such a turn is NON-RESUMABLE: promoting
      # _opening_sent would make the next send a bare resume while the worker
      # starts fresh (stranding the new session without the advisor contract),
      # and persisting would write a null session id. So: no promotion, no
      # chat.json, and the next send re-carries OPENING_PROMPT with reset=True.
      from spar.gui.orchestrator import OPENING_PROMPT
      fake = FakeSession()
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      panel.input_edit.setPlainText("pierwsze")
      panel.send_button.click()
      assert fake.session_id is None      # adapter yielded no session id
      fake.turn_finished.emit("ok", [])
      assert not (tmp_path / ".spar" / "chat.json").exists()  # nothing persisted
      panel.input_edit.setPlainText("drugie")
      panel.send_button.click()
      sent_text, reset = fake.sends[-1]
      assert reset is True                                # fresh session again
      assert OPENING_PROMPT.split("\n")[0] in sent_text   # opening re-armed
      assert "drugie" in sent_text

  def test_null_session_id_after_resume_deletes_stale_chat_json(self, qtbot, tmp_path):
      # Review #34: skipping save_chat is NOT enough when the session was
      # RESUMED from persisted metadata — the stale chat.json from the previous
      # launch still exists. If the null-id branch leaves it in place, the next
      # GUI launch reloads the dead id and treats the opening as already
      # delivered (bare resume against a fresh worker session). The branch must
      # DELETE the stale file so a fresh launch re-arms the opening.
      from spar.gui.chat_store import ChatMeta, load_chat, save_chat
      from spar.gui.orchestrator import OPENING_PROMPT
      chat_path = tmp_path / ".spar" / "chat.json"
      save_chat(chat_path, ChatMeta("sess-stale", "opus", 4))
      fake = FakeSession()
      fake.session_id = "sess-stale"
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      panel.input_edit.setPlainText("kontynuuj")
      panel.send_button.click()
      assert fake.sends[0] == ("kontynuuj", False)   # resumed: opening skipped
      fake.session_id = None                          # resumed turn came back id-less
      fake.turn_finished.emit("ok", [])
      assert not chat_path.exists()                   # stale metadata removed
      assert load_chat(chat_path) is None
      # Next-launch equivalent: a fresh panel over the same project dir finds no
      # metadata, so its first send must re-arm the opening contract.
      fake2 = FakeSession()
      panel2 = OrchestratorChatPanel(tmp_path, object(), 60, session=fake2)
      qtbot.addWidget(panel2)
      panel2.input_edit.setPlainText("znowu")
      panel2.send_button.click()
      sent_text, reset = fake2.sends[0]
      assert reset is True
      assert OPENING_PROMPT.split("\n")[0] in sent_text
      assert "znowu" in sent_text

  def test_discard_chat_swallows_oserror(self, tmp_path, monkeypatch):
      # Review #35: deletion is best-effort — an OSError from unlink must not
      # propagate out of the helper (it would abort the Qt recovery slot,
      # leaving input disabled and flags stale).
      from spar.gui.chat_store import discard_chat
      chat_path = tmp_path / "chat.json"
      chat_path.write_text("{}")
      def boom(*a, **k):
          raise OSError("read-only fs")
      monkeypatch.setattr(type(chat_path), "unlink", boom)
      discard_chat(chat_path)              # no raise
      discard_chat(tmp_path / "missing")   # missing file: also no raise

  def test_null_session_id_recovery_survives_deletion_failure(self, qtbot, tmp_path, monkeypatch):
      # Review #35: even when discard_chat cannot delete the stale file, the
      # recovery path must complete — input re-enabled, opening re-armed.
      from pathlib import Path
      from spar.gui.chat_store import ChatMeta, save_chat
      from spar.gui.orchestrator import OPENING_PROMPT
      chat_path = tmp_path / ".spar" / "chat.json"
      save_chat(chat_path, ChatMeta("sess-stale", "opus", 4))
      def boom(*a, **k):
          raise OSError("busy")
      monkeypatch.setattr(Path, "unlink", boom)
      fake = FakeSession()
      fake.session_id = "sess-stale"
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      panel.input_edit.setPlainText("kontynuuj")
      panel.send_button.click()
      fake.session_id = None
      fake.turn_finished.emit("ok", [])               # deletion fails inside — no crash
      assert panel.input_edit.isEnabled()              # slot completed
      assert panel.send_button.isEnabled()
      panel.input_edit.setPlainText("dalej")
      panel.send_button.click()
      sent_text, reset = fake.sends[-1]
      assert reset is True                             # opening re-armed despite stale file
      assert OPENING_PROMPT.split("\n")[0] in sent_text

  def test_null_session_id_resets_opening_and_gate_fingerprint(self, qtbot, tmp_path):
      # Review #37: a resumed panel seeded _opening_sent=True; a null-id turn
      # must re-arm BOTH the opening contract and the delivered-gate key —
      # the dead session took its delivered context with it.
      from spar.gui.chat_store import ChatMeta, save_chat
      from spar.gui.orchestrator import OPENING_PROMPT
      save_chat(tmp_path / ".spar" / "chat.json", ChatMeta("sess-stale", "opus", 4))
      fake = FakeSession()
      fake.session_id = "sess-stale"
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      gate = {"name": "review_rounds", "options": ["accept"],
              "context": {"task_id": "t1", "rounds": 3}}
      panel.on_status({"pending_gate": gate, "phase": "exec"})
      panel.input_edit.setPlainText("co robić?")
      panel.send_button.click()                        # resumed: gate context injected
      assert "t1" in fake.sends[0][0]
      fake.session_id = None
      fake.turn_finished.emit("ok", [])                # non-resumable turn
      assert panel._opening_sent is False              # review #37
      assert panel._injected_gate_key is None          # review #37
      panel.input_edit.setPlainText("no więc?")
      panel.send_button.click()                        # same gate still pending
      sent_text, reset = fake.sends[-1]
      assert reset is True
      assert OPENING_PROMPT.split("\n")[0] in sent_text  # opening re-delivered
      assert "t1" in sent_text                           # gate context re-injected

  def test_session_lost_recovery_survives_deletion_failure(self, qtbot, tmp_path, monkeypatch):
      # Review #35/#36: the OTHER recovery path — _on_session_lost must also
      # complete its re-enable/re-arm cleanup when discard_chat cannot delete.
      from pathlib import Path
      from spar.gui.chat_store import ChatMeta, save_chat
      from spar.gui.orchestrator import OPENING_PROMPT
      save_chat(tmp_path / ".spar" / "chat.json", ChatMeta("sess-x", "opus", 2))
      def boom(*a, **k):
          raise OSError("busy")
      monkeypatch.setattr(Path, "unlink", boom)
      fake = FakeSession()
      fake.session_id = "sess-x"
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      panel.input_edit.setPlainText("pytanie")
      panel.send_button.click()                        # in-flight: input disabled
      fake.session_lost.emit()                         # deletion fails inside — no crash
      assert panel.input_edit.isEnabled()              # slot completed cleanup
      assert panel.send_button.isEnabled()
      panel.input_edit.setPlainText("retry")
      panel.send_button.click()
      sent_text, reset = fake.sends[-1]
      assert reset is True                             # fresh first turn re-armed
      assert OPENING_PROMPT.split("\n")[0] in sent_text

  def test_session_lost_mid_turn_reenables_then_fresh_first_turn(self, qtbot, tmp_path):
      from spar.gui.orchestrator import OPENING_PROMPT
      fake = FakeSession()
      fake.session_id = "sess-x"
      panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
      qtbot.addWidget(panel)
      panel.show()
      # Simulate a resumed session (opening already ran) so we prove the LOSS
      # re-arms the opening, not merely a first send.
      panel._opening_sent = True
      # Review #16: a loss can arrive mid-turn. Send first so input/send are
      # disabled in flight; the loss must clear that disable, not brick the chat.
      panel.input_edit.setPlainText("pierwsze")
      panel.send_button.click()
      assert panel.input_edit.isEnabled() is False   # in-flight disable
      assert panel.send_button.isEnabled() is False
      fake.session_lost.emit()
      assert panel.banner.isVisible() is True
      assert panel.input_edit.isEnabled() is True     # loss re-enabled input
      assert panel.send_button.isEnabled() is True
      panel.input_edit.setPlainText("znowu")
      panel.send_button.click()
      sent_text, reset = fake.sends[-1]
      # Review #5 + #17: the first send after a loss is a FRESH-session first
      # turn — it must carry the read-only OPENING_PROMPT contract (advisor / no
      # gate decisions / ```zadanie``` marker) with reset=True, not the bare
      # user text (opening was re-armed because the lost turn never committed it).
      assert reset is True
      assert OPENING_PROMPT.split("\n")[0] in sent_text
      assert "znowu" in sent_text
  ```

- [ ] **Step 4: implement persistence in the panel.** On construct: `meta = load_chat(project_dir/'.spar'/'chat.json')`; if the panel builds its own `OrchestratorSession`, pass `initial_session_id=meta.session_id` (and seed `self._turn_count = meta.turn_count`, `self._model = meta.model or resolved`). Treat "opening prompt needed" as: `self._opening_sent = meta is not None` (a resumed session already ran its opening — the very reason the dispatch helper skips `OPENING_PROMPT` on resume). In `turn_finished`: **Review #30 — first check `session.session_id`. If it is falsy (`None`/empty — the adapter contract permits a successful turn without a session id), the turn is NON-RESUMABLE: clear both pending fields WITHOUT promoting, and **review #37: explicitly re-arm the contract exactly as session-loss recovery does — `self._opening_sent = False` and `self._injected_gate_key = None`** (clearing pendings alone is NOT enough: a panel resumed from persisted metadata seeded `_opening_sent = True` at construction, and `_injected_gate_key` may hold context delivered only to the now-dead session) and do NOT `save_chat` (never persist a null session id) — **review #34: additionally DELETE any existing persisted metadata via the best-effort helper `discard_chat(path)` (defined in Task 4 Step 2 next to `load_chat`/`save_chat`: `def discard_chat(path): \n    try: path.unlink(missing_ok=True)\n    except OSError: pass` — review #35: a raw `unlink` can raise `OSError` and abort the Qt slot mid-recovery, leaving input disabled and flags stale)**, because when the panel was constructed from a persisted id a stale `chat.json` from the previous launch still exists; merely skipping the save would let the next GUI launch reload that stale id, construct the session with `initial_session_id` and `self._opening_sent = True`, and send a bare resume against a dead session with the opening treated as already delivered; still commit the bubble, bump `self._turn_count`, and refresh the header. The next `_dispatch_user_text` then sends opening prompt (+ gate context) + user text with `reset=True`, matching the worker, which also starts fresh when it holds no session id.** Otherwise (truthy id): **first promote the pending flags (review #17 — `if self._pending_opening: self._opening_sent = True`; `if self._pending_gate_key is not None: self._injected_gate_key = self._pending_gate_key`; then clear both pending fields)**; `self._turn_count += 1`; `save_chat(...)` with `session.session_id`; refresh header. On `session_lost` (`_on_session_lost`): **Review #16 — apply the SAME in-flight cleanup/re-enable as `_on_turn_failed`**, because a loss can arrive mid-turn with input/send disabled and half-built streaming state: clear `self._streaming_segments`, drop the half-built bot bubble, re-enable `input_edit`/`send_button`, and restore the header — otherwise a loss during a live turn permanently bricks the chat exactly as an unhandled `AdapterError` would. Then show the banner "sesja utracona — nowa zostanie utworzona", set `self._session_lost = True`, delete the stale `chat.json` via the same best-effort `discard_chat` helper (review #35 — deletion failure must not abort the recovery slot), **set `self._opening_sent = False`** so the next send is a genuine fresh-session FIRST turn, and **Review #17 — also reset `self._injected_gate_key = None` and clear the pending fields (`self._pending_opening = False`, `self._pending_gate_key = None`)**: the shared session was reset, so any previously delivered gate context is gone and must be free to re-inject, and no stale delivered-gate key may survive the loss. Review #5: recovery must NOT send bare user text — the shared session was reset, so the new claude session needs the read-only `OPENING_PROMPT` contract (advisor / read-only / no gate decisions / ` ```zadanie``` ` marker) exactly as a first turn does. Because `_dispatch_user_text` already composes `OPENING_PROMPT` (when `not self._opening_sent`) + gate context (Task 5, when a gate pends) + user text with `reset=needs_opening`, a lost session's next send automatically carries opening prompt → optional gate context → user text with `reset=True`; no separate reset-only code path. The dispatch helper clears the lost flag/banner (`self._session_lost = False`, `set_running(self._is_running)` to restore the correct banner state) as part of sending. Reuse the RUNNING banner label or a second banner — use a distinct message via the same `banner` label state machine (RUNNING vs lost are mutually exclusive enough; if clarity needs it, add a second `QLabel` — keep it simple with one banner and a priority: lost > running).

- [ ] **Step 5: suite + commit.** Both interpreters green. Commit — `feat(gui): persist orchestrator chat session in .spar/chat.json with resume + loss recovery`

---

### Task 5: Silent gate-context injection (Sonnet)

When a gate pends, the chat's next turn silently carries the gate context (type, task id, test output) so the user can ask "co byś wybrał" — without the chat ever taking the decision.

**Files:**
- Modify: `spar/gui/orchestrator.py` (Qt-free `build_gate_context` + `_gate_fingerprint` + injection in the panel)
- Modify: `spar/gui/app.py` (forward `side_pane.status_changed` → `chat_panel.on_status` — connected BEFORE the single initial `side_pane.refresh()`, review #29)
- Modify: `tests/test_gui_orchestrator.py` (Qt injection tests)
- Modify: `tests/test_gui_app.py` (startup-pending-gate reaches the chat panel — review #29)
- Modify: `tests/test_gui_orchestrator_pure.py` (pure `build_gate_context` tests — NO `importorskip`, must run under `python3`; review #12)

**Interfaces:**

Both `build_gate_context` and `_gate_fingerprint` are **Qt-free** and live at the module top of `orchestrator.py`, ABOVE the `if _HAS_QT:` guard (mirroring `conversation.py`'s `Option`/`parse_options`), so they import and are tested on a plain interpreter.

Produces:
- `def build_gate_context(pending_gate: dict | None) -> str` — pure; `""` when `pending_gate` is `None`. Otherwise renders the **COMPLETE** gate/context payload the GUI receives (review #10). The real review-gate evidence — including the failing per-task-test output — does NOT live in a top-level `summary`; it lives in `context["open_remarks"][*]["text"]` (headless.py:74/81 builds `open_remarks` as `[{id, severity, author, text}]`, and the `review_rounds` gate also sets `reason` and optionally `command`; the consensus/rounds-exhausted gates use `nice_backlog`/`open_remarks` + `artifact`). Fingerprinting only `task_id/rounds/summary/command` (the previous draft) would drop that text entirely, so the advisor could get neither the test output nor changed remarks. Mirror `SidePane._format_gate_context` (sidepane.py:~530-555) as the field set. Render, when present in `context`, in order: `task_id`, `rounds`, `reason`, `summary`, `artifact`, `command`, then every remark from `open_remarks` **or** `nice_backlog` rendered as `[<severity>] (<author>) <text>`. Each free-text body (`summary` and each remark `text`) is truncated to 2000 chars (mirroring the engine's headless gate truncation). Wrap in a read-only header, e.g.:

  ```
  [KONTEKST BRAMKI — tylko do wglądu, NIE podejmuj decyzji]
  typ: <name>
  task: <task_id>  rundy: <rounds>  powód: <reason>
  podsumowanie:
  <summary, ≤2000 chars>
  plan: <artifact>
  komenda: <command>
  uwagi:
  [BLOCKER] (review) <remark text, ≤2000 chars>
  ...
  ```
  (Only lines whose field is present are emitted.)
- Panel: `on_status(self, status: dict) -> None` — stores `self._pending_gate = status.get("pending_gate")`. On the NEXT send while a gate pends and the context hasn't already been injected for this gate, PREPEND `build_gate_context(pending_gate)` (via the `_dispatch_user_text` composition) to the dispatched prompt (the user's visible bubble shows ONLY their typed text; the injected context is invisible in the transcript).
- **Dedup fingerprint (reviews #6 + #10).** `(name, task_id)` is NOT a valid identity (the SAME task re-reaches the SAME gate with NEW evidence after an `extend`/retry), and neither is a tuple over only `name/task_id/rounds/summary/command` — review #10: that omits `open_remarks[*].text`, where the failing-test output and changed remarks live, so a re-reached gate whose ONLY change is remark text would never re-inject. Fingerprint the **COMPLETE rendered context**: `def _gate_fingerprint(pending_gate) -> str` returns `build_gate_context(pending_gate)` (the fully rendered block already folds in `name` + every rendered context field incl. `open_remarks`/`nice_backlog`/`reason`/`artifact`/`command`). Store `self._injected_gate_key`; inject only when the current fingerprint differs from it. **Review #17: do NOT record the fingerprint at dispatch — stash it in `self._pending_gate_key` and let `_on_turn_finished` promote it to `self._injected_gate_key` only on success**, so a gate context whose turn fails or whose session is lost is never counted as delivered and re-injects on the retry. RESET the guard whenever the gate clears — in `on_status`, `if not pending_gate: self._injected_gate_key = None; self._pending_gate_key = None`. **Review #22: the clear MUST also drop `self._pending_gate_key`, not only `self._injected_gate_key`.** Otherwise an in-flight race corrupts the guard: a context-bearing turn is running (so `_pending_gate_key` holds its fingerprint, not yet promoted); the gate clears mid-turn → `on_status` resets `_injected_gate_key` but leaves the stale `_pending_gate_key`; the turn then completes and `_on_turn_finished` promotes that stale fingerprint into `_injected_gate_key`. When the SAME gate is re-reached it now matches the promoted key and is wrongly treated as already delivered — never re-injected. Clearing `_pending_gate_key` on gate-clear invalidates the pending promotion so `_on_turn_finished` has nothing stale to promote (equivalently, promote only if the current gate fingerprint still matches — clearing is the simpler invariant). A re-reached gate with changed evidence (rounds OR remark text) → different rendered block → re-injected; the same unchanged gate re-polled every 2s → identical block → injected once.

- [ ] **Step 1: failing tests.** Pure `build_gate_context` coverage goes in `tests/test_gui_orchestrator_pure.py` (NO `importorskip` — review #12: a pure helper tested under the Qt-file's module-level `pytest.importorskip("PySide6")` silently SKIPS on plain `python3`, the same false-green fixed for rails in #8). The Qt `TestGateInjection` stays in `tests/test_gui_orchestrator.py`.

  `tests/test_gui_orchestrator_pure.py` (runs on any interpreter):

  ```python
  from __future__ import annotations

  from spar.gui.orchestrator import build_gate_context


  class TestGateContext:
      def test_empty_when_no_gate(self):
          assert build_gate_context(None) == ""

      def test_includes_type_task_and_summary(self):
          gate = {"name": "review_rounds", "options": ["accept", "abort"],
                  "context": {"task_id": "t3", "summary": "FAILED: 2 tests"}}
          out = build_gate_context(gate)
          assert "review_rounds" in out and "t3" in out and "FAILED: 2 tests" in out
          assert "NIE podejmuj decyzji" in out

      def test_includes_open_remarks_failing_output(self):
          # Review #10: the failing per-task-test output lives in open_remarks,
          # NOT in a top-level summary. build_gate_context must render it.
          gate = {"name": "review_rounds", "context": {
              "task_id": "t1", "rounds": 3, "reason": "test_escalation",
              "command": "pytest -q",
              "open_remarks": [
                  {"id": 0, "severity": "USER", "author": "per-task-test",
                   "text": "per-task test FAILING. Last captured output:\nE assert 1 == 2"},
              ],
          }}
          out = build_gate_context(gate)
          assert "test_escalation" in out
          assert "pytest -q" in out
          assert "E assert 1 == 2" in out
          assert "per-task-test" in out

      def test_includes_nice_backlog_remarks(self):
          gate = {"name": "consensus", "context": {
              "artifact": "docs/plan.md",
              "nice_backlog": [
                  {"id": 1, "severity": "NICE", "author": "review", "text": "tidy names"},
              ],
          }}
          out = build_gate_context(gate)
          assert "docs/plan.md" in out and "tidy names" in out

      def test_truncates_long_output(self):
          gate = {"name": "g", "context": {"summary": "x" * 5000}}
          # summary truncated to 2000 chars -> whole block well under 2500.
          assert len(build_gate_context(gate)) < 2500

      def test_truncates_long_remark_text(self):
          gate = {"name": "g", "context": {
              "open_remarks": [{"severity": "USER", "author": "a", "text": "y" * 5000}],
          }}
          assert build_gate_context(gate).count("y") <= 2000
  ```

  `tests/test_gui_orchestrator.py` (Qt injection section):

  ```python
  class TestGateInjection:  # in the Qt panel section
      def test_next_send_injects_gate_context_silently(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.on_status({"pending_gate": {"name": "consensus",
                                            "context": {"task_id": "t1", "summary": "OUT"}}})
          panel.input_edit.setPlainText("co byś wybrał?")
          panel.send_button.click()
          sent_text, _reset = fake.sends[0]
          assert "co byś wybrał?" in sent_text
          assert "consensus" in sent_text and "OUT" in sent_text  # context injected
          # The visible transcript shows only the user's words, not the context.
          assert "consensus" not in panel.transcript.toPlainText()

      def test_failed_turn_reinjects_gate_context(self, qtbot, tmp_path):
          # Review #17: a gate context whose turn FAILS was never delivered, so
          # its fingerprint must NOT be recorded — the retry has to re-inject it.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          gate = {"name": "consensus", "context": {"summary": "OUT"}}
          panel.on_status({"pending_gate": gate})
          panel.input_edit.setPlainText("q1")
          panel.send_button.click()
          assert "OUT" in fake.sends[0][0]  # injected on the first (doomed) send
          fake.turn_failed.emit("boom")     # turn failed -> not committed
          panel.on_status({"pending_gate": gate})  # same gate still pending
          panel.input_edit.setPlainText("q2")
          panel.send_button.click()
          assert "OUT" in fake.sends[-1][0]  # re-injected: never counted delivered

      def test_context_injected_once_per_gate(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          gate = {"name": "consensus", "context": {"summary": "OUT"}}
          panel.on_status({"pending_gate": gate})
          panel.input_edit.setPlainText("q1")
          panel.send_button.click()
          fake.session_id = "sess-1"  # review #33: resumable turn promotes the gate fingerprint
          fake.turn_finished.emit("ok", [])
          panel.on_status({"pending_gate": gate})  # same gate still pending
          panel.input_edit.setPlainText("q2")
          panel.send_button.click()
          assert "OUT" in fake.sends[0][0]
          assert "OUT" not in fake.sends[1][0]  # not re-injected same gate

      def test_rereached_gate_with_new_output_reinjects(self, qtbot, tmp_path):
          # Review #6: the SAME task/gate after extend/retry carries NEW output;
          # (name, task_id) would wrongly dedup it. A changed payload fingerprint
          # must re-inject so the advisor sees the fresh output.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.on_status({"pending_gate": {"name": "review_rounds",
                                            "context": {"task_id": "t1", "rounds": 2,
                                                        "summary": "FAIL v1"}}})
          panel.input_edit.setPlainText("q1")
          panel.send_button.click()
          assert "FAIL v1" in fake.sends[0][0]
          # Review #25: complete the first turn — otherwise the panel stays
          # disabled in-flight and the second send never dispatches.
          # Review #33: truthy id so the fingerprint is PROMOTED — the re-inject
          # below then proves the CHANGED payload, not a never-recorded one.
          fake.session_id = "sess-1"
          fake.turn_finished.emit("ok", [])
          # gate clears, then the same task re-reaches the gate with new output
          panel.on_status({"pending_gate": None})
          panel.on_status({"pending_gate": {"name": "review_rounds",
                                            "context": {"task_id": "t1", "rounds": 4,
                                                        "summary": "FAIL v2"}}})
          panel.input_edit.setPlainText("q2")
          panel.send_button.click()
          assert "FAIL v2" in fake.sends[1][0]  # re-injected: fingerprint changed

      def test_rereached_gate_with_only_changed_remark_text_reinjects(self, qtbot, tmp_path):
          # Review #10: same name/task_id/rounds but the failing-test evidence in
          # open_remarks changed. A fingerprint over name/task_id/rounds/summary/
          # command alone would MISS this; fingerprinting the complete rendered
          # context (incl. open_remarks text) re-injects.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          base = {"name": "review_rounds",
                  "context": {"task_id": "t1", "rounds": 3,
                              "open_remarks": [{"severity": "USER", "author": "per-task-test",
                                                "text": "OUTPUT v1"}]}}
          panel.on_status({"pending_gate": base})
          panel.input_edit.setPlainText("q1")
          panel.send_button.click()
          assert "OUTPUT v1" in fake.sends[0][0]
          # Review #25: complete the first turn — otherwise the panel stays
          # disabled in-flight and the second send never dispatches.
          # Review #33: truthy id -> fingerprint promoted; re-inject proves the
          # changed remark text, not a skipped promotion.
          fake.session_id = "sess-1"
          fake.turn_finished.emit("ok", [])
          panel.on_status({"pending_gate": None})
          changed = {"name": "review_rounds",
                     "context": {"task_id": "t1", "rounds": 3,
                                 "open_remarks": [{"severity": "USER", "author": "per-task-test",
                                                   "text": "OUTPUT v2"}]}}
          panel.on_status({"pending_gate": changed})
          panel.input_edit.setPlainText("q2")
          panel.send_button.click()
          assert "OUTPUT v2" in fake.sends[1][0]  # re-injected: remark text differs

      def test_gate_cleared_mid_turn_does_not_corrupt_dedup(self, qtbot, tmp_path):
          # Review #22: a gate clears WHILE its context-bearing turn is still in
          # flight. on_status must invalidate the pending promotion so the later
          # turn_finished cannot promote a stale fingerprint. If it did, the SAME
          # gate re-reached afterwards would be wrongly deduped and never re-inject.
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          gate = {"name": "consensus", "context": {"summary": "OUT"}}
          panel.on_status({"pending_gate": gate})
          panel.input_edit.setPlainText("q1")
          panel.send_button.click()
          assert "OUT" in fake.sends[0][0]          # injected; fingerprint pending
          panel.on_status({"pending_gate": None})   # gate clears mid-turn
          # Review #33: truthy id so turn_finished takes the RESUMABLE branch —
          # the branch that would wrongly promote the stale fingerprint if
          # on_status had not invalidated it (the very bug of review #22).
          fake.session_id = "sess-1"
          fake.turn_finished.emit("ok", [])         # turn completes AFTER the clear
          # Same gate re-reached: must re-inject (pending promotion was invalidated).
          panel.on_status({"pending_gate": gate})
          panel.input_edit.setPlainText("q2")
          panel.send_button.click()
          assert "OUT" in fake.sends[1][0]          # re-injected, not stale-deduped
  ```

- [ ] **Step 2: implement** the Qt-free `build_gate_context` + `_gate_fingerprint` (both above the `if _HAS_QT:` guard) + panel injection. `_gate_fingerprint(pending_gate)` returns `build_gate_context(pending_gate)` — the complete rendered block — so any changed field (rounds, summary, command, OR `open_remarks`/`nice_backlog` text) yields a new fingerprint (reviews #6 + #10). In `_dispatch_user_text` (the single send path from Task 3), when a gate pends and `_gate_fingerprint(self._pending_gate) != self._injected_gate_key`, include the context block and set `self._pending_gate_key` to that fingerprint (review #17: it is promoted to `self._injected_gate_key` ONLY by `_on_turn_finished`, so a context that never ships re-injects on retry; `_on_turn_failed`/`_on_session_lost` clear the pending key without promoting); when the gate clears in `on_status`, reset BOTH `self._injected_gate_key = None` and `self._pending_gate_key = None` (review #22 — clearing only `_injected_gate_key` lets a still-pending fingerprint from an in-flight turn be promoted after the gate cleared, corrupting the guard so the same gate re-reached is wrongly deduped). The user bubble still appends only `user_text`. Composition order in `_dispatch_user_text`: opening prompt (when `not self._opening_sent`), then gate context, then user text.

- [ ] **Step 3: wire status in `app.py`.** `self.side_pane.status_changed.connect(self.chat_panel.on_status)` — and the PLACEMENT is load-bearing (**review #29**): make this connection BEFORE the single initial `self.side_pane.refresh()`, i.e. inside the Task 2 init-order block's "build `chat_panel` + `right_column` + rails + wiring" section that precedes the final `refresh()` (consistent with how `status_changed → _on_status_changed` is already connected pre-refresh). That initial `refresh()` fires `status_changed` SYNCHRONOUSLY and is the only startup delivery of a gate that is ALREADY pending when the GUI opens; connected after it, `chat_panel._pending_gate` stays `None` until the next 2s poll, so a user who opens the GUI onto a pending gate and immediately asks the chat gets NO gate context injected into that first turn. Resulting `__init__` sequence (Task 2 block, extended): connect `side_pane.status_changed → _on_status_changed` → build `chat_panel`/rails/splitter → **connect `side_pane.status_changed → chat_panel.on_status`** → … → THEN the single `self.side_pane.refresh()`.

  Add the startup test in `tests/test_gui_app.py` (seed a pending gate on disk the way `test_state.py::test_pending_gate_round_trip` does, so `build_status` reports it during `MainWindow.__init__`):

  ```python
  def test_startup_pending_gate_reaches_chat_panel(self, qtbot, tmp_path):
      # Review #29: a gate ALREADY pending when the GUI starts is delivered by
      # the single initial side_pane.refresh() (synchronous status_changed).
      # The on_status connection must exist BEFORE that refresh — otherwise
      # the first chat turn misses the gate context until the next 2s poll.
      from spar.state import DebateState, StateStore

      spar_dir = tmp_path / ".spar"
      spar_dir.mkdir()
      state = DebateState()
      state.pending_gate = {"name": "consensus", "options": ["accept", "abort"],
                            "context": {"summary": "STARTUP-GATE"}}
      StateStore(spar_dir).save(state)

      window = MainWindow(tmp_path)
      qtbot.addWidget(window)
      # Delivered at construction time — no poll tick, no manual refresh here.
      # (A bare tmp project has no chat_side_cfg, so the panel holds no real
      # session and its input is disabled — the ordering proof is the stored
      # gate itself; the "next send carries it" half is pinned at panel level
      # below, where a fake session can be injected.)
      assert window.chat_panel._pending_gate is not None
      assert window.chat_panel._pending_gate["name"] == "consensus"
  ```

  And pin the "next send carries a startup-pending gate" half at panel level in `tests/test_gui_orchestrator.py` (`TestGateInjection`):

  ```python
  def test_gate_pending_before_first_send_is_injected(self, qtbot, tmp_path):
      # Review #29 (panel half): a gate delivered via on_status BEFORE any
      # send — the startup case — must be injected into the first turn.
      fake = FakeSession()
      panel = _panel(qtbot, tmp_path, fake)
      panel.on_status({"pending_gate": {"name": "consensus",
                                        "context": {"summary": "STARTUP-GATE"}}})
      panel.input_edit.setPlainText("co z bramką?")
      panel.send_button.click()
      assert "STARTUP-GATE" in fake.sends[0][0]
  ```

- [ ] **Step 4: suite + commit.** Both interpreters green; confirm `python3 -m pytest tests/test_gui_orchestrator_pure.py -q` actually RUNS (not skips) the `build_gate_context` tests. Commit — `feat(gui): silently inject pending-gate context into the orchestrator chat's next turn`

---

### Task 6: Task-draft handoff → prefilled NewDebateDialog (Sonnet)

A ` ```zadanie … ``` ` block in the orchestrator's reply surfaces a green "Nowa debata z tym szkicem" button that opens a prefilled `NewDebateDialog` — enabled only while the engine is free.

**Files:**
- Modify: `spar/gui/orchestrator.py` (Qt-free `parse_task_draft` + green button + `set_engine_free`)
- Modify: `spar/gui/app.py` (open prefilled dialog; drive `set_engine_free` from `state_changed`)
- Modify: `tests/test_gui_orchestrator.py` (Qt handoff tests)
- Modify: `tests/test_gui_orchestrator_pure.py` (pure `parse_task_draft` tests — NO `importorskip`, must run under `python3`; review #12)

**Interfaces:**

Produces:
- `def parse_task_draft(reply_text: str) -> str | None` — **Qt-free**, defined at the module top of `orchestrator.py` above the `if _HAS_QT:` guard (so it imports/tests on a plain interpreter); returns the inner text of the LAST ` ```zadanie … ``` ` fenced block (trimmed), or `None` when absent. Fence open matches `^```zadanie\s*$` (tolerant of trailing spaces), closes on `^```\s*$`.
- Panel:
  - `handoff_button` (`objectName="handoffButton"`, text "Nowa debata z tym szkicem", green via `TOKENS['ok']`, hidden until a draft is parsed). On `turn_finished`, if `parse_task_draft(reply)` is non-None, store `self._draft` and show the button (enabled per `self._engine_free`).
  - `set_engine_free(self, free: bool) -> None` — stores the flag and enables/disables the handoff button.
  - Signal `handoff_requested = Signal(str)` — emitted with the draft when the button is clicked (the panel does NOT construct the dialog itself; `app.py` owns dialog/runner wiring).

- [ ] **Step 1: failing tests.** Pure `parse_task_draft` coverage goes in `tests/test_gui_orchestrator_pure.py` (NO `importorskip`, review #12 — same false-green trap as the gate helper); the Qt `TestHandoff` stays in `tests/test_gui_orchestrator.py`.

  `tests/test_gui_orchestrator_pure.py` (append):

  ```python
  from spar.gui.orchestrator import OPENING_PROMPT, parse_task_draft

  class TestParseTaskDraft:
      def test_none_when_absent(self):
          assert parse_task_draft("zwykła odpowiedź") is None

      def test_opening_prompt_format_example_parses(self):
          # Review #31: prompt/parser contract — the multiline format example
          # embedded verbatim in OPENING_PROMPT must itself parse with
          # parse_task_draft, so the prompt can never teach the model a
          # draft format the parser rejects.
          assert parse_task_draft(OPENING_PROMPT) == "<treść szkicu zadania>"

      def test_extracts_fenced_block(self):
          reply = "Oto szkic:\n\n```zadanie\nZbuduj X\n\n## Tasks\n- a\n```\ndaj znać"
          assert parse_task_draft(reply) == "Zbuduj X\n\n## Tasks\n- a"

      def test_last_block_wins(self):
          reply = "```zadanie\nstary\n```\n...\n```zadanie\nnowy\n```"
          assert parse_task_draft(reply) == "nowy"
  ```

  `tests/test_gui_orchestrator.py` (Qt handoff section):

  ```python
  class TestHandoff:  # Qt panel section
      def test_draft_reply_shows_green_button_when_engine_free(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.show()
          panel.set_engine_free(True)
          fake.turn_finished.emit("```zadanie\nZbuduj X\n```", [])
          assert panel.handoff_button.isVisible() is True
          assert panel.handoff_button.isEnabled() is True

      def test_button_disabled_when_engine_busy(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.show()
          panel.set_engine_free(False)
          fake.turn_finished.emit("```zadanie\nZbuduj X\n```", [])
          assert panel.handoff_button.isEnabled() is False

      def test_click_emits_handoff_requested_with_draft(self, qtbot, tmp_path):
          fake = FakeSession()
          panel = _panel(qtbot, tmp_path, fake)
          panel.set_engine_free(True)
          fake.turn_finished.emit("```zadanie\nZbuduj X\n```", [])
          seen = []
          panel.handoff_requested.connect(seen.append)
          panel.handoff_button.click()
          assert seen == ["Zbuduj X"]
  ```

- [ ] **Step 2: implement** `parse_task_draft`, the green button (styled `background-color: TOKENS['ok']`), `set_engine_free`, and `handoff_requested`. Hide the button on the next send/turn start (a stale draft should not linger across turns) — re-shown only when a new reply carries a draft.

- [ ] **Step 3: wire in `app.py`.** Drive engine-free from the runner state (reuse the toolbar enablement: engine is free when a new debate could start):

  ```python
  # In _on_state_changed, after apply_state:
  self.chat_panel.set_engine_free(
      self.toolbar.actions_by_label[toolbar_mod.NEW_DEBATE].isEnabled()
  )
  ```
  And connect the handoff. **Review #7:** `_on_new_debate` runs a two-step preflight — `_ensure_git_repo()` AND `repo_mod.ensure_project_config()` (with its "utworzono .spar/config.toml…" notice). The handoff must NOT skip the config step, or a first debate started from chat runs without the starter `.spar/config.toml`. Extract the common preflight and reuse it in BOTH paths:

  ```python
  def _new_debate_preflight(self) -> bool:
      """Shared new-debate preflight (toolbar button AND chat handoff): ensure a
      git repo, then seed .spar/config.toml (with a notice). Returns False when
      the user declines the repo offer, in which case the caller must not spawn."""
      if not self._ensure_git_repo():
          return False
      if repo_mod.ensure_project_config(self.project_dir):
          self.stream_pane.append_notice(
              "▶ utworzono .spar/config.toml — dostosuj modele i test_command do projektu"
          )
      return True
  ```

  Refactor the existing `_on_new_debate` to call `_new_debate_preflight()` in place of its inline `_ensure_git_repo()` + `ensure_project_config()` block (behavior identical). Then the handoff reuses it:

  ```python
  self.chat_panel.handoff_requested.connect(self._on_chat_handoff)

  def _on_chat_handoff(self, draft: str) -> None:
      if not self._new_debate_preflight():
          return
      dialog = toolbar_mod.NewDebateDialog(self.project_dir, self)
      dialog.task_edit.setPlainText(draft)
      if dialog.exec() != QDialog.DialogCode.Accepted:
          return
      self.runner.start_debate(**dialog.values())
  ```
  (Mirrors `_on_new_debate` exactly, via the shared preflight; the draft prefill is the one difference.)

- [ ] **Step 4: suite + commit.** Both interpreters green; confirm `python3 -m pytest tests/test_gui_orchestrator_pure.py -q` actually RUNS the `parse_task_draft` tests (not skipped). Commit — `feat(gui): task-draft handoff from orchestrator chat to a prefilled new debate`

---

### Task 7: Docs — README, HANDOFF, ADR status, screenshots note (Sonnet)

**Files:**
- Modify: `README.md` (the `spar gui` section)
- Modify: `docs/HANDOFF.md`
- Modify: `docs/adr/0005-conversation-modules-and-tool-window-rails.md` (implementation status note)

- [ ] **Step 1: README.** In the `spar gui (dashboard-pilot)` section, add a paragraph covering: the two icon rails (right: Taski / Czat toggles + a Bramka icon that lights with an attention dot while a gate pends and force-opens the panel; left: a disabled "Pliki" placeholder for a future tranche), collapse persistence and "everything collapsed → stream full width"; the docked orchestrator chat as a **read-only advisor** (bubbles, lettered options, free-text, persists across restarts via `.spar/chat.json`, shows a "run w toku — tylko odczyt" banner during a live run, silently gets gate context so you can ask "co byś wybrał", and NEVER makes gate decisions — the GatePanel stays the only pilot); and the "Nowa debata z tym szkicem" handoff. Add a one-line flow: `pytanie → czat (advisor) → szkic w bloku ```zadanie``` → „Nowa debata z tym szkicem" → prefilled formularz`. Add a screenshots placeholder as a textual TODO comment only — `<!-- TODO: screenshot gui-chat.png po manualnym smoke -->` — **review #32: do NOT add an `![…](docs/img/gui-chat.png)` image reference yet**; the file does not exist, so the reference would render as a broken image in the README. The real `![spar gui — orchestrator chat](docs/img/gui-chat.png)` line replaces the TODO comment only once the screenshot is captured (manual smoke) and committed.

- [ ] **Step 2: HANDOFF.** Append a dated section `## Orchestrator chat + tool-window rails (2026-07-11, <commit range>)` summarizing: ConversationSession extraction (grill green under it), both rails + collapse state machine, read-only advisor chat with `.spar/chat.json` persistence, silent gate-context injection, task-draft handoff; note the read-only boundary (adapter `readonly=True`, opening prompt, no gate actions in chat) and the deferred left-rail Pliki/Git tranches (ADR 0005 consequences). State the final suite count.

- [ ] **Step 3: ADR status.** Add a short "Implementation" note under ADR 0005's Status (e.g. "Implemented 2026-07-11 in `spar/gui/{conversation,orchestrator,rails,chat_store}.py`; left-rail Pliki/Git remain future tranches.").

- [ ] **Step 4: verify + commit.** Re-run both suites green; sanity-check every README path/command. Commit — `docs: orchestrator chat + tool-window rails in spar gui (README, HANDOFF, ADR 0005)`

---

## Self-review notes (spec coverage vs ADR 0005 + brief)

- **ConversationSession refactor (feature 1):** Task 1 — shared worker/facade, generation-token suppression of ALL signals incl. `stream_chunk`, session resume, `SessionLost → session_lost`, `_ABANDONED_THREADS`; grill + orchestrator are thin subclasses; grill tests untouched and green.
- **Tool-window rails (feature 2):** Task 2 — both edges, right rail Taski/Czat/Bramka (attention dot, force-open, decision never discarded because GatePanel stays alive), left disabled Pliki, QSettings-namespaced persistence, all-collapsed → full-width stream, 1.7:1 restored on reappearance.
- **Orchestrator chat panel (feature 3):** Task 3 — docked under Taski (GatePanel sits inside SidePane above it), bubbles, `tool:` dim monospace lines in bot bubbles, vertical lettered-option buttons, always-available free-text, header line, RUNNING read-only banner, read-only opening prompt; Task 5 — silent gate-context injection.
- **Persistent session (feature 4):** Task 4 — `.spar/chat.json`, resume on restart, corrupt/missing → fresh, SessionLost → banner + fresh next send.
- **Handoff (feature 5):** Task 6 — ` ```zadanie``` ` marker contract (defined in the opening prompt, Task 3), green button, engine-free gating via the toolbar signal, prefilled `NewDebateDialog`.
- **Decisions honored:** chat model = claude side's `debate_model or model or default_model` (same resolver as grill); no new config key; no terminal; grill stays modal; engine untouched; no history cap; rails QSettings-namespaced; left rail visual-only.

## Ambiguities resolved

1. **Read-only enforcement mechanism.** ADR asks the chat adapter to run "with read-only tooling" and be testable. Resolved: construct the orchestrator's `ClaudeAdapter` with `readonly=True` (→ `--allowedTools Read` only, no Edit/Write/Bash), plus the opening prompt's explicit read-only instruction. Trade-off noted: `readonly=True` also drops Grep/Glob, so the advisor explores via `Read` only — acceptable and strictly safe for v1; widening the read-only allowlist (adding Grep/Glob) is a later config decision, not opened here.
2. **Where the persisted session id surfaces for `.spar/chat.json`.** The worker owns the live session id; the facade already receives it on each finished turn. Resolved: thread the session id through the worker→facade `finished` signal (now `(gen, reply, session_id, extra)`) and expose it as `ConversationSession.session_id`; the panel writes `chat.json` in its own `turn_finished` handler. Keeps persistence out of the shared session class (grill does not persist).
3. **Ordering vs the chat panel existing.** The brief orders rails before the chat panel, but the "Czat" toggle needs a target. Resolved: Task 2 introduces `OrchestratorChatPanel` as a minimal shell with its FINAL constructor signature (so `app.py` wires it once), and Task 3 fills the body — each task stays independently green.
4. **GatePanel placement ("slots between Taski and chat").** The GatePanel already lives inside `SidePane` (below the task board). Resolved: keep `SidePane` intact as the "Taski" panel (task board + gate) and dock the chat below it in a `RightColumn`; the gate visually sits between the task board and the chat exactly as specified, with zero churn to `sidepane.py`/its tests.
5. **Engine-free signal for the handoff button.** No dedicated signal exists. Resolved: reuse the toolbar's `NEW_DEBATE` enablement (already recomputed on every `state_changed`) as the "engine free" truth, pushed to the panel via `set_engine_free` from `_on_state_changed`.
6. **Task-draft marker contract.** The brief says "define an explicit marker". Resolved: a ` ```zadanie … ``` ` fenced block, declared verbatim in the orchestrator `OPENING_PROMPT` and parsed by the pure `parse_task_draft` (last block wins, mirroring `parse_options`' block discipline).
7. **Single vs dual banner (RUNNING vs session-lost).** Resolved: one `banner` label with a priority (lost > running) to avoid widget churn; a second label is only added if a live smoke shows the combined state is confusing (noted, not pre-committed).

## Review history

- Round 1 (codex gpt-5.6-sol): verdict CONTINUE; accepted #1–#9 (all), applied to body.
  - #1 — construct/wire rails + chat before the initial `side_pane.refresh()`, which becomes the single explicit status sync (no `AttributeError`).
  - #2 — panel `stop_session()` (idempotent) called from `MainWindow.closeEvent`; retention test added.
  - #3 — `_apply_rail_layout` compares tracked logical `self._column_shown`, not pre-show `isVisible()`, so restored splitter sizes survive startup.
  - #4 — new pending gate force-opens Taski (identity edge on name+task_id+rounds) without resolving/hiding the gate.
  - #5 — session-loss recovery re-arms `_opening_sent=False` so the next send is a fresh first turn: opening prompt + optional gate context + user text, `reset=True`.
  - #6 — gate dedup fingerprints the whole gate/context payload and resets on gate clear, so a re-reached gate with new output re-injects.
  - #7 — extracted `_new_debate_preflight()` (git repo + `ensure_project_config` + notice) reused by both `_on_new_debate` and `_on_chat_handoff`.
  - #8 — split Qt-free rail test into `test_gui_rails_pure.py`; visibility assertions use `isHidden()` / `panel.show()` instead of vacuous pre-show `isVisible()`.
  - #9 — `_on_finished` re-checks generation after `turn_finished` before `_handle_extra`; test for stop() from a synchronous subscriber added.
- Round 2 (codex gpt-5.6-sol): verdict CONTINUE; accepted #10–#14, applied to body.
  - #10 — `build_gate_context` now renders the COMPLETE gate/context payload (task_id, rounds, reason, summary, artifact, command, and every `open_remarks`/`nice_backlog` remark's severity/author/text — where the failing per-task-test output actually lives), and `_gate_fingerprint` fingerprints that whole rendered block, so changed test output / remarks re-inject (Task 5).
  - #11 — dropped the self-contradictory `_owns_session`: `stop_session()` unconditionally + idempotently stops whatever session the panel holds; added a real `MainWindow.close()`→`stop_session` wiring test in `test_gui_app.py` and kept the panel idempotency test (Task 3).
  - #12 — moved the Qt-free `build_gate_context` and `parse_task_draft` tests into a new `tests/test_gui_orchestrator_pure.py` with NO `importorskip`, so they genuinely run under `python3` (Tasks 5 & 6).
  - #13 — specified and tested `_on_turn_failed`: clears streaming state, surfaces the error notice, re-enables input+send (an `AdapterError` no longer bricks the chat) (Task 3).
  - #14 — removed the broken `OrchestratorSession.send_opening()` (undefined `first_user_text`, duplicated the panel path); the opening prompt is composed solely by the panel's `_dispatch_user_text` (Task 3).
- Round 3 (codex gpt-5.6-sol): verdict CONTINUE; accepted #15–#19, applied to body.
  - #15 — option clicks now route through the single `_dispatch_user_text(letter)` path (user bubble, in-flight disable, option clearing, gate/opening composition), not a bare `session.send(letter)`; test asserts those effects (Task 3).
  - #16 — `_on_session_lost` now applies the same in-flight cleanup/re-enable as `_on_turn_failed`; recovery test drives send → disabled → loss → enabled → fresh retry (Task 4).
  - #17 — opening/gate-injection flags are committed ONLY on a successful turn via pending fields (`_pending_opening`/`_pending_gate_key`, promoted in `_on_turn_finished`, cleared without promotion on failure/loss); loss also resets `_injected_gate_key`; tests for the AdapterError-retry-re-includes-opening and failed-turn-re-injects-gate cases (Tasks 3–5).
  - #18 — `_on_turn_finished` commits the completed bot bubble from `_streaming_segments` (which hold the tool lines) instead of bare `reply_text`, so `tool:` lines survive turn completion; post-`turn_finished` assertion added (Task 3).
  - #19 — Task 1's stale `750 + 4 = 754` arithmetic replaced with "baseline + new tests, no failures" (no hard-coded intermediate total).
- Round 4 (codex gpt-5.6-sol): verdict CONTINUE; accepted #20–#23, applied to body.
  - #20 — `IconRail` is now a real icon rail: `RailButtonSpec` gained an `icon` glyph, buttons render the glyph (☰/💬/⚠/🗀) on a fixed square face with a larger font, full names moved to the tooltip; tests assert the glyph face + square (Task 2).
  - #21 — the Bramka attention dot is actually painted: `_RailButton.paintEvent` draws a yellow overlay circle when the `attention` property is set, `set_attention` flips the flag and calls `update()`; test grabs the pixmap and asserts the dot pixels/paint path, not just the property (Task 2).
  - #22 — gate-clear now invalidates the pending promotion (`on_status` clears BOTH `_injected_gate_key` and `_pending_gate_key`), fixing the in-flight race where a stale fingerprint got promoted after the gate cleared; clear-during-turn regression test added (Task 5).
  - #23 — the stream/reply merge is specified as an algorithm (`_commit_bubble_html`: committed bubble = arrival-ordered streamed segments verbatim; `reply_text` only as prose fallback when no prose streamed; never concatenated); tests for prose-before-tool, prose-after-tool, no-prose-only-tools, and pure-prose added (Task 3).
- Round 5 (codex gpt-5.6-sol): verdict CONTINUE; accepted #24–#25, applied to body.
  - #24 — terminal status lines (`done` / `done (…s)`) are filtered from prose: `_on_chunk` drops them at arrival and `_commit_bubble_html` filters defensively, so they never flip `has_prose`, suppress `reply_text`, or render in the bubble; tools-then-done and prose-then-done regression tests added (Task 3).
  - #25 — the two re-reached-gate tests now emit `turn_finished` before the second send, so the panel is re-enabled and `fake.sends[1]` actually dispatches (Task 5).
- Round 6 (codex gpt-5.6-sol): verdict CONTINUE; accepted #26 — test_set_button_visible shows the rail and asserts isHidden() both ways, so a no-op set_button_visible fails the test.
- Round 7 (codex gpt-5.6-sol): verdict CONTINUE; accepted #27–#29, applied to body.
  - #27 — the readonly/orchestrator adapter boundary is pinned: `TestOrchestratorSessionAdapter` monkeypatches `spar.gui.orchestrator.ClaudeAdapter` and asserts `readonly=True`, `side_name="orchestrator"`, cwd/events_dir/model kwargs at session construction (Task 3).
  - #28 — the chat banner is driven from `_on_state_changed` via `_CHAT_BANNER_STATES = {RUNNING, LOCKED}` (a live sibling process surfaces as LOCKED, and startup gets the state via `_sync_toolbar`), replacing the RUNNING-only lambda; state-mapping test asserts True/False for RUNNING/IDLE/LOCKED/DONE (Task 3).
  - #29 — `status_changed → chat_panel.on_status` is explicitly connected BEFORE the single initial `side_pane.refresh()` (Task 2 init-order block extended), so a gate already pending at startup reaches the chat synchronously; MainWindow startup-pending-gate test + panel-level gate-before-first-send injection test added (Task 5).
- Round 8 (codex gpt-5.6-sol): verdict CONTINUE; accepted #30–#32, applied to body.
  - #30 — a successful turn with `TurnResult.session_id = None` is treated as non-resumable: `_on_turn_finished` clears the pending flags WITHOUT promoting (opening re-armed, gate context undelivered) and skips `chat.json` persistence, so the next send carries the opening prompt with `reset=True` instead of a bare resume against a fresh worker session; regression test added (Tasks 3–4).
  - #31 — `OPENING_PROMPT` now shows the task-draft marker in the exact multiline format `parse_task_draft` accepts (opening fence line, content lines, separate closing fence line), replacing the inline one-liner the parser rejects; a pure prompt/parser contract test parses the example embedded in `OPENING_PROMPT` itself (Tasks 3 & 6).
  - #32 — the README screenshot is a textual TODO comment (`<!-- TODO: screenshot gui-chat.png po manualnym smoke -->`) instead of an `![…](docs/img/gui-chat.png)` reference to a nonexistent file, avoiding a broken image until the capture lands (Task 7).
- Round 9 (codex gpt-5.6-sol): verdict CONTINUE; accepted #33–#34, applied to body.
  - #33 — tests that emit `turn_finished` and then assert plain-resume/dedup behavior now set a truthy `fake.session_id = "sess-1"` before the emit, so they take the resumable branch of review #30 (promotion of `_opening_sent`/`_pending_gate_key`) instead of the non-resumable one: `test_second_send_is_plain_resume`, `test_option_click_routes_through_single_dispatch_path`, `test_context_injected_once_per_gate`, plus (for the same consistency) `test_rereached_gate_with_new_output_reinjects`, `test_rereached_gate_with_only_changed_remark_text_reinjects`, and `test_gate_cleared_mid_turn_does_not_corrupt_dedup` (Tasks 3 & 5).
  - #34 — the null-session-id branch of `_on_turn_finished` now also DELETES any existing `.spar/chat.json` (`unlink(missing_ok=True)`), not just skips the save — otherwise a resumed session whose turn returns `session_id=None` leaves stale metadata that the next launch reloads, resuming a dead id with the opening treated as delivered; regression test `test_null_session_id_after_resume_deletes_stale_chat_json` starts from persisted metadata, ends with a null id, and asserts chat.json is gone and a next-launch panel re-arms the opening (Tasks 3–4).
- Round 10 (codex gpt-5.6-sol): verdict CONTINUE; accepted #35 — chat.json deletion goes through a best-effort `discard_chat` helper swallowing OSError in both recovery paths (null-session-id and session_lost), with tests `test_discard_chat_swallows_oserror` and monkeypatched-unlink-raises regressions asserting input re-enabled and flags re-armed despite deletion failure.
- Round 11 (codex gpt-5.6-sol): verdict CONTINUE; accepted #36 — discard_chat added to Task 4 Step 2's actual chat_store.py code block, and a second unlink-raises regression (test_session_lost_recovery_survives_deletion_failure) covers the session_lost recovery path.
- Round 12 (codex gpt-5.6-sol): verdict CONTINUE; accepted #37 — the null-session-id branch now explicitly sets _opening_sent=False and _injected_gate_key=None (matching session-loss recovery), with test_null_session_id_resets_opening_and_gate_fingerprint covering the stale gate fingerprint.
