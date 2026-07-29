"""``GrillDialog`` — the chat UI driving a :class:`spar.gui.grill.GrillSession`.

A modal dialog: a read-only transcript (user turns vs. model turns visually
distinct; the in-flight model turn streams live), a dynamic row of lettered
option buttons parsed from the model's last reply, a multiline input with a
send button (Ctrl+Enter), a retry path for failed turns, a restart path for a
lost session, and a "Use in debate" path once ``.spar/requirements.md`` has
been written/updated.

Kept Qt-only (imported only where PySide6 is available, same convention as
``spar/gui/grill.py``).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from spar.gui.grill import GrillSession, Option
from spar.gui.theme import TOKENS

__all__ = ["GrillDialog"]

# How far off the bottom still counts as "following the stream". Chat bubbles
# are taller than log lines, so the stream pane's 2px slack is too tight here.
_FOLLOW_SLACK_PX = 8


class WrappingPushButton(QPushButton):
    """A push button whose label wraps instead of being elided or truncated.

    ``QPushButton`` renders its own text on ONE line and elides whatever does
    not fit, so a grill option had to be truncated to fit ("…") and the full
    wording only survived in the tooltip -- unreadable for options that are
    whole sentences. Here the text lives in a word-wrapping ``QLabel`` laid
    out inside the button, so the full option is always visible and reflows
    with the dialog width, while the widget stays a real button (native
    styling, click signal, object name, tooltip).

    ``text()``/``setText()`` proxy the inner label, so callers and tests keep
    using the ordinary button API.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        self._label = QLabel(text, self)
        self._label.setWordWrap(True)
        # The label must not eat the button's clicks or show a text cursor.
        self._label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self._label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._label)

    def text(self) -> str:  # noqa: D102 (proxies QPushButton.text)
        return self._label.text()

    def setText(self, text: str) -> None:  # noqa: N802 (Qt override)
        self._label.setText(text)


class _InputEdit(QPlainTextEdit):
    """Multiline input; Ctrl+Enter triggers ``send_requested``.

    Ctrl+V of an IMAGE (a screenshot straight off the clipboard) is saved as a
    PNG under ``paste_dir`` and its path is inserted as text: the model CLIs
    read images from disk, so a path is the transport. ``paste_dir`` lives
    under ``.spar/`` -- the one directory the artifact scope guard skips
    wholesale -- so pasting can never look like a foreign repo change.

    With no ``paste_dir`` (tests, or a panel with no project dir) image paste
    falls back to Qt's default handling.
    """

    _PASTE_DIR_NAME = "pasted"

    def __init__(
        self,
        send_requested: Callable[[], None],
        parent=None,
        paste_dir: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._send_requested = send_requested
        self._paste_dir = Path(paste_dir) if paste_dir is not None else None

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._send_requested()
            return
        super().keyPressEvent(event)

    # -- image paste ---------------------------------------------------
    def canInsertFromMimeData(self, source) -> bool:  # noqa: N802 (Qt override)
        if self._paste_dir is not None and source.hasImage():
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source) -> None:  # noqa: N802 (Qt override)
        path = self._save_pasted_image(source)
        if path is None:
            super().insertFromMimeData(source)
            return
        # Surround with spaces so the path never fuses with adjacent words.
        prefix = "" if self.toPlainText()[-1:] in ("", " ", "\n") else " "
        self.insertPlainText(f"{prefix}{path} ")

    def _save_pasted_image(self, source) -> Path | None:
        """Write the mime data's image to ``paste_dir``; return its path.

        ``None`` means "not an image paste, or writing failed" -- the caller
        then falls back to Qt's default paste rather than losing the content.
        """
        if self._paste_dir is None or not source.hasImage():
            return None
        image = QImage(source.imageData())
        if image.isNull():
            return None
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
        target = self._paste_dir / f"paste-{stamp}.png"
        try:
            self._paste_dir.mkdir(parents=True, exist_ok=True)
            if not image.save(str(target), "PNG"):
                return None
        except OSError:
            return None
        return target


