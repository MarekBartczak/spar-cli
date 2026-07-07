"""Tests for the orchestrator debate loop.

These use in-Python fake adapters and a scripted fake gate — no subprocesses.
Each fake adapter is handed a list of *steps*; every ``run_turn`` pops the next
step (which may write/edit the artifact, raise, and always returns a reply),
and records the prompt + session_id it was called with.
"""

from pathlib import Path

import pytest

from spar.adapters.base import SessionLost, TurnResult
from spar.config import DebateConfig
from spar.orchestrator import (
    ConsoleGate,
    GateDecision,
    GuardContext,
    GuardViolation,
    Orchestrator,
    build_turn_prompt,
    is_consensus,
)
from spar.state import (
    DebateState,
    SideState,
    StateRemark,
    StateStore,
    TurnInProgress,
    hash_artifact,
)
from spar.verdict import Severity


# ---------------------------------------------------------------------------
# Fakes & helpers
# ---------------------------------------------------------------------------


def vblock(status, resolved=(), remarks=()):
    lines = ["<verdict>", f"status: {status}"]
    if resolved:
        lines.append("resolved:")
        lines += [f"- {r}" for r in resolved]
    if remarks:
        lines.append("remarks:")
        lines += [f"- {r}" for r in remarks]
    lines.append("</verdict>")
    return "prose here\n" + "\n".join(lines)


class Step:
    """One scripted adapter turn."""

    def __init__(self, reply, sid="sess", write=None, artifact=None, raises=None):
        self.reply = reply
        self.sid = sid
        self.write = write
        self.artifact = artifact
        self.raises = raises

    def __call__(self, prompt, session_id):
        if self.raises is not None:
            raise self.raises
        if self.write is not None:
            self.artifact.write_text(self.write, encoding="utf-8")
        return TurnResult(
            session_id=self.sid, reply_text=self.reply, events_path=Path("ev"), exit_code=0
        )


class FakeAdapter:
    def __init__(self, name, steps):
        self.name = name
        self.steps = list(steps)
        self.calls = []  # list of {"prompt", "session_id", "timeout"}

    def run_turn(self, prompt, session_id, timeout_sec):
        self.calls.append(
            {"prompt": prompt, "session_id": session_id, "timeout": timeout_sec}
        )
        if not self.steps:
            raise AssertionError(f"{self.name}: no scripted step left for this call")
        return self.steps.pop(0)(prompt, session_id)


class FakeGate:
    def __init__(self, consensus=(), rounds=(), recovery=()):
        self.consensus = list(consensus)
        self.rounds = list(rounds)
        self.recovery = list(recovery)
        self.consensus_calls = []
        self.rounds_calls = []
        self.recovery_calls = []

    def consensus_gate(self, artifact_path, nice_backlog):
        self.consensus_calls.append((artifact_path, list(nice_backlog)))
        return self.consensus.pop(0)

    def rounds_exhausted_gate(self, artifact_path, pending):
        self.rounds_calls.append((artifact_path, list(pending)))
        return self.rounds.pop(0)

    def recovery_gate(self, artifact_path, expected_hash):
        self.recovery_calls.append((artifact_path, expected_hash))
        return self.recovery.pop(0)


def build_orch(tmp_path, sides_steps, order, gate, guard=None, max_rounds=6):
    adapters = {name: FakeAdapter(name, steps) for name, steps in sides_steps.items()}
    store = StateStore(tmp_path / ".spar")
    artifact = tmp_path / ".spar" / "artifact.md"
    debate = DebateConfig(max_rounds=max_rounds, turn_timeout_sec=10)
    logs = []
    orch = Orchestrator(
        adapters, order, store, artifact, debate, gate, guard=guard, log=logs.append
    )
    return orch, adapters, artifact, store, logs


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------


