from spar.exec.state import ExecState, TaskState, ExecStateStore
from spar.exec.tasklist import Task


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
