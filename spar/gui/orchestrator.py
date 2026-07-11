"""Docked orchestrator chat panel (ADR 0005) — read-only advisor.

Qt-free pieces (OPENING_PROMPT and the bubble-commit helpers) live above the
``if _HAS_QT:`` guard so the module imports on a plain interpreter. The Qt
layer holds ``OrchestratorSession`` (a thin ConversationSession subclass whose
adapter is ALWAYS constructed with ``readonly=True`` / ``side_name=
"orchestrator"`` — the ADR 0005 safety boundary) and the panel itself.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from spar.gui.chat_store import ChatMeta, discard_chat, load_chat, save_chat
from spar.gui.theme import TOKENS

# Read-only advisor contract sent (prepended) on the FIRST turn of a session.
# Conversational behavior (live smoke defect 2): a NORMAL conversation — no
# self-introduction, no volunteered capability lists, no lettered menus on
# every reply; the A./B./C. format is reserved for genuine choices (the GUI
# renders buttons off those lines). The task-draft fence example is shown in
# the exact MULTILINE form parse_task_draft accepts (review #31): opening
# ```zadanie line, content lines, closing ``` on its own line.
OPENING_PROMPT = """Jesteś orkiestratorem-DORADCĄ dla tego projektu spar. Pracujesz w trybie
TYLKO-DO-ODCZYTU: analizujesz repozytorium i stan w .spar/, odpowiadasz na
pytania i pomagasz planować kolejną pracę. NIE edytujesz plików, NIE
uruchamiasz narzędzi zmieniających repo, i NIGDY nie podejmujesz decyzji
bramek — decyzje bramek podejmuje wyłącznie panel Bramki w GUI.

Prowadź NORMALNĄ rozmowę: odpowiadaj bezpośrednio i zwięźle na to, co pisze
użytkownik. Na powitanie odpowiedz jednym zdaniem powitania — nic więcej.
NIE przedstawiaj się, NIE wypisuj swoich możliwości i
NIE proponuj menu opcji z własnej inicjatywy. Opcje oznaczone LITERAMI
(A., B., C., ...) rezerwuj WYŁĄCZNIE na sytuacje, w których naprawdę
potrzebujesz, aby użytkownik wybrał między konkretnymi alternatywami —
wtedy każdą opcję umieść w osobnej linii zaczynającej się od jej litery
(A., B., C., ...) i wskaż rekomendację.

Gdy przygotujesz szkic zadania do nowej debaty, umieść go w bloku
ogrodzonym DOKŁADNIE w tym wielowierszowym formacie (linia otwierająca
```zadanie, treść zadania w kolejnych liniach, osobna linia zamykająca ```):

```zadanie
<treść szkicu zadania>
```

