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
