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

    def test_mid_conversation_null_id_rearms_opening_contract(self, qtbot, tmp_path):
        # Reviewer finding (Task 3): a null-id SUCCESS mid-conversation clears
        # the worker's session id (conversation.py run_turn), so the next
        # run_turn starts a FRESH claude session. The panel must mirror that:
        # RESET the already-committed flags (like _on_session_lost), not just
        # skip promotion — otherwise the next dispatch sends bare user text
        # with reset=False and the fresh session never gets the read-only
        # advisor contract.
        fake = FakeSession()
        panel = _panel(qtbot, tmp_path, fake)
        panel.input_edit.setPlainText("pierwsze")
        panel.send_button.click()
        fake.session_id = "sess-1"  # resumable: opening promoted
        fake.turn_finished.emit("ok", [])
        assert panel._opening_sent is True
        panel._injected_gate_key = "gate-abc"  # simulate a delivered gate context
        panel.input_edit.setPlainText("drugie")
        panel.send_button.click()
        fake.session_id = None  # non-resumable success: worker id cleared
        fake.turn_finished.emit("ok", [])
        assert panel._opening_sent is False           # re-armed
        assert panel._injected_gate_key is None       # delivered-gate key reset
        panel.input_edit.setPlainText("trzecie")
        panel.send_button.click()
        text, reset = fake.sends[-1]
        assert OPENING_PROMPT.split("\n")[0] in text  # contract re-carried
        assert "trzecie" in text
        assert reset is True                          # matches the worker's fresh start

    # -- persistence + session-loss recovery (Task 4) ----------------------

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
        # the dead session took its delivered context with it. (Gate-context
        # injection itself lands in a later task; here the delivered-gate key
        # is seeded directly to prove the branch resets it.)
        from spar.gui.chat_store import ChatMeta, save_chat
        from spar.gui.orchestrator import OPENING_PROMPT
        save_chat(tmp_path / ".spar" / "chat.json", ChatMeta("sess-stale", "opus", 4))
        fake = FakeSession()
        fake.session_id = "sess-stale"
        panel = OrchestratorChatPanel(tmp_path, object(), 60, session=fake)
        qtbot.addWidget(panel)
        panel.input_edit.setPlainText("co robić?")
        panel.send_button.click()                        # resumed: opening skipped
        assert panel._opening_sent is True
        panel._injected_gate_key = "review_rounds:t1"    # as if delivered to dead session
        fake.session_id = None
        fake.turn_finished.emit("ok", [])                # non-resumable turn
        assert panel._opening_sent is False              # review #37
        assert panel._injected_gate_key is None          # review #37
        panel.input_edit.setPlainText("no więc?")
        panel.send_button.click()
        sent_text, reset = fake.sends[-1]
        assert reset is True
        assert OPENING_PROMPT.split("\n")[0] in sent_text  # opening re-delivered

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