def test_build_turn_prompt_has_five_elements_in_order():
    remarks = [StateRemark(1, Severity.MUST, "claude", "no rollback plan")]
    prompt = build_turn_prompt(
        side_name="codex",
        artifact_path=Path("/x/art.md"),
        artifact_hash="sha256:abc",
        open_remarks=remarks,
        task_prompt="Design a deploy pipeline",
    )
    i_role = prompt.index("adversarial design debate")
    i_hash = prompt.index("sha256:abc")
    i_remark = prompt.index("#1 [MUST] (claude): no rollback plan")
    i_task = prompt.index("Design a deploy pipeline")
    i_proto = prompt.index("<verdict>")
    assert i_role < i_hash < i_remark < i_task < i_proto
    assert 'side "codex"' in prompt
    assert "/x/art.md" in prompt


def test_build_turn_prompt_no_open_remarks():
    prompt = build_turn_prompt(
        side_name="a",
        artifact_path=Path("art.md"),
        artifact_hash="sha256:z",
        open_remarks=[],
        task_prompt="t",
    )
    assert "No open remarks." in prompt


def test_build_creation_prompt_variant():
    prompt = build_turn_prompt(
        side_name="a",
        artifact_path=Path("art.md"),
        artifact_hash=None,
        open_remarks=[],
        task_prompt="build a thing",
        kind="creation",
    )
    assert "does not exist yet" in prompt
    assert "create it" in prompt
    assert "Open remarks" not in prompt
    assert "build a thing" in prompt
    assert "<verdict>" in prompt


def test_build_verdict_retry_prompt_variant():
    prompt = build_turn_prompt(
        side_name="a",
        artifact_path=Path("art.md"),
        artifact_hash=None,
        open_remarks=[],
        task_prompt="",
        kind="verdict_retry",
    )
    assert "valid <verdict> block" in prompt
    assert "Do NOT edit the artifact" in prompt
    assert "prose" not in prompt  # quotes nothing


# ---------------------------------------------------------------------------
# is_consensus predicate
# ---------------------------------------------------------------------------


def _state_two_sides(a_status, a_hash, b_status, b_hash, pending=()):
    return DebateState(
        sides={
            "A": SideState(last_verdict_status=a_status, last_verdict_artifact_hash=a_hash),
            "B": SideState(last_verdict_status=b_status, last_verdict_artifact_hash=b_hash),
        },
        pending_remarks=list(pending),
    )


def test_consensus_true_when_both_agree_same_hash():
    st = _state_two_sides("AGREE", "h1", "AGREE", "h1")
    assert is_consensus(st, "h1", ["A", "B"]) is True


def test_consensus_false_on_different_hash():
    st = _state_two_sides("AGREE", "h1", "AGREE", "h2")
    assert is_consensus(st, "h2", ["A", "B"]) is False


def test_consensus_false_with_pending_must_even_if_both_agree():
    pending = [StateRemark(1, Severity.MUST, "A", "blocking")]
    st = _state_two_sides("AGREE", "h1", "AGREE", "h1", pending=pending)
    assert is_consensus(st, "h1", ["A", "B"]) is False


def test_consensus_true_with_only_pending_nice():
    pending = [StateRemark(1, Severity.NICE, "A", "optional")]
    st = _state_two_sides("AGREE", "h1", "AGREE", "h1", pending=pending)
    assert is_consensus(st, "h1", ["A", "B"]) is True


# ---------------------------------------------------------------------------
# Creator turn (unit, via _take_turn)
# ---------------------------------------------------------------------------


def test_creator_turn_writes_artifact_and_saves(tmp_path):
    gate = FakeGate()
    orch, adapters, artifact, store, _ = build_orch(tmp_path, {"A": [], "B": []}, ["A", "B"], gate)
    adapters["A"].steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] add tests"]), write="v0", artifact=artifact)
    ]

    state = DebateState(sides={"A": SideState(), "B": SideState()})
    orch._take_turn(state, "A", "creation", "the task", is_round_end=False)

    assert artifact.read_text() == "v0"
    assert state.sides["A"].last_verdict_status == "CONTINUE"
    assert len(state.pending_remarks) == 1
    assert state.pending_remarks[0].severity == Severity.MUST
    assert state.pending_remarks[0].author == "A"
    # state was persisted
    reloaded = store.load()
    assert reloaded.sides["A"].last_verdict_status == "CONTINUE"


