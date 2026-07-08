"""End-to-end-ish tests for the sequential execution Executor (spar/exec/loop.py).

These drive the whole Task FSM over a *real* tmp git repo with scripted fake
adapters (mirroring tests/test_orchestrator.py and tests/test_exec_review.py)
and a scripted fake gate. Per-Task / final test commands are plain shell
(``true`` / ``false`` / a sentinel-file script) so the objective exit-code gate
is exercised without spawning agents.
"""

import subprocess
from pathlib import Path

import pytest

from spar.adapters.base import TurnResult
from spar.config import ExecutionConfig, SideConfig
from spar.exec.loop import Executor
from spar.exec.state import ExecState, ExecStateStore, TaskState
from spar.exec.tasklist import Task
from spar.orchestrator import GateDecision


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
    """One scripted adapter turn.

    ``edits`` maps a worktree-relative path -> content written before the reply
    is returned (used by an implementer turn). A reviewer turn uses ``edits={}``.
    """

    def __init__(self, reply, sid="sess", edits=None):
        self.reply = reply
        self.sid = sid
        self.edits = edits or {}

    def __call__(self, root):
        for rel, content in self.edits.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        return TurnResult(
            session_id=self.sid, reply_text=self.reply, events_path=Path("ev"), exit_code=0
        )


class FakeAdapter:
    def __init__(self, name, steps):
        self.name = name
        self.steps = list(steps)
        self.root = None  # set on each make_adapter call
        self.calls = []

    def run_turn(self, prompt, session_id, timeout_sec):
        self.calls.append({"prompt": prompt, "session_id": session_id})
        if not self.steps:
            raise AssertionError(f"{self.name}: no scripted step left for this call")
        return self.steps.pop(0)(self.root)


def make_factory(steps_by_side):
    """Return (make_adapter, adapters) where make_adapter memoizes one adapter
    per side and updates its ``root`` (cwd/worktree) on each call."""
    adapters: dict[str, FakeAdapter] = {}

    def make_adapter(side, worktree):
        a = adapters.get(side)
        if a is None:
            a = FakeAdapter(side, steps_by_side.get(side, []))
            adapters[side] = a
        a.root = Path(worktree)
        return a

    return make_adapter, adapters


class FakeGate:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def final_merge_gate(self, summary):
        self.calls.append(summary)
        return self.decisions.pop(0)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


def branch_exists(repo, name):
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
        capture_output=True,
        text=True,
    )
    return r.returncode == 0


def commit_count(repo, ref):
    return int(git(repo, "rev-list", "--count", ref).stdout.strip())


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "master")
    git(r, "config", "user.email", "t@t")
    git(r, "config", "user.name", "t")
    (r / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def side_cfg():
    return {
        "A": SideConfig(adapter="claude", command="claude", models=("ma",), default_model="ma"),
        "B": SideConfig(adapter="codex", command="codex", models=("mb",), default_model="mb"),
    }


def make_task(tid, side, files, deps=(), test=None, model="ma", review="mb"):
    return Task(
        id=tid,
        description=f"do {tid}",
        side=side,
        model=model,
        review_model=review,
        deps=tuple(deps),
        files=tuple(files),
        test=test,
    )


def build_executor(repo, tmp_path, *, tasks, steps_by_side, gate, execution,
                   auto=False):
    spar_dir = tmp_path / ".spar"
    make_adapter, adapters = make_factory(steps_by_side)
    store = ExecStateStore(spar_dir)
    logs = []
    ex = Executor(
        repo=repo,
        spar_dir=spar_dir,
        make_adapter=make_adapter,
        sides=side_cfg(),
        order=["A", "B"],
        plan_path=tmp_path / "plan.md",
        tasks=tuple(tasks),
        execution=execution,
        gate=gate,
        store=store,
        log=logs.append,
        auto_integration_merge=auto,
    )
    return ex, adapters, store, logs


# ---------------------------------------------------------------------------
# Scenario 1: happy path (2 tasks, t2 deps t1) -> both merged, integration
# merged into master, phase=done, exit 0.
# ---------------------------------------------------------------------------


def test_happy_path_two_tasks(repo, tmp_path):
    tasks = [
        make_task("t1", "A", ["work1.py"], model="ma", review="mb"),
        make_task("t2", "B", ["work2.py"], deps=["t1"], model="mb", review="ma"),
    ]
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"work1.py": "print(1)\n"}),  # impl t1
            Step(vblock("DONE")),  # review t2
        ],
        "B": [
            Step(vblock("DONE")),  # review t1
            Step(vblock("CONTINUE"), edits={"work2.py": "print(2)\n"}),  # impl t2
        ],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0

    state = store.load()
    assert state.phase == "done"
    assert state.all_merged()
    assert state.tasks["t1"].status == "merged"
    assert state.tasks["t2"].status == "merged"
    # task branches deleted
    assert not branch_exists(repo, "spar/t1-A")
    assert not branch_exists(repo, "spar/t2-B")
    # worktrees removed
    assert not (tmp_path / ".spar" / "worktrees" / "A").exists()
    assert not (tmp_path / ".spar" / "worktrees" / "B").exists()
    # integration merged into master (target)
    assert git(repo, "cat-file", "-t", "spar/integration").stdout.strip() == "commit"
    master_files = git(repo, "ls-tree", "-r", "--name-only", "master").stdout.split()
    assert "work1.py" in master_files
    assert "work2.py" in master_files
    assert gate.calls  # gate was consulted