class GrillDialog(QDialog):
    """Modal chat window for a grill-with-docs conversation.

    ``session`` may be injected (tests use a scripted fake); by default a
    real :class:`GrillSession` is built from ``project_dir``/``side_cfg``/
    ``timeout_sec``.
    """

    def __init__(
        self,
        project_dir: "str | Path",
        side_cfg,
        timeout_sec: int,
        draft: str,
        parent: QWidget | None = None,
        session: GrillSession | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Grilluj zadanie")
        self.resize(860, 640)

        self._project_dir = Path(project_dir)
        self._side_cfg = side_cfg
        self._timeout_sec = timeout_sec
        self._draft = draft
        self._session = session or GrillSession(self._project_dir, side_cfg, timeout_sec)

        self._turns: list[tuple[str, str]] = []
        self._streaming_text = ""
        self._result_requirements: str | None = None
        self._last_send: Callable[[], None] | None = None
        self._stopped = False
        self._session_lost = False

        self._build_ui()
        self._wire_session()

        # First turn: the draft seeds the opening prompt.
        self._turns.append(("user", draft))
        self._render_transcript()
        self._begin_turn(lambda: self._session.start(self._draft))

    # -- construction ---------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        self.transcript = QTextBrowser(self)
        self.transcript.setObjectName("transcript")
        self.transcript.setOpenExternalLinks(False)
        layout.addWidget(self.transcript, stretch=1)

        self.status_label = QLabel("", self)
        self.status_label.setObjectName("statusLabel")
        layout.addWidget(self.status_label)

        self.banner_label = QLabel("", self)
        self.banner_label.setObjectName("bannerLabel")
        self.banner_label.setVisible(False)
        layout.addWidget(self.banner_label)

        self.options_row = QWidget(self)
        self.options_row.setObjectName("optionsRow")
        # Options stack VERTICALLY (live finding: a horizontal row of long
        # option labels blew the window width apart) — one full-width,
        # left-aligned button per option.
        self.options_layout = QVBoxLayout(self.options_row)
        self.options_layout.setContentsMargins(0, 0, 0, 0)
        self.options_layout.setSpacing(4)
        layout.addWidget(self.options_row)

        input_row = QHBoxLayout()
        self.input_edit = _InputEdit(
            self._on_send_clicked,
            self,
            paste_dir=self._project_dir / ".spar" / _InputEdit._PASTE_DIR_NAME,
        )
        self.input_edit.setObjectName("inputEdit")
        self.input_edit.setPlaceholderText(
            "Twoja odpowiedź… (Ctrl+Enter wysyła, Ctrl+V wkleja screena)"
        )
        input_row.addWidget(self.input_edit, stretch=1)

        buttons_col = QVBoxLayout()
        self.send_button = QPushButton("Wyślij", self)
        self.send_button.setObjectName("sendButton")
        self.send_button.clicked.connect(self._on_send_clicked)
        buttons_col.addWidget(self.send_button)

        self.retry_button = QPushButton("Ponów", self)
        self.retry_button.setObjectName("retryButton")
        self.retry_button.setVisible(False)
        self.retry_button.clicked.connect(self._on_retry_clicked)
        buttons_col.addWidget(self.retry_button)

        self.restart_button = QPushButton("Restart grilla", self)
        self.restart_button.setObjectName("restartButton")
        self.restart_button.setVisible(False)
        self.restart_button.clicked.connect(self._on_restart_clicked)
        buttons_col.addWidget(self.restart_button)

        self.use_button = QPushButton("Użyj w debacie", self)
        self.use_button.setObjectName("useButton")
        self.use_button.setVisible(False)
        self.use_button.clicked.connect(self.accept)
        buttons_col.addWidget(self.use_button)

        self.cancel_button = QPushButton("Anuluj", self)
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self.reject)
        buttons_col.addWidget(self.cancel_button)

        input_row.addLayout(buttons_col)
        layout.addLayout(input_row)

    def _wire_session(self) -> None:
        self._session.stream_chunk.connect(self._on_stream_chunk)
        self._session.turn_finished.connect(self._on_turn_finished)
        self._session.requirements_ready.connect(self._on_requirements_ready)
        self._session.turn_failed.connect(self._on_turn_failed)
        self._session.session_lost.connect(self._on_session_lost)

    # -- public ----------------------------------------------------------
    @property
    def result_requirements(self) -> str | None:
        return self._result_requirements

    # -- transcript rendering ---------------------------------------------
    def _render_transcript(self) -> None:
        parts = []
        for role, text in self._turns:
            parts.append(self._bubble_html(role, text))
        if self._streaming_text:
            parts.append(self._bubble_html("model", self._streaming_text))
        scrollbar = self.transcript.verticalScrollBar()
        # Sticky bottom: follow the stream only while the user is parked at
        # (or within a bubble-ish tolerance of) the end. Scrolled up to read
        # something? Keep the position -- setHtml resets it to 0, so restore.
        prev = scrollbar.value()
        at_bottom = prev >= scrollbar.maximum() - _FOLLOW_SLACK_PX
        self.transcript.setHtml("".join(parts))
        scrollbar.setValue(scrollbar.maximum() if at_bottom else prev)

    def _bubble_html(self, role: str, text: str) -> str:
        escaped = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br/>")
        )
        if role == "user":
            align = "right"
            color = TOKENS["ok"]
        else:
            align = "left"
            color = TOKENS["claude"]
        return (
            f'<div style="text-align:{align}; margin: 6px 0;">'
            f'<span style="color:{color};">{escaped}</span>'
            f"</div>"
        )

    # -- turn lifecycle ----------------------------------------------------
    def _begin_turn(self, send: Callable[[], None]) -> None:
        """Disable send controls, show the spinner, dispatch ``send``."""
        self._last_send = send
        self._streaming_text = ""
        self._set_in_flight(True)
        send()

    def _set_in_flight(self, in_flight: bool) -> None:
        self.status_label.setText("model myśli…" if in_flight else "")
        self.send_button.setEnabled(not in_flight)
        self.input_edit.setEnabled(not in_flight)
        self.retry_button.setVisible(False)
        self._clear_options()

    def _clear_options(self) -> None:
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # Detach synchronously so a re-query (findChild, tests) never
                # sees a button that is merely pending ``deleteLater``.
                widget.setParent(None)
                widget.deleteLater()

    def _render_options(self, options: list[Option]) -> None:
        self._clear_options()
        for opt in options:
            btn = WrappingPushButton(f"{opt.letter}.  {opt.label}", self.options_row)
            btn.setObjectName(f"option_{opt.letter}")
            btn.setToolTip(opt.label)
            btn.setStyleSheet("text-align: left;")
            # Minimum (not Fixed) vertically: a wrapped multi-line option needs
            # to be taller than a one-liner.
            btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
            btn.clicked.connect(lambda _checked=False, letter=opt.letter: self._on_option_clicked(letter))
            self.options_layout.addWidget(btn)

    # -- sending -------------------------------------------------------
    def _on_option_clicked(self, letter: str) -> None:
        self._clear_options()
        self._send_answer(letter)

    def _on_send_clicked(self) -> None:
        if not self.send_button.isEnabled():
            return
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        self.input_edit.clear()
        self._send_answer(text)

    def _send_answer(self, text: str) -> None:
        self._turns.append(("user", text))
        self._render_transcript()
        self._begin_turn(lambda: self._session.answer(text))

    # -- session signal handlers -------------------------------------------
    def _on_stream_chunk(self, text: str) -> None:
        self._streaming_text += text
        self._render_transcript()

    def _on_turn_finished(self, reply_text: str, options: list) -> None:
        self._streaming_text = ""
        self._turns.append(("model", reply_text))
        self._render_transcript()
        self._set_in_flight(False)
        self._render_options(options)

    def _on_requirements_ready(self, content: str) -> None:
        self._result_requirements = content
        self.banner_label.setText("Wymagania gotowe")
        self.banner_label.setVisible(True)
        self.use_button.setVisible(True)

    def _on_turn_failed(self, message: str) -> None:
        self._set_in_flight(False)
        self._turns.append(("model", f"[błąd] {message}"))
        self._render_transcript()
        self.retry_button.setVisible(True)

    def _on_session_lost(self) -> None:
        self._session_lost = True
        self._set_in_flight(False)
        self.banner_label.setText("sesja utracona")
        self.banner_label.setVisible(True)
        self.restart_button.setVisible(True)
        self.send_button.setEnabled(False)
        self.input_edit.setEnabled(False)

    def _on_retry_clicked(self) -> None:
        self.retry_button.setVisible(False)
        if self._last_send is not None:
            self._begin_turn(self._last_send)

    def _on_restart_clicked(self) -> None:
        self._session_lost = False
        self.banner_label.setVisible(False)
        self.restart_button.setVisible(False)
        self.send_button.setEnabled(True)
        self.input_edit.setEnabled(True)
        self._turns = [("user", self._draft)]
        self._render_transcript()
        self._begin_turn(lambda: self._session.start(self._draft))

    # -- lifecycle -----------------------------------------------------
    def _stop_session_once(self) -> None:
        if not self._stopped:
            self._stopped = True
            self._session.stop()

    def done(self, result: int) -> None:  # noqa: N802 (Qt override)
        self._stop_session_once()
        super().done(result)
