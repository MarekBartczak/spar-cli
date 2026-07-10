"""Tests for the orchestrator debate loop.

These use in-Python fake adapters and a scripted fake gate — no subprocesses.
Each fake adapter is handed a list of *steps*; every ``run_turn`` pops the next
step (which may write/edit the artifact, raise, and always returns a reply),
and records the prompt + session_id it was called with.
"""

from pathlib import Path

import pytest

from spar.adapters.base import SessionLost, TurnResult
from spar.config import DebateConfig, SideConfig
from spar.gates import GateChoice
from spar.headless import HeadlessGate
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

    def __init__(self, reply, sid="sess", write=None, artifact=None, raises=None, emit=None):
        self.reply = reply
        self.sid = sid
        self.write = write
        self.artifact = artifact
        self.raises = raises
        self.emit = emit

    def __call__(self, prompt, session_id, on_event=None):
        if self.raises is not None:
            raise self.raises
        if self.write is not None:
            self.artifact.write_text(self.write, encoding="utf-8")
        if self.emit is not None and on_event is not None:
            on_event(self.emit)
        return TurnResult(
            session_id=self.sid, reply_text=self.reply, events_path=Path("ev"), exit_code=0
        )


class FakeAdapter:
    def __init__(self, name, steps):
        self.name = name
        self.steps = list(steps)
        self.calls = []  # list of {"prompt", "session_id", "timeout"}

    def run_turn(self, prompt, session_id, timeout_sec, on_event=None):
        self.calls.append(
            {"prompt": prompt, "session_id": session_id, "timeout": timeout_sec}
        )
        if not self.steps:
            raise AssertionError(f"{self.name}: no scripted step left for this call")
        return self.steps.pop(0)(prompt, session_id, on_event=on_event)


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