aby GUI mogło go przejąć."""


def opening_prompt_hash() -> str:
    """Short fingerprint (sha256 prefix) of the CURRENT opening prompt.

    Persisted alongside the chat session id (Task: prompt-hash invalidation).
    A resumed session carries the OPENING_PROMPT it was opened with forever —
    when the prompt changes between versions, the stored hash mismatches and
    load_chat treats the file as no-session, so the panel starts fresh with
    the new prompt instead of resuming the old behavior. Lives here (not in
    chat_store) so chat_store stays generic.
    """
    return hashlib.sha256(OPENING_PROMPT.encode("utf-8")).hexdigest()[:16]


_DRAFT_OPEN_RE = re.compile(r"^```zadanie\s*$")
_DRAFT_CLOSE_RE = re.compile(r"^```\s*$")


def parse_task_draft(reply_text: str) -> "str | None":
    """Extract the LAST ```zadanie … ``` fenced block from a reply.

    Pure and Qt-free (mirrors parse_options' last-block-wins semantics).
    The fence opens on a line matching ``^```zadanie\\s*$`` (tolerant of
    trailing spaces) and closes on ``^```\\s*$``. Returns the trimmed inner
    text, or ``None`` when no complete block is present. An unterminated
    trailing fence does not count — only closed blocks are drafts.
    """
    draft = None
    inner: "list[str] | None" = None
    for line in reply_text.splitlines():
        if inner is None:
            if _DRAFT_OPEN_RE.match(line):
                inner = []
        elif _DRAFT_CLOSE_RE.match(line):
            draft = "\n".join(inner).strip()
            inner = None
        else:
            inner.append(line)
    return draft


# Free-text bodies (summary, each remark text) are truncated to this many
# chars, mirroring the engine's headless gate truncation.
_GATE_TEXT_LIMIT = 2000


def build_gate_context(pending_gate: dict | None) -> str:
    """Render the COMPLETE pending-gate payload as a read-only text block.

    Pure and Qt-free. Mirrors SidePane._render_context's field set (review
    #10): task_id, rounds, reason, summary, artifact, command, then every
    remark from ``open_remarks`` or ``nice_backlog`` — the failing per-task
    test output lives in ``open_remarks[*].text``, never in a top-level
    summary. Only lines whose field is present are emitted.
    """
    if not pending_gate:
        return ""
    context = pending_gate.get("context") or {}
    lines = ["[KONTEKST BRAMKI — tylko do wglądu, NIE podejmuj decyzji]"]
    lines.append(f"typ: {pending_gate.get('name', '?')}")
    meta = []
    if context.get("task_id") is not None:
        meta.append(f"task: {context['task_id']}")
    if context.get("rounds") is not None:
        meta.append(f"rundy: {context['rounds']}")
    if context.get("reason"):
        meta.append(f"powód: {context['reason']}")
    if meta:
        lines.append("  ".join(meta))
    summary = context.get("summary")
    if summary:
        lines.append("podsumowanie:")
        lines.append(str(summary)[:_GATE_TEXT_LIMIT])
    if context.get("artifact"):
        lines.append(f"plan: {context['artifact']}")
    if context.get("command"):
        lines.append(f"komenda: {context['command']}")
    remarks = context.get("open_remarks") or context.get("nice_backlog") or []
    if remarks:
        lines.append("uwagi:")
        for remark in remarks:
            severity = remark.get("severity", "")
            author = remark.get("author", "")
            text = str(remark.get("text", ""))
            # Truncate the WHOLE rendered line to the limit so the remark
            # body never exceeds it even after the [severity]/(author) prefix.
            lines.append(f"[{severity}] ({author}) {text}"[:_GATE_TEXT_LIMIT])
    return "\n".join(lines)


def _gate_fingerprint(pending_gate: dict | None) -> str:
    """Dedup identity of a pending gate = its complete rendered context.

    Reviews #6 + #10: neither ``(name, task_id)`` nor a tuple over
    name/task_id/rounds/summary/command is a valid identity — a re-reached
    gate whose only change is remark text (the failing-test evidence) must
    re-inject. The rendered block folds in every field, so any change yields
    a new fingerprint, while a 2s re-poll of the same gate stays identical.
    """
    return build_gate_context(pending_gate)


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
    from PySide6.QtCore import Signal
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

        # Task 6: emitted with the parsed task draft when the green handoff
        # button is clicked. The panel does NOT construct the dialog itself —
        # app.py owns the dialog/runner wiring.
        handoff_requested = Signal(str)

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
            # Lost-session banner state (Task 4): priority lost > running.
            self._session_lost = False
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
            # Latest pending gate from side_pane.status_changed (Task 5).
            self._pending_gate: dict | None = None
            # Task 6: last parsed task draft + engine-free flag (pushed by
            # MainWindow from the NEW_DEBATE toolbar enablement).
            self._draft: str | None = None
            self._engine_free = False

            # Persistence (Task 4): resume the previous chat session from
            # .spar/chat.json. A resumed session already ran its opening —
            # the dispatch helper therefore skips OPENING_PROMPT on resume.
            self._chat_path = Path(project_dir) / ".spar" / "chat.json"
            # Prompt-hash invalidation: metadata persisted under a DIFFERENT
            # opening prompt (or none — legacy file) is treated as no-session.
            meta = load_chat(self._chat_path, expected_prompt_hash=opening_prompt_hash())
            self._initial_session_id = meta.session_id if meta else None
            if meta is not None:
                self._turn_count = meta.turn_count
                self._model = meta.model or self._model
            self._opening_sent = meta is not None

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

            header_row = QHBoxLayout()
            self.header = QLabel("", self)
            self.header.setObjectName("orchestratorHeader")
            self.header.setStyleSheet(f"color: {TOKENS['muted']};")
            header_row.addWidget(self.header, stretch=1)

            # Escape hatch (live smoke): drop the persisted session + transcript
            # and start over — the next send builds a FRESH session carrying the
            # CURRENT opening prompt.
            self.clear_button = QPushButton("Wyczyść", self)
            self.clear_button.setObjectName("clearChatButton")
            self.clear_button.setStyleSheet(
                f"color: {TOKENS['muted']}; padding: 1px 8px;"
            )
            self.clear_button.clicked.connect(self._on_clear_clicked)
            header_row.addWidget(self.clear_button)
            layout.addLayout(header_row)

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

            # Task 6: green handoff button — shown only when the latest reply
            # carried a ```zadanie draft; enabled only while the engine is
            # free (mirrors the NEW_DEBATE toolbar enablement).
            self.handoff_button = QPushButton("Nowa debata z tym szkicem", self)
            self.handoff_button.setObjectName("handoffButton")
            self.handoff_button.setStyleSheet(
                f"background-color: {TOKENS['ok']}; padding: 6px 10px;"
            )
            self.handoff_button.setVisible(False)
            self.handoff_button.clicked.connect(self._on_handoff_clicked)
            layout.addWidget(self.handoff_button)

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
                self._project_dir,
                self._side_cfg,
                self._timeout_sec,
                initial_session_id=self._initial_session_id,
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
            self._update_banner()

        def _update_banner(self) -> None:
            """One banner label, priority lost > running (Task 4)."""
            if self._session_lost:
                self.banner.setText("sesja utracona — nowa zostanie utworzona")
                self.banner.setVisible(True)
            elif self._is_running:
                self.banner.setText("run w toku — tylko odczyt")
                self.banner.setVisible(True)
            else:
                self.banner.setVisible(False)

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
            # Task 5: silently carry the pending-gate context — once per gate
            # fingerprint (the complete rendered block, reviews #6 + #10).
            gate_key = None
            if self._pending_gate is not None:
                fingerprint = _gate_fingerprint(self._pending_gate)
                if fingerprint != self._injected_gate_key:
                    parts.append(build_gate_context(self._pending_gate))
                    gate_key = fingerprint
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
            self._pending_gate_key = gate_key

            # Task 4: sending is what clears the lost state — the next turn
            # starts the fresh session, so restore the normal banner priority.
            if self._session_lost:
                self._session_lost = False
                self.set_running(self._is_running)

            self._session.send(prompt, reset=needs_opening)

        def _set_in_flight(self, in_flight: bool) -> None:
            self.send_button.setEnabled(not in_flight)
            self.input_edit.setEnabled(not in_flight)
            if in_flight:
                self.header.setText(f"{self._header_text} · …myśli")
                self._clear_options()
                # Task 6: a stale draft must not linger across turns — the
                # button re-appears only when a NEW reply carries a draft.
                self._draft = None
                self.handoff_button.setVisible(False)
            else:
                self.header.setText(self._header_text)

        # -- status (pending gate) ----------------------------------------------
        def on_status(self, status: dict) -> None:
            """Track the pending gate from side_pane.status_changed (Task 5)."""
            pending_gate = status.get("pending_gate")
            self._pending_gate = pending_gate
            if not pending_gate:
                # Gate cleared: reset BOTH keys (review #22). Clearing only
                # _injected_gate_key would let an in-flight turn's stale
                # _pending_gate_key be promoted AFTER the clear, wrongly
                # deduping the same gate when it is re-reached.
                self._injected_gate_key = None
                self._pending_gate_key = None

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
            # — the worker cleared its session id and will start a FRESH claude
            # session on the next run_turn, so the panel must also RESET the
            # already-committed flags (mirroring _on_session_lost): the next
            # dispatch re-carries the opening contract with reset=True, and no
            # stale delivered-gate key survives (reviewer fix, Task 3).
            session_id = getattr(self._session, "session_id", None)
            if session_id:
                if self._pending_opening:
                    self._opening_sent = True
                if self._pending_gate_key is not None:
                    self._injected_gate_key = self._pending_gate_key
            else:
                self._opening_sent = False
                self._injected_gate_key = None
            self._pending_opening = False
            self._pending_gate_key = None
            self._turn_count += 1
            # Task 4 persistence: only a resumable (truthy-id) turn is worth
            # keeping. A null-id turn must NOT write chat.json (never persist
            # a null session id) and must DELETE any stale metadata from a
            # previous launch (review #34) — otherwise the next GUI launch
            # would resume a dead id with the opening treated as delivered.
            if session_id:
                save_chat(
                    self._chat_path,
                    ChatMeta(session_id, self._model, self._turn_count,
                             opening_prompt_hash()),
                )
            else:
                discard_chat(self._chat_path)
            self.set_header(self._model, self._turn_count)
            self._set_in_flight(False)
            self._render_transcript()
            self._render_options(options)
            # Task 6: a reply carrying a ```zadanie draft surfaces the green
            # handoff button — enabled only while the engine is free.
            draft = parse_task_draft(reply_text)
            if draft is not None:
                self._draft = draft
                self.handoff_button.setEnabled(self._engine_free)
                self.handoff_button.setVisible(True)

        # -- task-draft handoff (Task 6) -----------------------------------------
        def set_engine_free(self, free: bool) -> None:
            """Pushed by MainWindow from the NEW_DEBATE toolbar enablement."""
            self._engine_free = bool(free)
            self.handoff_button.setEnabled(self._engine_free)

        def _on_handoff_clicked(self) -> None:
            if self._draft is not None:
                self.handoff_requested.emit(self._draft)

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
            # Task 4: FULL loss semantics. Review #16 — a loss can arrive
            # MID-TURN with input/send disabled and half-built streaming
            # state; apply the same cleanup/re-enable as _on_turn_failed or
            # the chat is permanently bricked.
            self._streaming_segments = []
            # Review #17: clear pendings WITHOUT promoting, and re-arm the
            # committed flags — the shared session was reset, so the opening
            # contract and any delivered gate context died with it.
            self._pending_opening = False
            self._pending_gate_key = None
            self._opening_sent = False
            self._injected_gate_key = None
            # Review #34/#35: drop the stale persisted id best-effort — a
            # deletion failure must not abort this recovery slot.
            discard_chat(self._chat_path)
            self._session_lost = True
            self._update_banner()
            self._append_notice("⚠ sesja utracona — następna wiadomość rozpocznie nową")
            self._set_in_flight(False)
            self._render_transcript()

        # -- clear (Wyczyść) -------------------------------------------------------
        def _on_clear_clicked(self) -> None:
            """Discard the whole conversation and start over on demand.

            Live smoke: a persisted session resumed across GUI restarts keeps
            carrying the OPENING_PROMPT it was opened with — after a prompt
            change the user was stuck with the old behavior. Clearing stops the
            session (idempotent; a mid-turn thread is retained by stop()'s
            abandon path), deletes .spar/chat.json, wipes the transcript and
            option/handoff widgets, resets every delivery flag, and DROPS the
            session object so the next send lazily builds a FRESH one
            (reset=True, carrying the CURRENT opening prompt).
            """
            if self._session is not None:
                self._session.stop()
            self._session = None
            self._initial_session_id = None
            discard_chat(self._chat_path)
            # Transcript + widgets.
            self._bubbles = []
            self._streaming_segments = []
            self.transcript.clear()
            self._clear_options()
            self._draft = None
            self.handoff_button.setVisible(False)
            # Delivery / lifecycle flags — a brand-new panel over an empty
            # .spar/chat.json.
            self._opening_sent = False
            self._injected_gate_key = None
            self._pending_opening = False
            self._pending_gate_key = None
            self._session_lost = False
            self._turn_count = 0
            self.set_header(self._model, self._turn_count)
            self._update_banner()
            # A clear mid-turn must re-enable input (the old turn's late
            # signals are suppressed by stop()'s generation bump).
            self._set_in_flight(False)

        # -- lifecycle ------------------------------------------------------------
        def stop_session(self) -> None:
            """Stop whatever session the panel holds (owned OR injected).

            Idempotent: ConversationSession.stop() is safe to call twice
            (reviews #2 + #11 — no ownership gating of shutdown).
            """
            if self._session is not None:
                self._session.stop()
