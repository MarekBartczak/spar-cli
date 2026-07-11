"""Tests for the orchestrator chat panel (ADR 0005) — read-only advisor."""
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