def build_orch(
    tmp_path,
    sides_steps,
    order,
    gate,
    guard=None,
    max_rounds=6,
    side_configs=None,
    require_tasks=False,
    sink=None,
):
    adapters = {name: FakeAdapter(name, steps) for name, steps in sides_steps.items()}
    store = StateStore(tmp_path / ".spar")
    artifact = tmp_path / ".spar" / "artifact.md"
    debate = DebateConfig(max_rounds=max_rounds, turn_timeout_sec=10)
    logs = []
    orch = Orchestrator(
        adapters,
        order,
        store,
        artifact,
        debate,
        gate,
        guard=guard,
        log=logs.append,
        side_configs=side_configs,
        require_tasks=require_tasks,
        sink=sink,
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


def test_build_turn_prompt_require_tasks_injects_tasks_contract():
    prompt = build_turn_prompt(
        side_name="claude",
        artifact_path=Path("art.md"),
        artifact_hash="sha256:z",
        open_remarks=[],
        task_prompt="t",
        require_tasks=True,
        catalogs={"claude": ("opus", "sonnet"), "codex": ("gpt-5.5",)},
    )
    # The machine-parsable section is demanded, with the §4.1 grammar skeleton.
    assert "## Tasks" in prompt
    assert "side=" in prompt
    assert "model=" in prompt
    assert "review=" in prompt
    assert "deps=" in prompt
    assert "files=" in prompt
    # Each side's available models are listed so agents assign valid ones.
    assert "opus" in prompt
    assert "sonnet" in prompt
    assert "gpt-5.5" in prompt
    # It must appear before the protocol block.
    assert prompt.index("## Tasks") < prompt.index("<verdict>")


def test_tasks_contract_includes_planning_invariants():
    # The --tasks contract must teach the planner the two isolation
    # invariants (cross-reference rule, per-task test satisfiability) and
    # surface the optional per-task test= field in the grammar.
    from spar.orchestrator import _format_tasks_contract

    text = _format_tasks_contract({"claude": ("m1",), "codex": ("m2",)})
    # grammar line shows the optional test= field
    assert "[ | test=<cmd>]" in text
    # cross-reference rule: referencing another task's files => deps on it
    assert "references files owned by another task" in text
    # scaffold/build-config guidance: such a task comes last
    assert "comes LAST" in text
    # per-task test satisfiability: runnable on the task's own branch
    assert "only its deps" in text
    # the escape hatch is closed: omitted test= means the GLOBAL command
    # gates the merge and must itself be satisfiable on the branch
    assert "GLOBAL test command gates the task" in text
    assert "you MUST give a narrower test=" in text


def test_tasks_contract_shows_impl_model_restriction():
    from spar.orchestrator import _format_tasks_contract

    text = _format_tasks_contract(
        {"claude": ("opus", "sonnet", "haiku"), "codex": ("gpt-5.5",)},
        impl_catalogs={"claude": ("opus", "sonnet"), "codex": ()},
    )
    # restricted side: implementation subset called out
    assert "claude: opus, sonnet, haiku (implementation: ONLY opus, sonnet)" in text
    # unrestricted side: plain catalog line, no restriction note
    assert "- codex: gpt-5.5" in text
    assert "codex: gpt-5.5 (implementation" not in text
    # the rule is stated
    assert "review= may use any model of the reviewing side" in text


def test_tasks_contract_shows_review_model_restriction():
    from spar.orchestrator import _format_tasks_contract

    text = _format_tasks_contract(
        {"claude": ("opus", "sonnet", "haiku"), "codex": ("gpt-5.5", "gpt-5.4")},
        review_catalogs={"claude": ("opus", "sonnet"), "codex": ()},
    )
    # restricted side: review subset called out
    assert "claude: opus, sonnet, haiku (review: ONLY opus, sonnet)" in text
    # unrestricted side: plain catalog line, no restriction note
    assert "- codex: gpt-5.5, gpt-5.4" in text
    assert "codex: gpt-5.5, gpt-5.4 (review" not in text
    # the rule is stated
    assert (
        "where a side's catalog notes a review restriction, review= (when "
        "that side reviews) MUST be one of those models." in text
    )


def test_build_turn_prompt_default_omits_tasks_contract():
    prompt = build_turn_prompt(
        side_name="claude",
        artifact_path=Path("art.md"),
        artifact_hash="sha256:z",
        open_remarks=[],
        task_prompt="t",
    )
    assert "## Tasks" not in prompt


# ---------------------------------------------------------------------------
# Task-list bridge (--tasks): consensus gating on the ## Tasks section
# ---------------------------------------------------------------------------


_BRIDGE_SIDES = {
    "A": SideConfig(adapter="claude", command="claude", models=("opus", "sonnet")),
    "B": SideConfig(adapter="codex", command="codex", models=("gpt-5.5", "gpt-5.4")),
}

NO_TASKS_ARTIFACT = "# Plan\nno tasks here\n"

VALID_TASKS_ARTIFACT = (
    "# Plan\nstuff\n"
    "## Tasks\n"
    "- [t1] do a thing | side=A | model=opus | review=gpt-5.5 | deps=- | files=x.py\n"
)


def test_require_tasks_blocks_consensus_until_tasks_valid(tmp_path):
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, logs = build_orch(
        tmp_path, steps, order, gate, side_configs=_BRIDGE_SIDES, require_tasks=True
    )

    adapters["A"].steps = [
        Step(vblock("DONE"), write=NO_TASKS_ARTIFACT, artifact=artifact),  # creation -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE (invalid-tasks consensus)
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE (valid-tasks consensus)
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE (invalid-tasks consensus)
        # After the MUST remark is injected, B fixes the ## Tasks section.
        Step(
            vblock("AGREE", resolved=["#1 accepted"]),
            write=VALID_TASKS_ARTIFACT,
            artifact=artifact,
        ),
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE -> real consensus
    ]

    code = orch.run_new("plan a thing")
    assert code == 0
    # The user gate is reached exactly once, only after tasks parse.
    assert len(gate.consensus_calls) == 1

    st = store.load()
    assert st.sides["A"].last_verdict_status == "DONE"
    assert st.sides["B"].last_verdict_status == "DONE"
    # A MUST remark authored by "spar" was injected and later resolved.
    assert len(st.resolved_remarks) == 1
    injected = st.resolved_remarks[0].remark
    assert injected.severity == Severity.MUST
    assert injected.author == "spar"
    assert "## Tasks" in injected.text
    assert st.pending_remarks == []
    # The final artifact carries a parsable ## Tasks section.
    assert "## Tasks" in artifact.read_text()


def test_require_tasks_false_finalizes_without_tasks_section(tmp_path):
    """Regression guard: with the bridge off, a plan lacking ## Tasks still
    reaches consensus immediately (generic debate behavior unchanged)."""
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(
        tmp_path, steps, order, gate, side_configs=_BRIDGE_SIDES, require_tasks=False
    )

    adapters["A"].steps = [
        Step(vblock("DONE"), write=NO_TASKS_ARTIFACT, artifact=artifact),  # creation -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE -> consensus immediately
    ]

    code = orch.run_new("plan a thing")
    assert code == 0
    assert len(gate.consensus_calls) == 1


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


def test_consensus_true_when_both_done():
    st = _state_two_sides("DONE", "h1", "DONE", "h1")
    assert is_consensus(st, ["A", "B"]) is True


def test_consensus_false_when_only_one_done():
    st = _state_two_sides("DONE", "h1", "AGREE", "h1")
    assert is_consensus(st, ["A", "B"]) is False


def test_consensus_false_when_both_only_agree():
    st = _state_two_sides("AGREE", "h1", "AGREE", "h1")
    assert is_consensus(st, ["A", "B"]) is False


def test_consensus_false_with_pending_must_even_if_both_done():
    pending = [StateRemark(1, Severity.MUST, "A", "blocking")]
    st = _state_two_sides("DONE", "h1", "DONE", "h1", pending=pending)
    assert is_consensus(st, ["A", "B"]) is False


def test_consensus_true_with_only_pending_nice():
    pending = [StateRemark(1, Severity.NICE, "A", "optional")]
    st = _state_two_sides("DONE", "h1", "DONE", "h1", pending=pending)
    assert is_consensus(st, ["A", "B"]) is True


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


def test_sink_wiring_emits_prefixed_line_to_stdout_and_live_log(tmp_path):
    import io

    from spar.stream import StreamSink

    out = io.StringIO()
    sink = StreamSink(tmp_path / ".spar", quiet=False, stdout=out)
    gate = FakeGate()
    orch, adapters, artifact, store, _ = build_orch(
        tmp_path, {"claude": [], "codex": []}, ["claude", "codex"], gate, sink=sink
    )
    adapters["claude"].steps = [
        Step(vblock("CONTINUE"), write="v0", artifact=artifact, emit="model says hi")
    ]

    state = DebateState(sides={"claude": SideState(), "codex": SideState()})
    orch._take_turn(state, "claude", "creation", "the task", is_round_end=False)
    sink.close()

    assert "[claude r0] model says hi" in out.getvalue()
    live_log = (tmp_path / ".spar" / "live.log").read_text(encoding="utf-8")
    assert "[claude r0] model says hi" in live_log


def test_sink_wiring_quiet_suppresses_event_on_stdout(tmp_path):
    import io

    from spar.stream import StreamSink

    out = io.StringIO()
    sink = StreamSink(tmp_path / ".spar", quiet=True, stdout=out)
    gate = FakeGate()
    orch, adapters, artifact, store, _ = build_orch(
        tmp_path, {"claude": [], "codex": []}, ["claude", "codex"], gate, sink=sink
    )
    adapters["claude"].steps = [
        Step(vblock("CONTINUE"), write="v0", artifact=artifact, emit="model says hi")
    ]

    state = DebateState(sides={"claude": SideState(), "codex": SideState()})
    orch._take_turn(state, "claude", "creation", "the task", is_round_end=False)
    sink.close()

    assert "model says hi" not in out.getvalue()
    live_log = (tmp_path / ".spar" / "live.log").read_text(encoding="utf-8")
    assert "[claude r0] model says hi" in live_log


def test_sink_routes_orchestrator_log_through_sink(tmp_path):
    import io

    from spar.stream import StreamSink

    out = io.StringIO()
    sink = StreamSink(tmp_path / ".spar", quiet=True, stdout=out)
    gate = FakeGate()
    orch, adapters, artifact, store, _ = build_orch(
        tmp_path, {"claude": [], "codex": []}, ["claude", "codex"], gate, sink=sink
    )
    adapters["claude"].steps = [
        Step(vblock("CONTINUE"), write="v0", artifact=artifact)
    ]

    state = DebateState(sides={"claude": SideState(), "codex": SideState()})
    orch._take_turn(state, "claude", "creation", "the task", is_round_end=False)
    sink.close()

    # log() always reaches stdout even under quiet.
    assert "turn complete" in out.getvalue()
    live_log = (tmp_path / ".spar" / "live.log").read_text(encoding="utf-8")
    assert "turn complete" in live_log


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
        Step(vblock("DONE"), artifact=artifact),  # done, no edit
    ]
    adapters["B"].steps = [
        Step(vblock("AGREE", resolved=["#1 accepted"]), write="draft v1 handled", artifact=artifact),
        Step(vblock("DONE"), artifact=artifact),  # done, no edit -> consensus
    ]

    code = orch.run_new("Design something")
    assert code == 0
    assert len(gate.consensus_calls) == 1

    st = store.load()
    assert st.sides["A"].last_verdict_status == "DONE"
    assert st.sides["B"].last_verdict_status == "DONE"
    assert st.pending_remarks == []
    assert len(st.resolved_remarks) == 1
    assert st.resolved_remarks[0].resolution == "accepted"


