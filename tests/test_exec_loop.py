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
from spar.exec import gitops
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

    def __init__(self, reply, sid="sess", edits=None, raises=None):
        self.reply = reply
        self.sid = sid
        self.edits = edits or {}
        self.raises = raises

    def __call__(self, root):
        if self.raises is not None:
            raise self.raises
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
        self.models = []  # model passed on each make_adapter call for this side
        self.readonly_flags = []  # readonly flag on each make_adapter call

    def run_turn(self, prompt, session_id, timeout_sec):
        self.calls.append({"prompt": prompt, "session_id": session_id})
        if not self.steps:
            raise AssertionError(f"{self.name}: no scripted step left for this call")
        return self.steps.pop(0)(self.root)


def make_factory(steps_by_side):
    """Return (make_adapter, adapters) where make_adapter memoizes one adapter
    per side and updates its ``root`` (cwd/worktree) on each call.

    ``model`` (the third factory arg, per Task.model/review_model) is recorded
    on the adapter's ``models`` list every time the factory is invoked, so
    tests can assert the negotiated per-Task model was actually threaded
    through to the adapter construction call for each turn."""
    adapters: dict[str, FakeAdapter] = {}

    def make_adapter(side, worktree, model, readonly=False):
        a = adapters.get(side)
        if a is None:
            a = FakeAdapter(side, steps_by_side.get(side, []))
            adapters[side] = a
        a.root = Path(worktree)
        a.models.append(model)
        a.readonly_flags.append(readonly)
        return a

    return make_adapter, adapters


class FakeGate:
    def __init__(self, decisions, review_decisions=()):
        self.decisions = list(decisions)
        self.calls = []
        self.review_decisions = list(review_decisions)
        self.review_calls = []

    def final_merge_gate(self, summary):
        self.calls.append(summary)
        return self.decisions.pop(0)

    def review_rounds_exhausted_gate(self, task_id, rounds, pending):
        self.review_calls.append((task_id, rounds, list(pending)))
        return self.review_decisions.pop(0)


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

    # Per-Task model assignment is honored end-to-end: the implement turn for
    # t1 (side A) must run on t1.model ("ma"), and the review turn for t1
    # (performed by side B as reviewer) must run on t1.review_model ("mb").
    # Side A's adapter is built once for t1's implement turn (model="ma") and
    # once for t2's review turn (reviewer for t2's side B, model=t2.review_model
    # == "ma"); side B's adapter is built once for t1's review turn
    # (model=t1.review_model == "mb") and once for t2's implement turn
    # (model=t2.model == "mb").
    assert adapters["A"].models == ["ma", "ma"]
    assert adapters["B"].models == ["mb", "mb"]


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
# I1: a per-Task test failure must feed the captured failing output into the
# NEXT implement turn's prompt, so the testing→implementing loop can converge.
# ---------------------------------------------------------------------------


def test_per_task_test_failure_context_reaches_reimplement_prompt(repo, tmp_path):
    sentinel = tmp_path / "sent_ctx"
    marker = "UNIQUE_TEST_FAILURE_MARKER_XYZ"
    # First run: emit the marker on stdout and fail; second run: pass.
    per_task = f"test -f {sentinel} || (touch {sentinel}; echo {marker}; exit 1)"
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

    # Side A performed exactly the two implement turns (reviewer is B).
    impl_calls = adapters["A"].calls
    assert len(impl_calls) == 2
    # The FIRST implement prompt has no failure context (no test ran yet).
    assert marker not in impl_calls[0]["prompt"]
    # The SECOND (re-implement) prompt MUST carry the captured failing output
    # so the implementer knows what to fix.
    assert marker in impl_calls[1]["prompt"], (
        "re-implement turn did not receive the failing test output as context"
    )


