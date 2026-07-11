"""Docked orchestrator chat panel (ADR 0005) — read-only advisor.

Qt-free pieces (OPENING_PROMPT and the bubble-commit helpers) live above the
``if _HAS_QT:`` guard so the module imports on a plain interpreter. The Qt
layer holds ``OrchestratorSession`` (a thin ConversationSession subclass whose
adapter is ALWAYS constructed with ``readonly=True`` / ``side_name=
"orchestrator"`` — the ADR 0005 safety boundary) and the panel itself.
"""
from __future__ import annotations

import re

from spar.gui.theme import TOKENS

# Read-only advisor contract sent (prepended) on the FIRST turn of a session.
# The task-draft fence example is shown in the exact MULTILINE form
# parse_task_draft accepts (review #31): opening ```zadanie line, content
# lines, closing ``` on its own line.
OPENING_PROMPT = """Jesteś orkiestratorem-DORADCĄ dla tego projektu spar. Pracujesz w trybie
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

aby GUI mogło go przejąć."""


_TERMINAL_RE = re.compile(r"^done \(.*\)$")


def _is_terminal(text: str) -> bool:
    """ClaudeAdapter's terminal status events — ``done`` / ``done (12.3s)``."""
    s = text.strip()
    return s == "done" or _TERMINAL_RE.match(s) is not None


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


def _render_segment(kind: str, text: str) -> str:
    """One committed/in-flight bot-bubble segment: prose vs dim tool line."""
    if kind == "tool":
        return (
            f'<div style="color:{TOKENS["muted"]}; font-family: monospace;">'
            f"{_escape(text)}</div>"
        )
    return (
        f'<div style="color:{TOKENS["claude"]};">{_escape(text)}</div>'
    )


def _commit_bubble_html(segments: list, reply_text: str) -> str:
    """Merge the streamed segments with the final ``reply_text`` (review #23).

    Invariants: (a) streamed prose and reply_text are NEVER both rendered —
    streamed prose wins, reply_text is a pure fallback; (b) tool lines always
    survive, in arrival order (review #18); (c) terminal status lines
    (``done`` / ``done (…s)``) never render and never count as prose
    (review #24, defensive — _on_chunk already drops them at arrival).
    """
    segments = [(k, t) for k, t in segments if not (k == "text" and _is_terminal(t))]
    has_prose = any(kind == "text" and text.strip() for kind, text in segments)
    if has_prose:
        # The committed bubble IS the streamed segments, VERBATIM and in
        # arrival order. reply_text is IGNORED — never concatenated onto the
        # streamed prose (that would duplicate the model's text).
        parts = [_render_segment(kind, text) for kind, text in segments if text.strip()]
    else:
        # FALLBACK: no prose streamed. Use reply_text for the prose but KEEP
        # every streamed tool line, preserving arrival order.
        parts = [_render_segment("tool", text) for kind, text in segments if kind == "tool"]
        if reply_text.strip():
            parts.append(_render_segment("text", reply_text))
    return "".join(parts)


try:  # pragma: no cover - exercised via the two interpreters
    from PySide6.QtWidgets import (
        QHBoxLayout,
        QLabel,
        QPushButton,
        QSizePolicy,
        QTextBrowser,
        QVBoxLayout,
        QWidget,
    )

    _HAS_QT = True
except ImportError:  # pragma: no cover
    _HAS_QT = False