def test_done_that_edits_is_downgraded_to_agree(tmp_path):
    """A DONE turn that changes the artifact is recorded as AGREE (an edit
    means the side is not finished), so it does not by itself end the debate."""
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("DONE"), write="v0", artifact=artifact),  # creation edits -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), write="v1", artifact=artifact),  # edits -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE -> consensus
    ]

    code = orch.run_new("t")
    assert code == 0
    # A's and B's editing DONE turns were downgraded, so consensus needed the
    # later clean DONE turns from both sides.
    assert len(adapters["A"].calls) == 2
    assert len(gate.consensus_calls) == 1
    st = store.load()
    assert st.sides["A"].last_verdict_status == "DONE"
    assert st.sides["B"].last_verdict_status == "DONE"


# ---------------------------------------------------------------------------
# User remarks at the consensus gate
# ---------------------------------------------------------------------------


def test_user_remarks_reset_statuses_and_continue(tmp_path):
    gate = FakeGate(consensus=[GateDecision("remarks", remarks=("add section Z",)), GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("AGREE"), write="v0", artifact=artifact),  # creation edits -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE (first consensus)
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE (second consensus)
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE (first consensus)
        # after the user remark #1 is injected, B resolves it while editing in Z
        Step(vblock("AGREE", resolved=["#1 accepted"]), write="v1 with Z", artifact=artifact),
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE (second consensus)
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
        Step(vblock("DONE"), artifact=artifact),  # later terminal no-edit DONE
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE -> consensus
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
        Step(vblock("DONE"), artifact=artifact),  # terminal no-edit DONE
    ]
    adapters["B"].steps = [
        Step(vblock("AGREE"), artifact=artifact),  # AGREE but leaves #1 unresolved -> retry
        Step(vblock("AGREE", resolved=["#1 rejected: not actually needed here"]), artifact=artifact),
        Step(vblock("DONE"), artifact=artifact),  # terminal no-edit DONE -> consensus
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
        Step(vblock("DONE"), raises=SessionLost("gone")),  # resume fails
        Step(vblock("DONE"), sid="A2", artifact=artifact),  # fresh retry, no edit -> DONE
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), sid="B1", artifact=artifact),  # no edit -> DONE
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


