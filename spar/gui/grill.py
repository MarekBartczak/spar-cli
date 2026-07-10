"""Grill-with-docs conversation, threaded over the ``ClaudeAdapter``.

Two pure, Qt-free pieces live at the top so they can be imported and tested
without PySide6:

* ``OPENING_PROMPT_TEMPLATE`` — the verbatim opening prompt that launches the
  user's ``grill-with-docs`` skill on a task draft.
* ``Option`` / ``parse_options`` — block-based extraction of lettered choices
  (A./B./C. …) from a model reply, for rendering as answer buttons.

The Qt layer (defined only when PySide6 is importable) is a GUI-thread FACADE,
``GrillSession(QObject)``, whose private ``_GrillWorker`` is moved onto a
persistent ``QThread`` and owns the one ``ClaudeAdapter`` conversation. Each
turn's ``run_turn`` blocks for up to minutes, so it must never run on the GUI
thread; all interaction is via queued signals. Stop-suppression is FACADE-side
via a generation token: the facade stamps every dispatched turn with the
current generation, ``stop()`` (GUI thread) increments it, and EVERY
worker→facade signal — stream chunks, turn results, failures and session-lost
— carries that stamp so the facade can drop anything stale before re-emitting
on its public signals. This works even while the worker is blocked inside
``run_turn`` (where a queued worker-side stop flag could never run).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional


OPENING_PROMPT_TEMPLATE = """Użyj skilla grill-with-docs dla tego projektu. Zadanie do wygrillowania:
"{draft}".
Zadawaj pytania POJEDYNCZO, każde z opcjami oznaczonymi LITERAMI (A., B., C., ...)
i Twoją rekomendacją — ja odpowiadam w kolejnych wiadomościach. Gdy uznasz
wymagania za kompletne, zapisz finalne wymagania do .spar/requirements.md
(pełna treść zadania dla dwustronnej debaty, zakończona wymaganiem sekcji
## Tasks) i napisz GOTOWE."""


@dataclass(frozen=True)
class Option:
    """A single lettered answer choice extracted from a model reply.

    ``label`` holds the FULL text of the choice — any truncation for display
    is a dialog-side concern, never applied here.
    """

    letter: str
    label: str


# Matches one lettered-option line, tolerant of leading list/bold markers and a
# bold closer immediately after the letter+delimiter:
#   "A. foo" / "A) foo" / "- **B. Implicit fallback** — ..." / "* C. baz"
_OPTION_RE = re.compile(r"^[-*\s]*\**([A-H])[.)]\**\s*(.+)$")


def _clean_label(text: str) -> str:
    """Strip ALL ``**`` markers (mid-line closers included) and trim."""
    return text.replace("**", "").strip()


def parse_options(reply_text: str) -> list[Option]:
    """Extract the active lettered-option block from a model reply.

    Block-based: option lines that are consecutive — allowing at most one
    intervening blank/continuation line — form a BLOCK. Two or more non-option
    lines in a row break the block. Among all blocks, the LAST one whose
    letters form a contiguous run from ``A`` (A, B, C, …) wins, so a stale
    ``C`` from an earlier block can never leak into a later ``A``/``B`` block.
    Returns ``[]`` when no such block exists.
    """
    blocks: list[list[Option]] = []
    current: list[Option] = []
    gap = 0  # non-option lines seen since the last option line in `current`

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
        return all(
            opt.letter == chr(ord("A") + i) for i, opt in enumerate(block)
        )

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
    from spar.adapters.claude import ClaudeAdapter
    from spar.config import SideConfig

    _REQ_RELPATH = Path(".spar") / "requirements.md"

    def _content_hash(path: Path) -> Optional[str]:
        """SHA-256 of ``path``'s bytes, or ``None`` when it does not exist."""
        try:
            data = path.read_bytes()
        except (FileNotFoundError, OSError):
            return None
        return hashlib.sha256(data).hexdigest()

    class _GrillWorker(QObject):
        """Owns the single adapter conversation; lives on a worker QThread.

        Every outgoing signal carries the generation stamp it was dispatched
        with so the facade can drop stale results after a ``stop()``.
        """

        # gen, text
        chunk = Signal(int, str)
        # gen, reply_text, requirements_content_or_None
        finished = Signal(int, str, object)
        # gen, message
        failed = Signal(int, str)
        # gen
        lost = Signal(int)

        def __init__(
            self,
            adapter_factory: Callable[[], object],
            project_dir: Path,
            timeout_sec: int,
        ) -> None:
            super().__init__()
            self._adapter = adapter_factory()
            self._project_dir = Path(project_dir)
            self._timeout_sec = timeout_sec
            self._session_id: str | None = None
            self._req_path = self._project_dir / _REQ_RELPATH
            # Baseline snapshot taken at session start (worker construction).
            self._req_hash = _content_hash(self._req_path)

        def run_turn(self, generation: int, prompt: str, reset_session: bool) -> None:
            """Slot: execute one turn (blocks the worker's event loop)."""
            if reset_session:
                self._session_id = None

            def on_event(line: str) -> None:
                self.chunk.emit(generation, line)

            try:
                result = self._adapter.run_turn(
                    prompt,
                    self._session_id,
                    self._timeout_sec,
                    on_event,
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
            req_content = self._detect_requirements()
            self.finished.emit(generation, result.reply_text, req_content)

        def _detect_requirements(self) -> str | None:
            """Return the requirements content iff created/changed since start."""
            new_hash = _content_hash(self._req_path)
            if new_hash is None or new_hash == self._req_hash:
                return None
            self._req_hash = new_hash
            try:
                return self._req_path.read_text(encoding="utf-8")
            except OSError:
                return None

    class GrillSession(QObject):
        """GUI-thread facade driving a grill-with-docs conversation.

        Owns a ``_GrillWorker`` on a persistent ``QThread``. Public methods
        (``start``/``answer``/``stop``) run on the GUI thread and only emit
        queued signals to the worker; the worker reports back via queued
        signals which this facade filters by generation stamp before
        re-emitting publicly.
        """

        stream_chunk = Signal(str)
        turn_finished = Signal(str, list)  # reply_text, options
        requirements_ready = Signal(str)  # content
        turn_failed = Signal(str)  # message (retryable)
        session_lost = Signal()  # resume died; caller must start() fresh

        # Private facade→worker dispatch: gen, prompt, reset_session
        _dispatch = Signal(int, str, bool)

        def __init__(
            self,
            project_dir: Path,
            side_cfg: "SideConfig",
            timeout_sec: int,
            adapter_factory: Callable[[], object] | None = None,
            parent: QObject | None = None,
        ) -> None:
            super().__init__(parent)
            self._project_dir = Path(project_dir)
            self._side_cfg = side_cfg
            self._timeout_sec = timeout_sec
            self._generation = 0

            if adapter_factory is None:
                adapter_factory = self._default_adapter_factory

            self._thread = QThread()
            self._worker = _GrillWorker(
                adapter_factory, self._project_dir, timeout_sec
            )
            self._worker.moveToThread(self._thread)

            # facade → worker (runs on the worker thread)
            self._dispatch.connect(
                self._worker.run_turn, Qt.ConnectionType.QueuedConnection
            )
            # worker → facade (runs on the GUI thread, generation-filtered)
            self._worker.chunk.connect(
                self._on_chunk, Qt.ConnectionType.QueuedConnection
            )
            self._worker.finished.connect(
                self._on_finished, Qt.ConnectionType.QueuedConnection
            )
            self._worker.failed.connect(
                self._on_failed, Qt.ConnectionType.QueuedConnection
            )
            self._worker.lost.connect(
                self._on_lost, Qt.ConnectionType.QueuedConnection
            )
            self._thread.finished.connect(self._worker.deleteLater)
            self._thread.start()

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

        # -- public API (GUI thread) --------------------------------------
        def start(self, draft: str) -> None:
            """Begin a FRESH session with the opening template on ``draft``."""
            prompt = OPENING_PROMPT_TEMPLATE.format(draft=draft)
            self._dispatch.emit(self._generation, prompt, True)

        def answer(self, text: str) -> None:
            """Send the next turn, resuming the stored session id."""
            self._dispatch.emit(self._generation, text, False)

        def stop(self) -> None:
            """Abandon the session: suppress further public signals and quit.

            Incrementing the generation on the GUI thread drops any in-flight
            turn's late signals (stream chunks included). The worker thread
            quits after the current turn returns.
            """
            self._generation += 1
            self._thread.quit()

        # -- worker → facade (GUI thread; generation-filtered) ------------
        def _on_chunk(self, generation: int, text: str) -> None:
            if generation != self._generation:
                return
            self.stream_chunk.emit(text)

        def _on_finished(
            self, generation: int, reply_text: str, req_content: object
        ) -> None:
            if generation != self._generation:
                return
            self.turn_finished.emit(reply_text, parse_options(reply_text))
            if isinstance(req_content, str):
                self.requirements_ready.emit(req_content)

        def _on_failed(self, generation: int, message: str) -> None:
            if generation != self._generation:
                return
            self.turn_failed.emit(message)

        def _on_lost(self, generation: int) -> None:
            if generation != self._generation:
                return
            self.session_lost.emit()