# ---------------------------------------------------------------------------
# Full happy debate
# ---------------------------------------------------------------------------


def test_full_happy_debate_reaches_consensus(tmp_path):
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] handle errors"]), write="draft v0", artifact=artifact),
        Step(vblock("AGREE"), artifact=artifact),  # confirm, no edit
    ]
    adapters["B"].steps = [
        Step(vblock("AGREE", resolved=["#1 accepted"]), write="draft v1 handled", artifact=artifact),
    ]

    code = orch.run_new("Design something")
    assert code == 0
    assert len(gate.consensus_calls) == 1

    st = store.load()
    assert st.sides["A"].last_verdict_status == "AGREE"
    assert st.sides["B"].last_verdict_status == "AGREE"
    assert st.pending_remarks == []
    assert len(st.resolved_remarks) == 1
    assert st.resolved_remarks[0].resolution == "accepted"


def test_agree_different_hash_requires_reconfirm(tmp_path):
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("AGREE"), write="v0", artifact=artifact),  # creator agrees at h0
        Step(vblock("AGREE"), artifact=artifact),  # must re-confirm at h1
    ]
    adapters["B"].steps = [
        Step(vblock("AGREE"), write="v1", artifact=artifact),  # edits -> h1, agrees
    ]

    code = orch.run_new("t")
    assert code == 0
    # B's AGREE at a new hash did NOT trigger consensus; A had to move again.
    assert len(adapters["A"].calls) == 2
    assert len(gate.consensus_calls) == 1


# ---------------------------------------------------------------------------
# User remarks at the consensus gate
# ---------------------------------------------------------------------------


def test_user_remarks_reset_statuses_and_continue(tmp_path):
    gate = FakeGate(consensus=[GateDecision("remarks", remarks=("add section Z",)), GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("AGREE"), write="v0", artifact=artifact),
        Step(vblock("AGREE", resolved=["#1 accepted"]), write="v1 with Z", artifact=artifact),
    ]
    adapters["B"].steps = [
        Step(vblock("AGREE"), artifact=artifact),  # agree at h0, no edit
        Step(vblock("AGREE"), artifact=artifact),  # agree at h1
    ]

    code = orch.run_new("t")
    assert code == 0
    assert len(gate.consensus_calls) == 2
    st = store.load()
    # the user remark became a resolved USER remark
    assert len(st.resolved_remarks) == 1
    assert st.resolved_remarks[0].remark.severity == Severity.USER
    assert st.resolved_remarks[0].remark.author == "user"


# ---------------------------------------------------------------------------
# Verdict errors and retries
# ---------------------------------------------------------------------------


def test_verdict_error_retry_succeeds_and_uses_retry_prompt(tmp_path):
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step("no verdict at all", write="v0", artifact=artifact),  # invalid -> retry
        Step(vblock("CONTINUE"), artifact=artifact),  # valid retry, no edit
        Step(vblock("AGREE"), artifact=artifact),  # later confirm
    ]
    adapters["B"].steps = [
        Step(vblock("AGREE"), artifact=artifact),
    ]

    code = orch.run_new("t")
    assert code == 0
    # second call to A was the verdict-retry prompt
    assert "valid <verdict> block" in adapters["A"].calls[1]["prompt"]
    assert "Do NOT edit the artifact" in adapters["A"].calls[1]["prompt"]


def test_verdict_double_failure_aborts_4(tmp_path):
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step("garbage", write="v0", artifact=artifact),
        Step("still garbage", artifact=artifact),
    ]
    code = orch.run_new("t")
    assert code == 4
    assert store.exists()  # state saved


def test_verdict_retry_that_edits_artifact_aborts_4(tmp_path):
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step("garbage", write="v0", artifact=artifact),  # invalid -> retry
        Step(vblock("CONTINUE"), write="v1-edited", artifact=artifact),  # edits during retry
    ]
    code = orch.run_new("t")
    assert code == 4