def test_guard_with_pre_turn_is_called_before_each_adapter_turn(tmp_path):
    class RecordingGuard:
        def __init__(self):
            self.pre_turn_calls = 0
            self.call_calls = 0

        def pre_turn(self):
            self.pre_turn_calls += 1

        def __call__(self, ctx: GuardContext):
            self.call_calls += 1

    guard = RecordingGuard()
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate, guard=guard)

    adapters["A"].steps = [Step(vblock("CONTINUE"), write="v0", artifact=artifact)]
    state = DebateState(sides={"A": SideState(), "B": SideState()})
    orch._take_turn(state, "A", "creation", "t", is_round_end=False)

    assert guard.pre_turn_calls == 1
    assert guard.call_calls == 1


def test_plain_callable_guard_without_pre_turn_still_works(tmp_path):
    # A guard that is just a plain callable (no pre_turn attribute) must not
    # break -- the getattr-based pre_turn wiring should simply skip it.
    def guard(ctx: GuardContext):
        pass

    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate, guard=guard)

    adapters["A"].steps = [Step(vblock("CONTINUE"), write="v0", artifact=artifact)]
    state = DebateState(sides={"A": SideState(), "B": SideState()})
    orch._take_turn(state, "A", "creation", "t", is_round_end=False)

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

    adapters["B"].steps = [Step(vblock("DONE"), artifact=artifact)]  # rerun B, no edit -> DONE
    adapters["A"].steps = [Step(vblock("DONE"), artifact=artifact)]  # then A confirms DONE

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
    adapters["A"].steps = [Step(vblock("DONE"), artifact=artifact)]  # no edit -> DONE
    adapters["B"].steps = [Step(vblock("DONE"), artifact=artifact)]  # no edit -> DONE

    code = orch.run_continue()
    assert code == 0
    assert len(gate.recovery_calls) == 1
    assert gate.recovery_calls[0][1] == old_hash