# ---------------------------------------------------------------------------
# Scenario 2: per-Task test fails once, then passes -> loops back to
# implementing, then merges.
# ---------------------------------------------------------------------------


def test_per_task_test_fail_then_pass(repo, tmp_path):
    sentinel = tmp_path / "sent_task"
    # fail first (create sentinel), pass afterwards
    per_task = f"test -f {sentinel} || (touch {sentinel}; exit 1)"
    tasks = [make_task("t1", "A", ["work.py"], test=per_task)]
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"work.py": "v1\n"}),  # impl attempt 1
            Step(vblock("CONTINUE"), edits={"work.py": "v2\n"}),  # impl attempt 2
        ],
        "B": [
            Step(vblock("DONE")),  # review attempt 1
            Step(vblock("DONE")),  # review attempt 2
        ],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0
    state = store.load()
    assert state.tasks["t1"].status == "merged"
    # two implement + two review turns happened (looped once)
    assert len(adapters["A"].calls) == 2
    assert len(adapters["B"].calls) == 2


# ---------------------------------------------------------------------------
# Scenario 3: final test fails once -> a t<next> fix task is generated and run
# -> re-run passes -> merged, exit 0.
# ---------------------------------------------------------------------------


def test_final_test_fail_generates_fix_task(repo, tmp_path):
    sentinel = tmp_path / "sent_final"
    final_cmd = f"test -f {sentinel} || (touch {sentinel}; exit 1)"
    tasks = [make_task("t1", "A", ["work.py"], test="true")]
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"work.py": "v1\n"}),  # impl t1
            Step(vblock("CONTINUE")),  # impl fix task t2 (no edits)
        ],
        "B": [
            Step(vblock("DONE")),  # review t1
            Step(vblock("DONE")),  # review fix task t2
        ],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command=final_cmd),
    )
    rc = ex.run()
    assert rc == 0
    state = store.load()
    assert state.phase == "done"
    # a fix task t2 was generated, run through the FSM, and merged
    assert "t2" in state.tasks
    assert state.tasks["t2"].status == "merged"
    assert state.tasks["t2"].task.deps == ()
    # fix side defaults to first side in order (no failing files identified)
    assert state.tasks["t2"].task.side == "A"


# ---------------------------------------------------------------------------
# Scenario 4: recovery — a task merged before state save. run_continue detects
# it via is_ancestor, marks it merged, and does NOT double-merge.
# ---------------------------------------------------------------------------