# ---------------------------------------------------------------------------
# I2: §5 branch/worktree collision policy. A FRESH `exec` that finds a leftover
# spar/integration, spar/t* branch, or .spar/worktrees/* dir with no matching
# state must REFUSE (exit 3) naming the leftover, and clobber nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["integration_branch", "task_branch", "worktree_dir"])
def test_fresh_refuses_on_leftover_artifacts(repo, tmp_path, kind):
    spar_dir = tmp_path / ".spar"
    if kind == "integration_branch":
        git(repo, "branch", "spar/integration", "master")
        needle = "spar/integration"
    elif kind == "task_branch":
        git(repo, "branch", "spar/t1-claude", "master")
        needle = "spar/t1-claude"
    else:  # worktree_dir
        leftover_wt = spar_dir / "worktrees" / "claude"
        leftover_wt.mkdir(parents=True)
        (leftover_wt / "stale.txt").write_text("stale\n", encoding="utf-8")
        needle = "worktrees"

    tasks = [make_task("t1", "A", ["work.py"])]
    gate = FakeGate([])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side={"A": [], "B": []}, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    head_before = git(repo, "rev-parse", "HEAD").stdout.strip()
    rc = ex.run()
    assert rc == 3
    # The refusal message names the leftover.
    assert any(needle in m for m in logs), f"no log named the leftover; logs={logs}"
    # Nothing was clobbered / created: no exec state, no adapter constructed,
    # HEAD untouched, and any pre-existing leftover still present.
    assert not store.exists()
    assert adapters == {}
    assert git(repo, "rev-parse", "HEAD").stdout.strip() == head_before
    if kind == "integration_branch":
        assert branch_exists(repo, "spar/integration")
    elif kind == "task_branch":
        assert branch_exists(repo, "spar/t1-claude")
    else:
        assert (spar_dir / "worktrees" / "claude" / "stale.txt").exists()


# ---------------------------------------------------------------------------
# I3: aborting after a conflicting final merge must leave the working tree
# clean (git merge --abort), not stuck mid-merge.
# ---------------------------------------------------------------------------


def test_surface_merge_conflict_abort_cleans_up(repo, tmp_path):
    # Put the repo into a conflicted, mid-merge state.
    git(repo, "checkout", "-qb", "feature")
    (repo / "seed.txt").write_text("feature side\n", encoding="utf-8")
    git(repo, "commit", "-aqm", "feature change")
    git(repo, "checkout", "-q", "master")
    (repo / "seed.txt").write_text("master side\n", encoding="utf-8")
    git(repo, "commit", "-aqm", "master change")
    conflict = subprocess.run(
        ["git", "-C", str(repo), "merge", "feature"], capture_output=True, text=True
    )
    assert conflict.returncode != 0, "expected a merge conflict"
    assert not gitops.is_clean(repo)

    tasks = [make_task("t1", "A", ["work.py"])]
    gate = FakeGate([GateDecision("abort")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side={"A": [], "B": []}, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    state = ExecState(
        phase="test",
        target_branch="master",
        target_base_oid="deadbeef",
        integration_branch="spar/integration",
        tasks={},
    )
    rc = ex._surface_merge_conflict(state, gitops.GitError("merge conflict"))
    assert rc == 5
    # The abort left a clean working tree (no lingering conflicted merge).
    assert gitops.is_clean(repo), "aborting left the repo mid-merge"


def test_merge_abort_is_noop_without_merge(repo):
    # No merge in progress: merge_abort must be a safe no-op (no raise).
    assert gitops.is_clean(repo)
    gitops.merge_abort(repo)
    assert gitops.is_clean(repo)


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
            Step(vblock("CONTINUE"), edits={"work.py": "v2\n"}),  # impl fix task t2
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


def test_recovery_empty_branch_equal_tip_restarts_not_merged(repo, tmp_path):
    # CRITICAL regression: a task interrupted (Ctrl+C) during its FIRST
    # implementer turn is left status="implementing" with the task branch
    # already created but pointing at the SAME commit as integration (zero
    # commits — no work done yet). ``git merge-base --is-ancestor`` reports a
    # commit as its own ancestor, so a naive is_ancestor check would wrongly
    # mark the task "merged", skip it, and report a false success with the
    # task's work never done. --continue must RESTART the task and run it to
    # real completion, ending merged with actual commits in integration.
    base_oid = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "branch", "spar/integration", "master")
    # Task branch created off integration with ZERO commits → equal tip.
    git(repo, "branch", "spar/t1-A", "spar/integration")
    assert (
        git(repo, "rev-parse", "spar/t1-A").stdout.strip()
        == git(repo, "rev-parse", "spar/integration").stdout.strip()
    )
    integration_oid_before = git(repo, "rev-parse", "spar/integration").stdout.strip()

    spar_dir = tmp_path / ".spar"
    store = ExecStateStore(spar_dir)
    task = make_task("t1", "A", ["work.py"], test="true")
    state = ExecState(
        phase="execution",
        target_branch="master",
        target_base_oid=base_oid,
        integration_branch="spar/integration",
        tasks={"t1": TaskState(task=task, status="implementing", branch="spar/t1-A")},
    )
    store.save(state)

    # Scripted adapters that DO the work on restart (an implementer turn that
    # writes+commits work, then a passing review).
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "restarted work\n"})],  # impl t1
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
    assert rc == 0, f"expected clean completion; logs={logs}"
    reloaded = store.load()
    # Task ran to REAL completion, not a false skip.
    assert reloaded.tasks["t1"].status == "merged"
    assert reloaded.phase == "done"
    # The task was genuinely restarted → an adapter was constructed and called.
    assert "A" in adapters and adapters["A"].calls, "task must have been restarted"
    # Integration actually advanced (real merge commit), not left at the empty tip.
    assert (
        git(repo, "rev-parse", "spar/integration").stdout.strip()
        != integration_oid_before
    )
    # The task's work is present in integration AND in the merged target.
    assert "work.py" in git(
        repo, "ls-tree", "-r", "--name-only", "spar/integration"
    ).stdout.split()
    assert "work.py" in git(repo, "ls-tree", "-r", "--name-only", "master").stdout.split()
    assert not branch_exists(repo, "spar/t1-A")