def test_agree_with_pending_must_demands_retry(tmp_path):
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] critical gap"]), write="v0", artifact=artifact),
        Step(vblock("AGREE"), artifact=artifact),  # confirm at end
    ]
    adapters["B"].steps = [
        Step(vblock("AGREE"), artifact=artifact),  # AGREE but leaves #1 unresolved -> retry
        Step(vblock("AGREE", resolved=["#1 rejected: not actually needed here"]), artifact=artifact),
    ]

    code = orch.run_new("t")
    assert code == 0
    # B's second call was a verdict retry
    assert "valid <verdict> block" in adapters["B"].calls[1]["prompt"]
    st = store.load()
    assert len(st.resolved_remarks) == 1
    assert st.resolved_remarks[0].resolution == "rejected"


# ---------------------------------------------------------------------------
# Session loss
# ---------------------------------------------------------------------------


def test_session_lost_retries_with_none_session(tmp_path):
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("CONTINUE"), sid="A1", write="v0", artifact=artifact),
        Step(vblock("AGREE"), raises=SessionLost("gone")),  # resume fails
        Step(vblock("AGREE"), sid="A2", artifact=artifact),  # fresh retry
    ]
    adapters["B"].steps = [
        Step(vblock("AGREE"), sid="B1", write="v1", artifact=artifact),
    ]

    code = orch.run_new("t")
    assert code == 0
    assert adapters["A"].calls[1]["session_id"] == "A1"  # tried to resume
    assert adapters["A"].calls[2]["session_id"] is None  # then fresh


# ---------------------------------------------------------------------------
# Rounds exhausted
# ---------------------------------------------------------------------------


def test_rounds_exhausted_extend_then_accept(tmp_path):
    gate = FakeGate(rounds=[GateDecision("extend", extra_rounds=1), GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate, max_rounds=1)

    adapters["A"].steps = [
        Step(vblock("CONTINUE"), write="v0", artifact=artifact),
        Step(vblock("CONTINUE"), write="v2", artifact=artifact),
    ]
    adapters["B"].steps = [
        Step(vblock("CONTINUE"), write="v1", artifact=artifact),
        Step(vblock("CONTINUE"), write="v3", artifact=artifact),
    ]

    code = orch.run_new("t")
    assert code == 0
    assert len(gate.rounds_calls) == 2


def test_rounds_exhausted_abort_returns_5(tmp_path):
    gate = FakeGate(rounds=[GateDecision("abort")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate, max_rounds=1)

    adapters["A"].steps = [Step(vblock("CONTINUE"), write="v0", artifact=artifact)]
    adapters["B"].steps = [Step(vblock("CONTINUE"), write="v1", artifact=artifact)]

    code = orch.run_new("t")
    assert code == 5


# ---------------------------------------------------------------------------
# Guard hook
# ---------------------------------------------------------------------------


def test_guard_violation_retries_whole_turn(tmp_path):
    calls = {"n": 0}

    def guard(ctx: GuardContext):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GuardViolation("touched a forbidden file")

    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate, guard=guard)

    adapters["A"].steps = [
        Step(vblock("CONTINUE"), write="v0", artifact=artifact),
        Step(vblock("CONTINUE"), write="v0-redo", artifact=artifact),
    ]
    state = DebateState(sides={"A": SideState(), "B": SideState()})
    orch._take_turn(state, "A", "creation", "t", is_round_end=False)

    assert len(adapters["A"].calls) == 2  # turn was redone
    assert state.sides["A"].last_verdict_status == "CONTINUE"


def test_guard_second_violation_aborts_4(tmp_path):
    def guard(ctx: GuardContext):
        raise GuardViolation("nope")

    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate, guard=guard)

    adapters["A"].steps = [
        Step(vblock("CONTINUE"), write="v0", artifact=artifact),
        Step(vblock("CONTINUE"), write="v0b", artifact=artifact),
    ]
    code = orch.run_new("t")
    assert code == 4