def test_recovery_merged_before_save(repo, tmp_path):
    # Build git state as if t1 was implemented, merged into integration, but the
    # state file was never updated past "testing".
    base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", "spar/integration", "master")
    git(repo, "branch", "spar/t1-A", "spar/integration")
    wt = tmp_path / ".spar" / "worktrees" / "A"
    git(repo, "worktree", "add", "-q", str(wt), "spar/t1-A")
    (wt / "work.py").write_text("done\n", encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "t1: work")
    git(repo, "checkout", "-q", "spar/integration")
    git(repo, "merge", "--no-ff", "-m", "merge t1", "spar/t1-A")
    git(repo, "worktree", "remove", "--force", str(wt))
    # branch spar/t1-A lingers (crash before delete + before state save)
    integration_oid = git(repo, "rev-parse", "spar/integration").stdout.strip()

    # Hand-write exec.json with t1 still "testing".
    spar_dir = tmp_path / ".spar"
    store = ExecStateStore(spar_dir)
    task = make_task("t1", "A", ["work.py"], test="true")
    state = ExecState(
        phase="execution",
        target_branch="master",
        target_base_oid=base_oid,
        integration_branch="spar/integration",
        tasks={"t1": TaskState(task=task, status="testing", branch="spar/t1-A")},
    )
    store.save(state)

    # Adapters must NOT be called for t1 (it is already merged).
    steps = {"A": [], "B": []}
    make_adapter, adapters = make_factory(steps)
    gate = FakeGate([GateDecision("accept")])
    logs = []
    ex = Executor(
        repo=repo,
        spar_dir=spar_dir,
        make_adapter=make_adapter,
        sides=side_cfg(),
        order=["A", "B"],
        plan_path=tmp_path / "plan.md",
        tasks=(task,),
        execution=ExecutionConfig(test_command="true"),
        gate=gate,
        store=store,
        log=logs.append,
    )
    rc = ex.run_continue()
    assert rc == 0
    reloaded = store.load()
    assert reloaded.tasks["t1"].status == "merged"
    assert reloaded.phase == "done"
    # branch cleaned up
    assert not branch_exists(repo, "spar/t1-A")
    # integration was NOT re-merged (its tip is unchanged)
    assert git(repo, "rev-parse", "spar/integration").stdout.strip() == integration_oid
    # no adapter turns happened (no adapter was ever even constructed)
    assert adapters == {}
    # master now contains the integration merge
    assert "work.py" in git(repo, "ls-tree", "-r", "--name-only", "master").stdout.split()


# ---------------------------------------------------------------------------
# Scenario 5: dirty target worktree at start -> exit 3.
# ---------------------------------------------------------------------------


def test_dirty_target_exits_3(repo, tmp_path):
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    tasks = [make_task("t1", "A", ["work.py"])]
    gate = FakeGate([])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side={"A": [], "B": []}, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 3
    # nothing was created
    assert not branch_exists(repo, "spar/integration")


# ---------------------------------------------------------------------------
# Scenario 6 (Bug 1): completion ordering — a task's status is written to
# "merged" and persisted BEFORE its branch is deleted, so a crash in the
# merge→cleanup window can never leave "testing" + branch-gone (unrecoverable).
# ---------------------------------------------------------------------------


def test_completion_orders_merged_save_before_branch_delete(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work1.py"], model="ma", review="mb")]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work1.py": "print(1)\n"})],  # impl t1
        "B": [Step(vblock("DONE"))],  # review t1
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )

    # Spy on every state write, recording t1's status and whether its branch
    # still exists in git at the moment of the save.
    observations = []
    orig_save = store.save

    def spy(state):
        ts = state.tasks.get("t1")
        observations.append(
            ((ts.status if ts else None), branch_exists(repo, "spar/t1-A"))
        )
        return orig_save(state)

    store.save = spy
    rc = ex.run()
    assert rc == 0

    # The FIRST save that recorded t1 as "merged" must have happened while the
    # task branch STILL existed (status durable before the branch disappears).
    merged_saves = [exists for status, exists in observations if status == "merged"]
    assert merged_saves, "expected a save recording t1 as merged"
    assert merged_saves[0] is True, (
        "status 'merged' was persisted only after the branch was deleted — "
        "a crash in that window would be unrecoverable"
    )
    # And the end state is fully cleaned up: branch gone, status merged.
    assert not branch_exists(repo, "spar/t1-A")
    assert store.load().tasks["t1"].status == "merged"


# ---------------------------------------------------------------------------
# Scenario 7 (Bug 1): recovery of the merge→cleanup window. A task that really
# merged into integration but whose status was never advanced past "testing"
# (branch still lingering, ancestor of integration) is marked merged on
# --continue without re-invoking any adapter and without double-merging.
# ---------------------------------------------------------------------------


