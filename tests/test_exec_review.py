"""Tests for the asymmetric cross-review loop (spar/exec/review.py).

These use in-Python scripted fake adapters over a *real* tmp git repo plus a
worktree checked out on the task branch. The implementer adapter's scripted
steps write files into the worktree (git then commits them); the reviewer
adapter never touches the filesystem — it only reads the diff embedded in its
prompt and returns a verdict.
"""

import subprocess
from pathlib import Path

import pytest

from spar.adapters.base import SessionLost, TurnResult
from spar.exec.review import ReviewAbort, run_cross_review
from spar.exec.state import ExecState, ExecStateStore, TaskState
from spar.exec.tasklist import Task
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
    """One scripted adapter turn.

    ``edits`` is a mapping of worktree-relative path -> content written before
    the reply is returned (used by the implementer). ``raises`` raises instead.
    """

    def __init__(self, reply, sid="sess", edits=None, raises=None):
        self.reply = reply
        self.sid = sid
        self.edits = edits or {}
        self.raises = raises

    def __call__(self, prompt, session_id, root):
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
    def __init__(self, name, steps, root=None):
        self.name = name
        self.steps = list(steps)
        self.root = root  # worktree for the implementer; None for the reviewer
        self.calls = []

    def run_turn(self, prompt, session_id, timeout_sec):
        self.calls.append(
            {"prompt": prompt, "session_id": session_id, "timeout": timeout_sec}
        )
        if not self.steps:
            raise AssertionError(f"{self.name}: no scripted step left for this call")
        return self.steps.pop(0)(prompt, session_id, self.root)


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )


@pytest.fixture
def env(tmp_path):
    """A repo with master seed, a spar/integration branch, a task branch, and a
    worktree checked out on the task branch. Returns a small namespace dict."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")

    _git(repo, "branch", "spar/integration", "master")
    branch = "spar/t1-A"
    _git(repo, "branch", branch, "spar/integration")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), branch)

    store = ExecStateStore(tmp_path / ".spar")
    return {
        "repo": repo,
        "worktree": worktree,
        "integration_base": "spar/integration",
        "branch": branch,
        "store": store,
        "plan_path": tmp_path / "plan.md",
    }


def _task(files=("work.py",)):
    return Task(
        id="t1",
        description="do the thing",
        side="A",
        model="m-impl",
        review_model="m-rev",
        deps=(),
        files=tuple(files),
        test=None,
    )


def _run(env, task, impl_steps, review_steps):
    impl = FakeAdapter("impl", impl_steps, root=env["worktree"])
    review = FakeAdapter("review", review_steps, root=None)
    task_state = TaskState(task=task, status="review", branch=env["branch"])
    exec_state = ExecState(
        target_branch="feature",
        integration_branch=env["integration_base"],
        tasks={task.id: task_state},
    )
    logs = []
    run_cross_review(
        task_state=task_state,
        impl_adapter=impl,
        review_adapter=review,
        repo=env["repo"],
        worktree=env["worktree"],
        integration_base=env["integration_base"],
        plan_path=env["plan_path"],
        timeout_sec=30,
        store=env["store"],
        exec_state=exec_state,
        log=logs.append,
    )
    return impl, review, task_state, exec_state, logs


def _branch_files(env):
    out = _git(env["repo"], "diff", "--name-only", f"{env['integration_base']}..{env['branch']}")
    return [ln for ln in out.stdout.splitlines() if ln]


# ---------------------------------------------------------------------------
# Scenario 1: MUST raised -> implementer resolves it -> DONE
# ---------------------------------------------------------------------------


def test_must_then_resolve_then_done(env):
    task = _task(files=("work.py",))
    impl_steps = [
        Step(
            vblock("CONTINUE", resolved=["#1 accepted"]),
            edits={"work.py": "print('hi')\n"},
        ),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] add a docstring"])),
        Step(vblock("DONE")),
    ]
    impl, review, task_state, exec_state, logs = _run(env, task, impl_steps, review_steps)

    assert len(review.calls) == 2
    assert len(impl.calls) == 1
    assert len(task_state.resolved_remarks) == 1
    assert task_state.resolved_remarks[0].resolution == "accepted"
    # no blocking remark left pending
    assert not [r for r in task_state.pending_remarks if r.severity in (Severity.MUST, Severity.USER)]
    # the implementer's commit landed on the task branch
    assert "work.py" in _branch_files(env)
    assert exec_state.turn_in_progress is None
    # state persisted
    reloaded = env["store"].load()
    assert len(reloaded.tasks["t1"].resolved_remarks) == 1


# ---------------------------------------------------------------------------
# Scenario 2: reviewer DONE immediately, zero implementer turns
# ---------------------------------------------------------------------------


def test_done_immediately_no_impl_turn(env):
    task = _task()
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps=[], review_steps=[Step(vblock("DONE"))]
    )
    assert len(review.calls) == 1
    assert len(impl.calls) == 0
    assert task_state.pending_remarks == []
    assert exec_state.turn_in_progress is None


# ---------------------------------------------------------------------------
# Scenario 3: out-of-scope edit -> rollback + retry -> second violation aborts
# ---------------------------------------------------------------------------


def test_out_of_scope_edit_rolls_back_and_aborts_on_second(env):
    task = _task(files=("allowed.py",))
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"forbidden.py": "nope\n"}),
        Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"forbidden.py": "still\n"}),
    ]
    review_steps = [Step(vblock("CONTINUE", remarks=["[MUST] implement it"]))]

    impl = FakeAdapter("impl", impl_steps, root=env["worktree"])
    review = FakeAdapter("review", review_steps, root=None)
    task_state = TaskState(task=task, status="review", branch=env["branch"])
    exec_state = ExecState(
        integration_branch=env["integration_base"], tasks={task.id: task_state}
    )

    with pytest.raises(ReviewAbort):
        run_cross_review(
            task_state=task_state,
            impl_adapter=impl,
            review_adapter=review,
            repo=env["repo"],
            worktree=env["worktree"],
            integration_base=env["integration_base"],
            plan_path=env["plan_path"],
            timeout_sec=30,
            store=env["store"],
            exec_state=exec_state,
            log=lambda *_: None,
        )

    # both implementer attempts happened, then abort
    assert len(impl.calls) == 2
    # worktree was rolled back clean; forbidden file is gone
    assert not (env["worktree"] / "forbidden.py").exists()
    porcelain = _git(env["worktree"], "status", "--porcelain").stdout
    assert porcelain.strip() == ""
    # nothing landed on the branch
    assert _branch_files(env) == []


# ---------------------------------------------------------------------------
# Scenario 4: MUST stays open across a turn; a DONE while it is open does not
# terminate; only a DONE after resolution returns.
# ---------------------------------------------------------------------------


def test_no_premature_done_while_must_open(env):
    task = _task(files=("work.py",))
    impl_steps = [
        # first turn: resolves nothing, makes no edit -> #1 stays open
        Step(vblock("CONTINUE")),
        # second turn: resolves #1 and edits the file
        Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"work.py": "done\n"}),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] must fix"])),  # raise #1
        Step(vblock("DONE")),  # DONE but #1 still open -> must NOT terminate
        Step(vblock("DONE")),  # #1 now resolved -> terminate
    ]
    impl, review, task_state, exec_state, logs = _run(env, task, impl_steps, review_steps)

    assert len(review.calls) == 3  # the premature DONE did not end the loop
    assert len(impl.calls) == 2
    assert len(task_state.resolved_remarks) == 1
    assert task_state.pending_remarks == []
    assert "work.py" in _branch_files(env)
