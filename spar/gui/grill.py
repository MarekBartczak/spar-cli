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
    from PySide6.QtCore import QThread, Signal  # noqa: F401

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