def test_recovery_merged_before_cleanup_save(repo, tmp_path):
    base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", "spar/integration", "master")
    git(repo, "branch", "spar/t1-A", "spar/integration")
    wt = tmp_path / ".spar" / "worktrees" / "A"
    git(repo, "worktree", "add", "-q", str(wt), "spar/t1-A")
    (wt / "work.py").write_text("done\n", encoding="utf-8")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "t1: work")
    git(repo, "checkout", "-q", "spar/integration")
    git(repo, "merge", "--no-ff", "-m", "merge t1", "spar/t1-A")
    git(repo, "worktree", "remove", "--force", str(wt))
    # Crash window: branch spar/t1-A lingers, status never advanced past testing.
    integration_oid = git(repo, "rev-parse", "spar/integration").stdout.strip()

    spar_dir = tmp_path / ".spar"
    store = ExecStateStore(spar_dir)
    task = make_task("t1", "A", ["work.py"], test="true")
    state = ExecState(
        phase="execution",
        target_branch="master",
        target_base_oid=base_oid,
        integration_branch="spar/integration",
        tasks={"t1": TaskState(task=task, status="testing", branch="spar/t1-A")},
    )
    store.save(state)

    # No adapter must EVER be constructed for an already-merged task.
    steps = {"A": [], "B": []}
    make_adapter, adapters = make_factory(steps)
    gate = FakeGate([GateDecision("accept")])
    logs = []
    ex = Executor(
        repo=repo,
        spar_dir=spar_dir,
        make_adapter=make_adapter,
        sides=side_cfg(),
        order=["A", "B"],
        plan_path=tmp_path / "plan.md",
        tasks=(task,),
        execution=ExecutionConfig(test_command="true"),
        gate=gate,
        store=store,
        log=logs.append,
    )
    rc = ex.run_continue()
    assert rc == 0
    reloaded = store.load()
    assert reloaded.tasks["t1"].status == "merged"
    assert reloaded.phase == "done"
    assert not branch_exists(repo, "spar/t1-A")
    # integration tip unchanged — no double-merge happened
    assert git(repo, "rev-parse", "spar/integration").stdout.strip() == integration_oid
    # zero adapters constructed
    assert adapters == {}


# ---------------------------------------------------------------------------
# Scenario 8 (Bug 2): the ready+branch-created window. A crash after the task
# branch/worktree were created but before the first "implementing" save leaves
# status "ready" with the branch (and worktree) present. --continue must NOT
# hard-fail (GitError → exit 4) on the leftover branch; it sweeps the stale
# artifacts and runs the task to completion.
# ---------------------------------------------------------------------------


def test_ready_with_leftover_branch_recovers(repo, tmp_path):
    base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", "spar/integration", "master")
    # Leftover task branch + worktree from a crashed prior attempt.
    git(repo, "branch", "spar/t1-A", "spar/integration")
    wt = tmp_path / ".spar" / "worktrees" / "A"
    git(repo, "worktree", "add", "-q", str(wt), "spar/t1-A")

    spar_dir = tmp_path / ".spar"
    store = ExecStateStore(spar_dir)
    task = make_task("t1", "A", ["work.py"], test="true")
    state = ExecState(
        phase="execution",
        target_branch="master",
        target_base_oid=base_oid,
        integration_branch="spar/integration",
        tasks={"t1": TaskState(task=task, status="ready", branch="spar/t1-A")},
    )
    store.save(state)

    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "impl\n"})],  # impl t1
        "B": [Step(vblock("DONE"))],  # review t1
    }
    make_adapter, adapters = make_factory(steps)
    gate = FakeGate([GateDecision("accept")])
    logs = []
    ex = Executor(
        repo=repo,
        spar_dir=spar_dir,
        make_adapter=make_adapter,
        sides=side_cfg(),
        order=["A", "B"],
        plan_path=tmp_path / "plan.md",
        tasks=(task,),
        execution=ExecutionConfig(test_command="true"),
        gate=gate,
        store=store,
        log=logs.append,
    )
    rc = ex.run_continue()
    assert rc == 0, f"leftover branch must not hard-fail; logs={logs}"
    reloaded = store.load()
    assert reloaded.tasks["t1"].status == "merged"
    assert reloaded.phase == "done"
    assert not branch_exists(repo, "spar/t1-A")
    assert "work.py" in git(repo, "ls-tree", "-r", "--name-only", "master").stdout.split()