def test_continue_agree_is_not_terminal_requires_done_handshake(tmp_path):
    # Both sides last said AGREE (accepting the artifact but not terminal), and
    # the artifact was then edited out-of-band. Under the DONE-handshake
    # protocol AGREE never triggers consensus: only a mutual clean DONE does.
    # So resuming must NOT fire consensus off the stale AGREE state -- both
    # sides have to actually take another turn and emit a terminal DONE first.
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, logs = build_orch(tmp_path, steps, order, gate)

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("v1", encoding="utf-8")
    h1 = hash_artifact(artifact)

    state = DebateState(
        round=1,
        last_actor="B",
        artifact_hash=h1,
        turn_in_progress=None,
        sides={
            "A": SideState(last_verdict_status="AGREE", last_verdict_artifact_hash=h1),
            "B": SideState(last_verdict_status="AGREE", last_verdict_artifact_hash=h1),
        },
    )
    _seed_state(store, artifact, state)

    # Out-of-band edit after both AGREEd: disk now differs from h1, though
    # state.artifact_hash and both sides' recorded hashes still say h1.
    artifact.write_text("v1-edited-outside-the-debate", encoding="utf-8")

    adapters["A"].steps = [Step(vblock("DONE"), artifact=artifact)]  # no edit -> DONE
    adapters["B"].steps = [Step(vblock("DONE"), artifact=artifact)]  # no edit -> DONE

    code = orch.run_continue()
    assert code == 0
    # Consensus did NOT fire immediately off the stale AGREE state: both sides
    # had to actually take a turn and upgrade their handshake to a clean DONE.
    assert len(adapters["A"].calls) == 1
    assert len(adapters["B"].calls) == 1
    assert len(gate.consensus_calls) == 1


def test_continue_consensus_fires_immediately_when_both_recorded_done(tmp_path):
    # Both sides already recorded a terminal DONE and the artifact is present
    # on disk, so on resume consensus must fire immediately without requiring
    # another turn from either side.
    gate = FakeGate(consensus=[GateDecision("accept")])
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, logs = build_orch(tmp_path, steps, order, gate)

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("v1", encoding="utf-8")
    h1 = hash_artifact(artifact)

    state = DebateState(
        round=1,
        last_actor="B",
        artifact_hash=h1,
        turn_in_progress=None,
        sides={
            "A": SideState(last_verdict_status="DONE", last_verdict_artifact_hash=h1),
            "B": SideState(last_verdict_status="DONE", last_verdict_artifact_hash=h1),
        },
    )
    _seed_state(store, artifact, state)

    code = orch.run_continue()
    assert code == 0
    assert adapters["A"].calls == []
    assert adapters["B"].calls == []
    assert len(gate.consensus_calls) == 1