def test_empty_initial_implementation_retries_then_aborts(repo, tmp_path):
    # Bug: with a too-weak model the implementer never writes files on the
    # initial turn. That empty diff must NOT proceed to review/test (where the
    # reviewer emits DONE on nothing and the per-Task test then fails, spinning
    # forever). The initial turn is retried ONCE with a warning; still empty ->
    # abort loudly (exit 4) with a clear message, never entering review.
    tasks = [make_task("t1", "A", ["work.py"], test="true")]
    steps = {
        "A": [
            Step(vblock("CONTINUE")),  # initial turn: no edits
            Step(vblock("CONTINUE")),  # retry-with-warning: still no edits
        ],
        "B": [Step(vblock("DONE"))],  # reviewer MUST never be reached
    }
    gate = FakeGate([])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 4, f"expected protocol abort (4); logs={logs}"
    # clear abort message surfaced
    assert any("implementer created no files" in m for m in logs), logs
    # exactly the initial turn + one warned retry ran; the reviewer was never
    # invoked (we aborted before review/test).
    assert len(adapters["A"].calls) == 2
    assert adapters.get("B") is None or adapters["B"].calls == []
    # the second (retry) prompt carried the stern empty-implementation warning
    assert "no files on disk" in adapters["A"].calls[1]["prompt"].lower()
    # nothing was merged
    state = store.load()
    assert state.tasks["t1"].status != "merged"


def test_nonempty_initial_implementation_proceeds(repo, tmp_path):
    # Regression: an initial turn that writes a real file passes the empty-impl
    # guard on the FIRST try (no retry) and proceeds through review/test/merge.
    tasks = [make_task("t1", "A", ["work.py"], test="true")]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "print(1)\n"})],  # impl t1
        "B": [Step(vblock("DONE"))],  # review t1
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0, f"expected clean completion; logs={logs}"
    state = store.load()
    assert state.tasks["t1"].status == "merged"
    # exactly one implement turn (no empty-impl retry) and one review turn
    assert len(adapters["A"].calls) == 1
    assert len(adapters["B"].calls) == 1


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


# ---------------------------------------------------------------------------
# plan_path is resolved to an absolute path: the implementer runs with its
# worktree as cwd, so a relative plan path (".spar/artifact.md") would point
# nowhere from there. The prompt must carry the absolute path.
# ---------------------------------------------------------------------------


