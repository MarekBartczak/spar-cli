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