def test_debate_loop_aborts_4_when_artifact_missing_at_consensus_check(tmp_path):
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, logs = build_orch(tmp_path, steps, order, gate)

    h1 = "sha256:" + "0" * 64
    state = DebateState(
        round=1,
        last_actor="B",
        artifact_hash=h1,
        turn_in_progress=None,
        sides={
            "A": SideState(last_verdict_status="AGREE", last_verdict_artifact_hash=h1),
            "B": SideState(last_verdict_status="AGREE", last_verdict_artifact_hash=h1),
        },
    )
    _seed_state(store, artifact, state)
    # artifact.md is never written -> missing on disk.

    code = orch.run_continue()
    assert code == 4
    assert any("missing" in m and str(artifact) in m for m in logs)


def test_continue_missing_state_returns_3(tmp_path):
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)
    code = orch.run_continue()
    assert code == 3


def test_continue_side_mismatch_returns_3_no_traceback(tmp_path):
    # Persisted debate was between "claude"/"codex"; this orchestrator is
    # configured with a completely different pair of side names.
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, logs = build_orch(tmp_path, steps, order, gate)

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("v0", encoding="utf-8")
    h0 = hash_artifact(artifact)

    state = DebateState(
        round=0,
        last_actor="claude",
        artifact_hash=h0,
        sides={
            "claude": SideState(last_verdict_status="AGREE", last_verdict_artifact_hash=h0),
            "codex": SideState(last_verdict_status="AGREE", last_verdict_artifact_hash=h0),
        },
    )
    _seed_state(store, artifact, state)

    code = orch.run_continue()
    assert code == 3
    assert any("claude" in m and "codex" in m for m in logs)
    assert any("A" in m and "B" in m for m in logs)
    # no adapter was ever invoked
    assert adapters["A"].calls == []
    assert adapters["B"].calls == []


def test_continue_last_actor_outside_order_returns_3(tmp_path):
    # sides match self.order, but last_actor references a name that isn't
    # one of them (corrupted/foreign state file).
    gate = FakeGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, logs = build_orch(tmp_path, steps, order, gate)

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("v0", encoding="utf-8")
    h0 = hash_artifact(artifact)

    state = DebateState(
        round=0,
        last_actor="ghost",
        artifact_hash=h0,
        sides={
            "A": SideState(last_verdict_status="AGREE", last_verdict_artifact_hash=h0),
            "B": SideState(last_verdict_status="AGREE", last_verdict_artifact_hash=h0),
        },
    )
    _seed_state(store, artifact, state)

    code = orch.run_continue()
    assert code == 3
    assert any("ghost" in m for m in logs)


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


# ---------------------------------------------------------------------------
# HeadlessGate: headless debate gates (consensus/rounds pend, recovery repeats)
# ---------------------------------------------------------------------------


def test_headless_consensus_pends_exit_10(tmp_path):
    gate = HeadlessGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("DONE"), write="v0", artifact=artifact),  # creation edits -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), write="v1", artifact=artifact),  # edits -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE -> consensus
    ]

    code = orch.run_new("t")
    assert code == 10

    st = store.load()
    assert st.pending_gate is not None
    assert st.pending_gate["name"] == "consensus"
    assert st.pending_gate["options"] == ["accept", "remarks", "abort"]
    assert st.pending_gate["context"]["artifact"] == str(artifact)


def test_headless_resume_with_remarks_reinjects_and_repends(tmp_path):
    gate = HeadlessGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(tmp_path, steps, order, gate)

    adapters["A"].steps = [
        Step(vblock("DONE"), write="v0", artifact=artifact),  # creation edits -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), write="v1", artifact=artifact),  # edits -> AGREE
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE -> consensus
    ]
    code = orch.run_new("t")
    assert code == 10

    # Resume with a "remarks" decision: the remark is injected as a USER
    # severity, both sides' handshake resets, and the scripted sides re-AGREE
    # to a fresh DONE/DONE consensus -- which pends again (rc 10).
    adapters["A"].steps = [
        Step(vblock("AGREE", resolved=["#1 accepted"]), artifact=artifact),  # resolves it
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE
    ]
    adapters["B"].steps = [
        Step(vblock("DONE"), artifact=artifact),  # no edit -> DONE -> consensus again
    ]

    code2 = orch.run_continue(
        gate_choice=GateChoice(action="remarks", remarks=("tighten the API",))
    )
    assert code2 == 10

    st = store.load()
    assert st.pending_gate is not None
    assert st.pending_gate["name"] == "consensus"
    # the remark was injected as a USER-severity remark and later resolved
    injected = st.resolved_remarks[-1]
    assert injected.remark.severity == Severity.USER
    assert injected.remark.author == "user"
    assert injected.remark.text == "tighten the API"
    assert injected.resolution == "accepted"