def test_plan_path_resolved_to_absolute_in_prompts(repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    plan_rel = Path(".spar") / "artifact.md"
    (tmp_path / ".spar").mkdir()
    (tmp_path / ".spar" / "artifact.md").write_text("plan\n", encoding="utf-8")

    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "x\n"})],
        "B": [Step(vblock("DONE"))],
    }
    gate = FakeGate([GateDecision("accept")])
    spar_dir = tmp_path / ".spar"
    make_adapter, adapters = make_factory(steps)
    store = ExecStateStore(spar_dir)
    ex = Executor(
        repo=repo,
        spar_dir=spar_dir,
        make_adapter=make_adapter,
        sides=side_cfg(),
        order=["A", "B"],
        plan_path=plan_rel,  # RELATIVE on purpose
        tasks=tuple(tasks),
        execution=ExecutionConfig(test_command="true"),
        gate=gate,
        store=store,
        log=lambda *_: None,
    )
    rc = ex.run()
    assert rc == 0
    impl_prompt = adapters["A"].calls[0]["prompt"]
    assert str(tmp_path / ".spar" / "artifact.md") in impl_prompt


# ---------------------------------------------------------------------------
# The reviewer adapter is built read-only; the implementer is not.
# ---------------------------------------------------------------------------


def test_reviewer_adapter_is_readonly(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "x\n"})],
        "B": [Step(vblock("DONE"))],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0
    # side A implemented t1 -> built without readonly; side B reviewed -> readonly
    assert adapters["A"].readonly_flags == [False]
    assert adapters["B"].readonly_flags == [True]


# ---------------------------------------------------------------------------
# Review-round cap wiring: max_review_rounds from ExecutionConfig reaches the
# cross-review loop; an abort decision at the gate exits 5.
# ---------------------------------------------------------------------------


def test_review_rounds_gate_abort_exits_5(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"work.py": "v1\n"}),  # initial impl
        ],
        "B": [
            Step(vblock("CONTINUE", remarks=["[MUST] never happy"])),  # review r1
        ],
    }
    gate = FakeGate([], review_decisions=[GateDecision("abort")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true", max_review_rounds=1),
    )
    rc = ex.run()
    assert rc == 5
    assert gate.review_calls and gate.review_calls[0][0] == "t1"
    # the still-open blocking remark was surfaced to the gate
    assert any(r.text == "never happy" for r in gate.review_calls[0][2])


def test_review_rounds_gate_accept_proceeds_to_test_and_merge(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"work.py": "v1\n"}),  # initial impl
        ],
        "B": [
            Step(vblock("CONTINUE", remarks=["[MUST] never happy"])),  # review r1
        ],
    }
    gate = FakeGate(
        [GateDecision("accept")], review_decisions=[GateDecision("accept")]
    )
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true", max_review_rounds=1),
    )
    rc = ex.run()
    assert rc == 0
    state = store.load()
    assert state.phase == "done"
    assert state.tasks["t1"].status == "merged"


# ---------------------------------------------------------------------------
# Fix-task cap: a final test that keeps failing opens at most max_fix_tasks
# integration-fix tasks, then aborts loudly instead of churning forever.
# ---------------------------------------------------------------------------


def test_fix_task_cap_aborts_after_budget(repo, tmp_path):
    # The fix task inherits execution.test_command as its per-task test, so a
    # bare ``false`` would loop the fix task's own implement/test cycle instead
    # of reaching the final test again. Use a call counter: invocation 1 (final
    # test after t1) FAILS -> opens fix t2; invocation 2 (per-task test of t2)
    # PASSES -> t2 merges; invocation 3 (final test again) FAILS -> cap trips.
    cnt = tmp_path / "cnt"
    test_cmd = (
        f'n=$(($(cat {cnt} 2>/dev/null || echo 0)+1)); echo $n > {cnt}; [ $n -eq 2 ]'
    )
    tasks = [make_task("t1", "A", ["work.py"], test="true")]
    # the failing output names no files -> no failing files -> the fix task
    # lands on order[0] == side A; side B reviews it.
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"work.py": "v1\n"}),  # impl t1
            Step(vblock("CONTINUE"), edits={"work.py": "fix\n"}),  # impl fix t2
        ],
        "B": [
            Step(vblock("DONE")),  # review t1
            Step(vblock("DONE")),  # review fix t2
        ],
    }
    # only ONE fix task budgeted
    gate = FakeGate([])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command=test_cmd, max_fix_tasks=1),
    )
    rc = ex.run()
    assert rc == 4
    state = store.load()
    # exactly one fix task was opened (t2), then the cap tripped
    assert set(state.tasks) == {"t1", "t2"}
    assert any("fix" in ln and "cap" in ln.lower() or "budget" in ln.lower() for ln in logs)


