from spar.exec.state import ExecState, TaskState, ExecStateStore
from spar.exec.tasklist import Task
from spar.state import StateRemark, ResolvedRemark
from spar.verdict import Severity


def _t(id, deps=()):
    return Task(id, "d", "claude", "opus", "gpt-5.4", tuple(deps), ("a.py",))


def test_round_trip(tmp_path):
    st = ExecState(
        target_branch="master", target_base_oid="abc", tasks={"t1": TaskState(_t("t1"))}
    )
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    got = store.load()
    assert got.target_branch == "master"
    assert got.tasks["t1"].task.id == "t1"
    assert got.tasks["t1"].status == "pending"


def test_round_trip_with_populated_remarks(tmp_path):
    """Test that a TaskState with populated pending and resolved remarks survives serialization."""
    pending_remark = StateRemark(
        remark_id=2, severity=Severity.MUST, author="codex", text="fix X"
    )
    resolved_remark = StateRemark(
        remark_id=1, severity=Severity.NICE, author="codex", text="nit"
    )
    resolved = ResolvedRemark(
        remark=resolved_remark, resolution="accepted", justification=None
    )

    task_state = TaskState(
        task=_t("t1"),
        status="review",
        branch="spar/t1-claude",
        pending_remarks=[pending_remark],
        resolved_remarks=[resolved],
        next_remark_id=3,
        impl_session_id="s-impl",
        review_session_id="s-rev",
    )

    st = ExecState(target_branch="master", target_base_oid="abc", tasks={"t1": task_state})
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    got = store.load()

    # Assert ExecState fields
    assert got.target_branch == "master"
    assert got.target_base_oid == "abc"

    # Assert TaskState fields
    loaded_task_state = got.tasks["t1"]
    assert loaded_task_state.task.id == "t1"
    assert loaded_task_state.status == "review"
    assert loaded_task_state.branch == "spar/t1-claude"
    assert loaded_task_state.next_remark_id == 3
    assert loaded_task_state.impl_session_id == "s-impl"
    assert loaded_task_state.review_session_id == "s-rev"

    # Assert pending_remarks
    assert len(loaded_task_state.pending_remarks) == 1
    pending = loaded_task_state.pending_remarks[0]
    assert pending.remark_id == 2
    assert pending.severity == Severity.MUST
    assert pending.author == "codex"
    assert pending.text == "fix X"

    # Assert resolved_remarks
    assert len(loaded_task_state.resolved_remarks) == 1
    resolved_loaded = loaded_task_state.resolved_remarks[0]
    assert resolved_loaded.remark.remark_id == 1
    assert resolved_loaded.remark.severity == Severity.NICE
    assert resolved_loaded.remark.author == "codex"
    assert resolved_loaded.remark.text == "nit"
    assert resolved_loaded.resolution == "accepted"
    assert resolved_loaded.justification is None or resolved_loaded.justification == ""


def test_ready_gating_on_deps():
    st = ExecState(
        tasks={
            "t1": TaskState(_t("t1")),
            "t2": TaskState(_t("t2", deps=["t1"])),
        }
    )
    st.mark_ready()  # t1 -> ready, t2 stays pending
    assert st.tasks["t1"].status == "ready"
    assert st.tasks["t2"].status == "pending"
    st.tasks["t1"].status = "merged"
    st.mark_ready()  # now t2 -> ready
    assert st.tasks["t2"].status == "ready"


def test_next_task_first_ready_in_id_order():
    st = ExecState(tasks={"t2": TaskState(_t("t2")), "t1": TaskState(_t("t1"))})
    st.mark_ready()
    assert st.next_task().task.id == "t1"


def test_all_merged():
    st = ExecState(tasks={"t1": TaskState(_t("t1"))})
    assert not st.all_merged()
    st.tasks["t1"].status = "merged"
    assert st.all_merged()


def test_fix_tasks_opened_roundtrip(tmp_path):
    st = ExecState(
        target_branch="master",
        target_base_oid="abc",
        tasks={"t1": TaskState(_t("t1"))},
        fix_tasks_opened=2,
    )
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    assert store.load().fix_tasks_opened == 2


def test_fix_tasks_opened_missing_key_defaults_to_zero(tmp_path):
    # A pre-upgrade exec.json without the key must still load (default 0).
    import json

    st = ExecState(target_branch="master", target_base_oid="abc",
                   tasks={"t1": TaskState(_t("t1"))})
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    data = json.loads(store.exec_path.read_text())
    data.pop("fix_tasks_opened", None)
    store.exec_path.write_text(json.dumps(data))
    assert store.load().fix_tasks_opened == 0


def test_pending_gate_roundtrip(tmp_path):
    st = ExecState(
        target_branch="master",
        target_base_oid="abc",
        tasks={"t1": TaskState(_t("t1"))},
        pending_gate={"name": "review-gate", "options": ["accept", "abort"], "context": {}},
    )
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    got = store.load()
    assert got.pending_gate == {
        "name": "review-gate",
        "options": ["accept", "abort"],
        "context": {},
    }


def test_pending_gate_missing_key_defaults_to_none(tmp_path):
    # A pre-upgrade exec.json without the key must still load (default None).
    import json

    st = ExecState(target_branch="master", target_base_oid="abc",
                   tasks={"t1": TaskState(_t("t1"))})
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    data = json.loads(store.exec_path.read_text())
    data.pop("pending_gate", None)
    store.exec_path.write_text(json.dumps(data))
    assert store.load().pending_gate is None


def test_pending_gate_reason_roundtrips(tmp_path):
    # The gate-reason flag (why the gate pended) rides in the pending_gate
    # context and must survive save/load so a headless resume can honor
    # accept/extend on a per-task-test escalation.
    st = ExecState(
        target_branch="master",
        target_base_oid="abc",
        tasks={"t1": TaskState(_t("t1"))},
        pending_gate={
            "name": "review_rounds",
            "options": ["accept", "extend", "fix", "abort"],
            "context": {"task_id": "t1", "reason": "test_escalation"},
        },
    )
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    assert store.load().pending_gate["context"]["reason"] == "test_escalation"


def test_pending_gate_context_without_reason_still_loads(tmp_path):
    # A gate persisted by an older spar (no ``reason`` in context) must still
    # load; the executor reads it with ``.get("reason")`` → review-dispute path.
    st = ExecState(
        target_branch="master",
        target_base_oid="abc",
        tasks={"t1": TaskState(_t("t1"))},
        pending_gate={
            "name": "review_rounds",
            "options": ["accept", "extend", "abort"],
            "context": {"task_id": "t1"},
        },
    )
    store = ExecStateStore(tmp_path / ".spar")
    store.save(st)
    ctx = store.load().pending_gate["context"]
    assert ctx.get("reason") is None