def test_headless_rounds_exhausted_pends_then_extend_resumes(tmp_path):
    gate = HeadlessGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, _ = build_orch(
        tmp_path, steps, order, gate, max_rounds=1
    )

    adapters["A"].steps = [Step(vblock("CONTINUE"), write="v0", artifact=artifact)]
    adapters["B"].steps = [Step(vblock("CONTINUE"), write="v1", artifact=artifact)]

    code = orch.run_new("t")
    assert code == 10

    st = store.load()
    assert st.pending_gate is not None
    assert st.pending_gate["name"] == "rounds_exhausted"
    assert st.pending_gate["options"] == ["accept", "extend", "abort"]
    assert st.pending_gate["context"]["artifact"] == str(artifact)

    # Resume with "extend:1": one more round runs, then rounds-exhausted pends
    # again (a fresh, un-preloaded gate call).
    adapters["A"].steps = [Step(vblock("CONTINUE"), write="v2", artifact=artifact)]
    adapters["B"].steps = [Step(vblock("CONTINUE"), write="v3", artifact=artifact)]

    code2 = orch.run_continue(gate_choice=GateChoice(action="extend", extra_rounds=1))
    assert code2 == 10
    # exactly one more turn each -- the extended round actually ran
    assert len(adapters["A"].calls) == 2
    assert len(adapters["B"].calls) == 2

    st2 = store.load()
    assert st2.round == 2
    assert st2.pending_gate["name"] == "rounds_exhausted"


def test_headless_gate_mismatch_returns_2_state_untouched(tmp_path):
    gate = HeadlessGate()
    order = ["A", "B"]
    steps = {"A": [], "B": []}
    orch, adapters, artifact, store, logs = build_orch(
        tmp_path, steps, order, gate, max_rounds=1
    )

    adapters["A"].steps = [Step(vblock("CONTINUE"), write="v0", artifact=artifact)]
    adapters["B"].steps = [Step(vblock("CONTINUE"), write="v1", artifact=artifact)]

    code = orch.run_new("t")
    assert code == 10
    before = store.load()
    calls_before_a = len(adapters["A"].calls)
    calls_before_b = len(adapters["B"].calls)

    # "rounds_exhausted" only accepts accept/extend/abort -- "remarks" is a
    # mismatch. Validated PURELY, before any side effect: rc 2, state intact.
    code2 = orch.run_continue(
        gate_choice=GateChoice(action="remarks", remarks=("nope",))
    )
    assert code2 == 2
    after = store.load()
    assert after.pending_gate == before.pending_gate
    assert after.round == before.round
    assert len(adapters["A"].calls) == calls_before_a
    assert len(adapters["B"].calls) == calls_before_b
    assert any("--gate rejected" in m for m in logs)


def test_headless_recovery_never_pends_repeats_interrupted_turn(tmp_path):
    gate = HeadlessGate()
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

    # "repeat" is the unconditional headless recovery choice: B's interrupted
    # turn is re-run (no gate pend from recovery itself).
    adapters["B"].steps = [Step(vblock("DONE"), artifact=artifact)]  # no edit -> DONE
    adapters["A"].steps = [Step(vblock("DONE"), artifact=artifact)]  # no edit -> DONE -> consensus

    code = orch.run_continue()
    # The eventual rc 10 comes from the CONSENSUS gate reached afterwards, not
    # from recovery -- recovery never raises GatePending / never pends.
    assert code == 10
    assert len(adapters["B"].calls) == 1  # the interrupted turn was repeated
    st = store.load()
    assert st.pending_gate["name"] == "consensus"