def test_fix_task_cap_zero_is_unlimited(repo, tmp_path):
    # max_fix_tasks=0 keeps today's behavior: failing final test opens a fix
    # task; a then-passing final test merges. (One fix round here.)
    sentinel = tmp_path / "final_sent"
    final = f"test -f {sentinel} || (touch {sentinel}; exit 1)"
    tasks = [make_task("t1", "A", ["work.py"], test="true")]
    steps = {
        "A": [
            Step(vblock("CONTINUE"), edits={"work.py": "v1\n"}),  # impl t1
            Step(vblock("CONTINUE"), edits={"work.py": "fix\n"}),  # impl fix t2
        ],
        "B": [
            Step(vblock("DONE")),  # review t1
            Step(vblock("DONE")),  # review fix t2
        ],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command=final, max_fix_tasks=0),
    )
    rc = ex.run()
    assert rc == 0
    state = store.load()
    assert state.phase == "done"


# ---------------------------------------------------------------------------
# ConsoleExecGate.review_rounds_exhausted_gate: input handling.
# ---------------------------------------------------------------------------


def test_console_review_rounds_gate_accept_extend_abort():
    from spar.exec.loop import ConsoleExecGate

    def drive(answers):
        answers = list(answers)
        printed = []
        gate = ConsoleExecGate(
            input_fn=lambda _: answers.pop(0), print_fn=printed.append
        )
        return gate.review_rounds_exhausted_gate("t1", 3, []), printed

    decision, _ = drive(["a"])
    assert decision.action == "accept"

    decision, _ = drive(["x"])
    assert decision.action == "abort"

    # extend: rejects junk / non-positive counts, then accepts a valid one
    decision, printed = drive(["e", "abc", "e", "0", "e", "2"])
    assert decision.action == "extend"
    assert decision.extra_rounds == 2

    # unknown answer re-prompts
    decision, printed = drive(["?", "a"])
    assert decision.action == "accept"
    assert any("'a', 'e' or 'x'" in p for p in printed)


def test_reviewer_prompt_lists_unmerged_tasks_files_only(repo, tmp_path):
    # While t1 is under review, t2 (pending) is foreign; after t1 merges,
    # t2's review must NOT list t1 (already merged).
    tasks = [
        make_task("t1", "A", ["work1.py"]),
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
    # B's first call reviewed t1: t2 was pending -> listed as foreign;
    # nothing merged yet -> no merged-files section
    assert "t2: work2.py" in adapters["B"].calls[0]["prompt"]
    assert "already merged from earlier tasks" not in adapters["B"].calls[0]["prompt"]
    # A's second call reviewed t2: t1 already merged -> NOT foreign, but its
    # actual file appears in the merged-files section
    assert "t1: work1.py" not in adapters["A"].calls[1]["prompt"]
    assert "work1.py" in adapters["A"].calls[1]["prompt"]


def test_merge_summary_lists_open_nice_remarks(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "x\n"})],
        # DONE with a NICE remark: non-blocking, stays pending through merge
        "B": [Step(vblock("DONE", remarks=["[NICE] consider a docstring"]))],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0
    summary = gate.calls[0]
    assert "open NICE remarks" in summary
    assert "consider a docstring" in summary
    assert "[t1]" in summary


def test_merge_summary_omits_nice_block_when_none(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step(vblock("CONTINUE"), edits={"work.py": "x\n"})],
        "B": [Step(vblock("DONE"))],
    }
    gate = FakeGate([GateDecision("accept")])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 0
    assert "open NICE remarks" not in gate.calls[0]


def test_keyboard_interrupt_exits_130_with_resume_hint(repo, tmp_path):
    tasks = [make_task("t1", "A", ["work.py"])]
    steps = {
        "A": [Step("", raises=KeyboardInterrupt())],
        "B": [],
    }
    gate = FakeGate([])
    ex, adapters, store, logs = build_executor(
        repo, tmp_path, tasks=tasks, steps_by_side=steps, gate=gate,
        execution=ExecutionConfig(test_command="true"),
    )
    rc = ex.run()
    assert rc == 130
    assert any("--continue" in ln for ln in logs)
    # state survived and the lock is free: a resume can load it
    assert store.exists()
    with store.locked():
        pass