if _HAS_QT:
    from spar.adapters.claude import ClaudeAdapter
    from spar.gui.conversation import ConversationSession
    from spar.gui.grill_dialog import _InputEdit, _truncate

    class OrchestratorSession(ConversationSession):
        """Advisor conversation. The adapter is read-only BY CONSTRUCTION."""

        def _default_adapter_factory(self) -> object:
            cfg = self._side_cfg
            model = cfg.debate_model or cfg.model or cfg.default_model
            return ClaudeAdapter(
                command=cfg.command,
                model=model,
                cwd=self._project_dir,
                events_dir=self._project_dir / ".spar" / "transcript",
                side_name="orchestrator",
                readonly=True,
            )

    class OrchestratorChatPanel(QWidget):
        """Read-only advisor chat, docked at the bottom of the right column."""

        def __init__(self, project_dir, side_cfg, timeout_sec, parent=None, session=None):
            super().__init__(parent)
            self.setObjectName("orchestratorPanel")
            self._project_dir = project_dir
            self._side_cfg = side_cfg
            self._timeout_sec = timeout_sec
            self._session = session

            self._model = (
                getattr(side_cfg, "debate_model", "")
                or getattr(side_cfg, "model", "")
                or getattr(side_cfg, "default_model", "")
                or "?"
            )
            self._turn_count = 0
            self._is_running = False
            # Committed transcript bubbles (final HTML fragments, in order).
            self._bubbles: list[str] = []
            # In-flight bot bubble: arrival-ordered (kind, text) segments,
            # kind ∈ {"text", "tool"}.
            self._streaming_segments: list[tuple[str, str]] = []
            # Opening-contract / gate-context delivery state (review #17):
            # committed flags change ONLY on a successful resumable turn.
            self._opening_sent = False
            self._injected_gate_key: str | None = None
            self._pending_opening = False
            self._pending_gate_key: str | None = None

            self._build_ui()
            self.set_header(self._model, self._turn_count)

            if self._session is not None:
                self._wire_session()
            elif not self._side_cfg:
                self._set_unavailable("czat niedostępny — brak strony claude")
            # else: owned session is built LAZILY on first dispatch —
            # constructing it here would spin up a worker QThread (and a
            # real adapter) for every MainWindow.

        # -- construction ---------------------------------------------------
        def _build_ui(self) -> None:
            layout = QVBoxLayout(self)

            self.header = QLabel("", self)
            self.header.setObjectName("orchestratorHeader")
            self.header.setStyleSheet(f"color: {TOKENS['muted']};")
            layout.addWidget(self.header)

            self.banner = QLabel("run w toku — tylko odczyt", self)
            self.banner.setObjectName("orchestratorBanner")
            self.banner.setStyleSheet(
                f"color: {TOKENS['warn']}; border: 1px solid {TOKENS['warn']};"
                " padding: 2px 6px;"
            )
            self.banner.setVisible(False)
            layout.addWidget(self.banner)

            self.transcript = QTextBrowser(self)
            self.transcript.setObjectName("transcript")
            self.transcript.setOpenExternalLinks(False)
            layout.addWidget(self.transcript, stretch=1)

            self.options_row = QWidget(self)
            self.options_row.setObjectName("optionsRow")
            self.options_layout = QVBoxLayout(self.options_row)
            self.options_layout.setContentsMargins(0, 0, 0, 0)
            self.options_layout.setSpacing(4)
            layout.addWidget(self.options_row)

            input_row = QHBoxLayout()
            self.input_edit = _InputEdit(self._on_send_clicked, self)
            self.input_edit.setObjectName("orchestratorInput")
            self.input_edit.setPlaceholderText("Zapytaj orkiestratora… (Ctrl+Enter wysyła)")
            self.input_edit.setMaximumHeight(72)
            input_row.addWidget(self.input_edit, stretch=1)

            self.send_button = QPushButton("Wyślij", self)
            self.send_button.setObjectName("orchestratorSend")
            self.send_button.clicked.connect(self._on_send_clicked)
            input_row.addWidget(self.send_button)
            layout.addLayout(input_row)

        def _wire_session(self) -> None:
            self._session.stream_chunk.connect(self._on_chunk)
            self._session.turn_finished.connect(self._on_turn_finished)
            # Review #13: do NOT omit — the only thing lifting the in-flight
            # disable after an AdapterError.
            self._session.turn_failed.connect(self._on_turn_failed)
            self._session.session_lost.connect(self._on_session_lost)

        def _ensure_session(self) -> bool:
            if self._session is not None:
                return True
            if not self._side_cfg:
                return False
            self._session = OrchestratorSession(
                self._project_dir, self._side_cfg, self._timeout_sec
            )
            self._wire_session()
            return True

        def _set_unavailable(self, reason: str) -> None:
            self.input_edit.setEnabled(False)
            self.send_button.setEnabled(False)
            self.header.setText(reason)

        # -- header / banner --------------------------------------------------
        def set_header(self, model, turn_count: int) -> None:
            self._header_text = f"claude · {model or '?'} · tura {turn_count} · sesja trwała"
            self.header.setText(self._header_text)

        def set_running(self, is_running: bool) -> None:
            """Show/hide the LIVE-run read-only banner. Never disables input."""
            self._is_running = bool(is_running)
            self.banner.setVisible(self._is_running)

        # -- transcript rendering ---------------------------------------------
        def _render_transcript(self) -> None:
            parts = list(self._bubbles)
            if self._streaming_segments:
                inner = "".join(
                    _render_segment(kind, text)
                    for kind, text in self._streaming_segments
                    if text.strip()
                )
                if inner:
                    parts.append(self._bot_bubble(inner))
            self.transcript.setHtml("".join(parts))
            scrollbar = self.transcript.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

        @staticmethod
        def _bot_bubble(inner_html: str) -> str:
            return f'<div style="text-align:left; margin: 6px 0;">{inner_html}</div>'

        @staticmethod
        def _user_bubble(text: str) -> str:
            return (
                f'<div style="text-align:right; margin: 6px 0;">'
                f'<span style="color:{TOKENS["ok"]};">{_escape(text)}</span>'
                f"</div>"
            )

        def _append_notice(self, text: str) -> None:
            self._bubbles.append(
                f'<div style="text-align:left; margin: 6px 0;">'
                f'<span style="color:{TOKENS["muted"]};">{_escape(text)}</span>'
                f"</div>"
            )

        # -- options ----------------------------------------------------------
        def _clear_options(self) -> None:
            while self.options_layout.count():
                item = self.options_layout.takeAt(0)
                widget = item.widget()
                if widget is not None:
                    # Detach synchronously so a re-query (findChild, tests)
                    # never sees a button merely pending deleteLater.
                    widget.setParent(None)
                    widget.deleteLater()

        def _render_options(self, options: list) -> None:
            self._clear_options()
            for opt in options:
                btn = QPushButton(f"{opt.letter}.  {_truncate(opt.label)}", self.options_row)
                btn.setObjectName(f"option_{opt.letter}")
                btn.setToolTip(opt.label)
                btn.setStyleSheet("text-align: left; padding: 6px 10px;")
                btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
                # Review #15: through the ONE send path — never session.send.
                btn.clicked.connect(
                    lambda _checked=False, letter=opt.letter: self._dispatch_user_text(letter)
                )
                self.options_layout.addWidget(btn)

        # -- sending (the ONE path) --------------------------------------------
        def _on_send_clicked(self) -> None:
            if not self.send_button.isEnabled():
                return
            text = self.input_edit.toPlainText().strip()
            if not text:
                return
            self.input_edit.clear()
            self._dispatch_user_text(text)

        def _dispatch_user_text(self, user_text: str) -> None:
            """The single send path: options, free text, and later extensions."""
            if not self._ensure_session():
                return
            needs_opening = not self._opening_sent
            parts = []
            if needs_opening:
                parts.append(OPENING_PROMPT)
            # (Task 5 inserts the pending-gate context block here.)
            parts.append(user_text)
            prompt = "\n\n".join(p for p in parts if p)

            # Visible bubble carries ONLY the user's text — never the opening
            # prompt or gate context.
            self._bubbles.append(self._user_bubble(user_text))
            self._streaming_segments = []
            self._render_transcript()
            self._set_in_flight(True)

            # Review #17: record what THIS turn carries; commit only on a
            # successful resumable turn (_on_turn_finished promotes).
            self._pending_opening = needs_opening
            self._pending_gate_key = None

            self._session.send(prompt, reset=needs_opening)

        def _set_in_flight(self, in_flight: bool) -> None:
            self.send_button.setEnabled(not in_flight)
            self.input_edit.setEnabled(not in_flight)
            if in_flight:
                self.header.setText(f"{self._header_text} · …myśli")
                self._clear_options()
            else:
                self.header.setText(self._header_text)

        # -- session signal handlers --------------------------------------------
        def _on_chunk(self, text: str) -> None:
            # Review #24: the adapter's terminal status line is NOT prose —
            # drop it at arrival (never appended nor concatenated).
            if _is_terminal(text):
                return
            if text.lstrip().startswith("tool:"):
                self._streaming_segments.append(("tool", text.strip()))
            elif self._streaming_segments and self._streaming_segments[-1][0] == "text":
                kind, prev = self._streaming_segments[-1]
                self._streaming_segments[-1] = ("text", prev + text)
            else:
                self._streaming_segments.append(("text", text))
            self._render_transcript()

        def _on_turn_finished(self, reply_text: str, options: list) -> None:
            # Review #18/#23: commit from the streamed segments (they hold the
            # tool lines); reply_text is only the no-prose fallback.
            inner = _commit_bubble_html(self._streaming_segments, reply_text)
            if inner:
                self._bubbles.append(self._bot_bubble(inner))
            self._streaming_segments = []
            # Review #30: promote the pending flags ONLY when the turn is
            # resumable (truthy session id). A None-id success is non-resumable
            # — the next dispatch must re-carry the opening contract.
            if getattr(self._session, "session_id", None):
                if self._pending_opening:
                    self._opening_sent = True
                if self._pending_gate_key is not None:
                    self._injected_gate_key = self._pending_gate_key
            self._pending_opening = False
            self._pending_gate_key = None
            self._turn_count += 1
            self.set_header(self._model, self._turn_count)
            self._set_in_flight(False)
            self._render_transcript()
            self._render_options(options)

        def _on_turn_failed(self, message: str) -> None:
            # Review #13: retryable — drop the half-built bubble, surface the
            # error, re-enable input. No turn-count bump, no persistence.
            self._streaming_segments = []
            # Review #17: clear pending WITHOUT promoting — the retry re-sends
            # the opening contract / gate context the failed turn carried.
            self._pending_opening = False
            self._pending_gate_key = None
            self._append_notice(f"⚠ tura nie powiodła się: {message}")
            self._set_in_flight(False)
            self.set_running(self._is_running)
            self._render_transcript()

        def _on_session_lost(self) -> None:
            # Minimal handling here; Task 4 adds persistence/reset semantics.
            self._streaming_segments = []
            self._pending_opening = False
            self._pending_gate_key = None
            self._opening_sent = False
            self._injected_gate_key = None
            self._append_notice("⚠ sesja utracona — następna wiadomość rozpocznie nową")
            self._set_in_flight(False)
            self._render_transcript()

        # -- lifecycle ------------------------------------------------------------
        def stop_session(self) -> None:
            """Stop whatever session the panel holds (owned OR injected).

            Idempotent: ConversationSession.stop() is safe to call twice
            (reviews #2 + #11 — no ownership gating of shutdown).
            """
            if self._session is not None:
                self._session.stop()