# ---------------------------------------------------------------------------
# Creation failure
# ---------------------------------------------------------------------------


def test_creator_that_never_writes_file_aborts_4(tmp_path):
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("CONTINUE")),  # no write
        Step(vblock("CONTINUE")),  # retry, still no write
    ]
    code = orch.run_new("t")
    assert code == 4


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


def test_lock_held_returns_3(tmp_path):
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [Step(vblock("CONTINUE"))], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    blocker = StateStore(tmp_path / ".spar")
    blocker.acquire_lock()
    try:
        code = orch.run_new("t")
        assert code == 3
    finally:
        blocker.release_lock()


# ---------------------------------------------------------------------------
# run_continue recovery
# ---------------------------------------------------------------------------


def _seed_state(store, artifact, state, task="the task"):
    store.save(state)
    (store.spar_dir / "task.md").write_text(task, encoding="utf-8")


def test_continue_repeat_turn_reruns_interrupted_side(tmp_path):
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, logs = build_orch(tmp_path, steps, order, gate)

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("v0", encoding="utf-8")
    h0 = hash_artifact(artifact)

    state = DebateState(
        round=0,
        last_actor="A",
        artifact_hash=h0,
        turn_in_progress=TurnInProgress(side="B", artifact_hash_before=h0),
        sides={
            "A": SideState(last_verdict_status="CONTINUE", last_verdict_artifact_hash=h0),
            "B": SideState(),
        },
    )
    _seed_state(store, artifact, state)

    adapters["B"].steps = [Step(vblock("AGREE"), artifact=artifact)]  # rerun B
    adapters["A"].steps = [Step(vblock("AGREE"), artifact=artifact)]  # then A confirms

    code = orch.run_continue()
    assert code == 0
    assert len(adapters["B"].calls) == 1
    assert any("re-running B" in m for m in logs)
    assert store.load().turn_in_progress is None


def test_continue_artifact_changed_consults_recovery_gate(tmp_path):
    gate = FakeGate(recovery=["keep"], consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("v-new", encoding="utf-8")  # already changed on disk
    old_hash = "sha256:" + "0" * 64

    state = DebateState(
        round=0,
        last_actor="A",
        artifact_hash=old_hash,
        turn_in_progress=TurnInProgress(side="B", artifact_hash_before=old_hash),
        sides={
            "A": SideState(last_verdict_status="CONTINUE", last_verdict_artifact_hash=old_hash),
            "B": SideState(),
        },
    )
    _seed_state(store, artifact, state)

    # keep -> adopt current file, advance past B to A
    adapters["A"].steps = [Step(vblock("AGREE"), artifact=artifact)]
    adapters["B"].steps = [Step(vblock("AGREE"), artifact=artifact)]

    code = orch.run_continue()
    assert code == 0
    assert len(gate.recovery_calls) == 1
    assert gate.recovery_calls[0][1] == old_hash


def test_continue_missing_state_returns_3(tmp_path):
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)
    code = orch.run_continue()
    assert code == 3


# ---------------------------------------------------------------------------
# ConsoleGate (default UserGate)
# ---------------------------------------------------------------------------


def test_console_gate_accept():
    outputs = []
    gate = ConsoleGate(input_fn=lambda _: "a", print_fn=outputs.append)
    decision = gate.consensus_gate(Path("art.md"), [])
    assert decision.action == "accept"


def test_console_gate_collect_remarks():
    replies = iter(["r", "first remark", "second remark", ""])
    gate = ConsoleGate(input_fn=lambda _: next(replies), print_fn=lambda *_: None)
    decision = gate.consensus_gate(Path("art.md"), [])
    assert decision.action == "remarks"
    assert decision.remarks == ("first remark", "second remark")


def test_console_gate_recovery():
    gate = ConsoleGate(input_fn=lambda _: "repeat", print_fn=lambda *_: None)
    assert gate.recovery_gate(Path("art.md"), "sha256:x") == "repeat"
