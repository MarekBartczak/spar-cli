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
    ``commit=True`` additionally commits the edits itself (an agent running its
    own ``git commit`` inside the worktree).
    """

    def __init__(self, reply, sid="sess", edits=None, raises=None, commit=False, emit=None):
        self.reply = reply
        self.sid = sid
        self.edits = edits or {}
        self.raises = raises
        self.commit = commit
        self.emit = emit

    def __call__(self, prompt, session_id, root, on_event=None):
        if self.raises is not None:
            raise self.raises
        for rel, content in self.edits.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        if self.commit:
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "agent self-commit")
        if self.emit is not None and on_event is not None:
            on_event(self.emit)
        return TurnResult(
            session_id=self.sid, reply_text=self.reply, events_path=Path("ev"), exit_code=0
        )


class FakeAdapter:
    def __init__(self, name, steps, root=None):
        self.name = name
        self.steps = list(steps)
        self.root = root  # worktree for the implementer; None for the reviewer
        self.calls = []

    def run_turn(self, prompt, session_id, timeout_sec, on_event=None):
        self.calls.append(
            {"prompt": prompt, "session_id": session_id, "timeout": timeout_sec}
        )
        if not self.steps:
            raise AssertionError(f"{self.name}: no scripted step left for this call")
        return self.steps.pop(0)(prompt, session_id, self.root, on_event=on_event)


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


def _run(env, task, impl_steps, review_steps, **extra):
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
        **extra,
    )
    return impl, review, task_state, exec_state, logs


def _branch_files(env):
    out = _git(env["repo"], "diff", "--name-only", f"{env['integration_base']}..{env['branch']}")
    return [ln for ln in out.stdout.splitlines() if ln]


# ---------------------------------------------------------------------------
# start_with="implementer": the review-resume `extend` path re-enters the loop
# at an implementer turn, skipping the reviewer block exactly once (no reviewer
# verdict, no new remark, no round counted) so the still-open remarks go
# straight to the implementer.
# ---------------------------------------------------------------------------


def test_start_with_implementer_skips_reviewer_once(env):
    from spar.state import StateRemark

    task = _task(files=("work.py",))
    # Non-empty branch so a later reviewer turn has a real diff to read.
    (env["worktree"] / "work.py").write_text("seed impl\n", encoding="utf-8")
    _git(env["worktree"], "add", "-A")
    _git(env["worktree"], "commit", "-qm", "t1: seed")

    task_state = TaskState(task=task, status="review", branch=env["branch"])
    task_state.pending_remarks.append(
        StateRemark(remark_id=1, severity=Severity.MUST, author="reviewer", text="fix the bug")
    )
    task_state.next_remark_id = 2
    exec_state = ExecState(
        target_branch="feature",
        integration_branch=env["integration_base"],
        tasks={task.id: task_state},
    )

    impl = FakeAdapter(
        "impl",
        [Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"work.py": "fix\n"})],
        root=env["worktree"],
    )
    review = FakeAdapter("review", [Step(vblock("DONE"))], root=None)

    # Record the interleaving of adapter calls to prove the implementer runs first.
    seq: list[str] = []
    _orig_impl, _orig_review = impl.run_turn, review.run_turn
    impl.run_turn = lambda *a, **k: (seq.append("impl"), _orig_impl(*a, **k))[1]
    review.run_turn = lambda *a, **k: (seq.append("review"), _orig_review(*a, **k))[1]

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
        start_with="implementer",
    )

    # The FIRST adapter call was the implementer's, and its prompt listed the
    # still-open remark #1; the reviewer was called exactly once, after.
    assert seq == ["impl", "review"]
    assert "#1" in impl.calls[0]["prompt"]
    assert "fix the bug" in impl.calls[0]["prompt"]
    assert len(review.calls) == 1
    # The loop converged: the MUST was resolved, none left pending blocking.
    assert not [
        r for r in task_state.pending_remarks if r.severity in (Severity.MUST, Severity.USER)
    ]


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


# ---------------------------------------------------------------------------
# Scenario 5: a verdict-only retry that makes an out-of-scope edit must NOT
# slip past the scope guard. The FIRST implementer reply has a malformed
# verdict AND edits an out-of-scope file; the verdict-only retry returns a
# valid verdict but the out-of-scope change is still present in the worktree.
# The turn must be rolled back (nothing lands on the branch); a second such
# turn aborts.
# ---------------------------------------------------------------------------


def test_verdict_retry_out_of_scope_edit_rolls_back_and_aborts(env):
    task = _task(files=("allowed.py",))
    impl_steps = [
        # turn 1, first reply: malformed verdict, IN-scope (no edit) -> passes the
        # old first-reply scope check, then triggers a verdict-only retry.
        Step("prose only, no verdict block", edits={}),
        # turn 1, verdict-only retry: valid verdict AND an out-of-scope edit that
        # the old code never scope-checked before committing.
        Step(vblock("CONTINUE"), edits={"forbidden.py": "nope\n"}),
        # turn 2, first reply: malformed verdict, no edit
        Step("still no verdict here", edits={}),
        # turn 2, verdict-only retry: valid verdict + out-of-scope edit again
        Step(vblock("CONTINUE"), edits={"forbidden.py": "again\n"}),
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

    # both turns ran their first reply + verdict-only retry (4 impl calls)
    assert len(impl.calls) == 4
    # the out-of-scope change never landed and the worktree is clean
    assert not (env["worktree"] / "forbidden.py").exists()
    assert _git(env["worktree"], "status", "--porcelain").stdout.strip() == ""
    assert _branch_files(env) == []


# ---------------------------------------------------------------------------
# Scenario 6: a single '*' glob must NOT cross a path separator, so a nested
# file under an allowed directory is out of scope and rolled back.
# ---------------------------------------------------------------------------


def test_nested_file_under_star_glob_is_out_of_scope(env):
    task = _task(files=("src/*.py",))
    impl_steps = [
        Step(vblock("CONTINUE"), edits={"src/sub/deep.py": "x\n"}),
        Step(vblock("CONTINUE"), edits={"src/sub/deep.py": "y\n"}),
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

    # nested edit was treated as a scope violation and rolled back
    assert not (env["worktree"] / "src" / "sub" / "deep.py").exists()
    assert _branch_files(env) == []


# ---------------------------------------------------------------------------
# Scenario 7: the implementer's OWN remarks in its verdict never enter the
# ledger — only the reviewer raises remarks.
# ---------------------------------------------------------------------------


def test_anti_spin_accept_without_edit_aborts(env):
    # An implementer that marks a remark accepted but writes NO file across turns
    # is not converging. The anti-spin guard retries each such turn once (stern
    # warning) and, after consecutive no-change turns, raises ReviewAbort rather
    # than spinning forever on an empty diff.
    task = _task(files=("work.py",))
    # 2 turns x (1 turn + 1 anti-spin retry) = 4 implementer calls, each accepts
    # #1 while writing nothing.
    impl_steps = [Step(vblock("CONTINUE", resolved=["#1 accepted"])) for _ in range(4)]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] implement it"])),  # raises #1
        Step(vblock("CONTINUE", remarks=["[MUST] still nothing on disk"])),  # #2
    ]

    impl = FakeAdapter("impl", impl_steps, root=env["worktree"])
    review = FakeAdapter("review", review_steps, root=None)
    task_state = TaskState(task=task, status="review", branch=env["branch"])
    exec_state = ExecState(
        integration_branch=env["integration_base"], tasks={task.id: task_state}
    )

    with pytest.raises(ReviewAbort) as excinfo:
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
    assert "no changes" in str(excinfo.value)
    # It aborted rather than spinning: two reviewer turns, two implementer turns
    # (each retried once by the anti-spin guard).
    assert len(review.calls) == 2
    assert len(impl.calls) == 4
    # nothing landed on the branch
    assert _branch_files(env) == []


def test_anti_spin_does_not_trip_legit_progress(env):
    # Regression: a task that edits a real file each time it addresses a remark
    # converges to reviewer DONE normally — the anti-spin guard never fires.
    task = _task(files=("work.py",))
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"work.py": "v1\n"}),
        Step(vblock("CONTINUE", resolved=["#2 accepted"]), edits={"work.py": "v2\n"}),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] first fix"])),  # #1
        Step(vblock("CONTINUE", remarks=["[MUST] second fix"])),  # #2
        Step(vblock("DONE")),  # both resolved -> terminate
    ]
    impl, review, task_state, exec_state, logs = _run(env, task, impl_steps, review_steps)

    assert len(review.calls) == 3
    assert len(impl.calls) == 2  # one call per turn, no anti-spin retry
    assert len(task_state.resolved_remarks) == 2
    assert task_state.pending_remarks == []
    assert "work.py" in _branch_files(env)


def test_implementer_wrong_status_is_coerced_to_continue(env):
    # A live model treated the implementer turn as a review and emitted the
    # wrong status (AGREE) while still writing the file and resolving the
    # remark. The loop must coerce the status to CONTINUE rather than let a
    # non-reviewer status derail or terminate the cross-review loop.
    task = _task(files=("work.py",))
    impl_steps = [
        Step(
            vblock("AGREE", resolved=["#1 accepted"]),
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
    assert not [
        r for r in task_state.pending_remarks if r.severity in (Severity.MUST, Severity.USER)
    ]
    assert "work.py" in _branch_files(env)
    assert exec_state.turn_in_progress is None
    assert any("coerc" in line.lower() for line in logs)


def test_implementer_bare_continue_verdict_with_no_open_remarks_parses(env):
    # First implementer turn with NO open remarks yet (the reviewer's first
    # turn raised nothing). A bare `status: CONTINUE` verdict with neither a
    # `resolved:` nor a `remarks:` section must parse fine and the turn must
    # apply normally.
    task = _task(files=("work.py",))
    impl_steps = [
        Step(vblock("CONTINUE"), edits={"work.py": "print('hi')\n"}),
    ]
    review_steps = [
        Step(vblock("CONTINUE")),
        Step(vblock("DONE")),
    ]
    impl, review, task_state, exec_state, logs = _run(env, task, impl_steps, review_steps)

    assert len(review.calls) == 2
    assert len(impl.calls) == 1
    assert task_state.pending_remarks == []
    assert task_state.resolved_remarks == []
    assert "work.py" in _branch_files(env)


# ---------------------------------------------------------------------------
# Review-round cap: a non-converging review (reviewer keeps raising MUSTs while
# the implementer makes real edits every turn) must not churn forever — after
# ``max_rounds`` reviewer CONTINUE verdicts the ``rounds_gate`` decides:
# accept-as-is (stop reviewing), extend (more rounds), or — without a gate —
# the loop aborts loudly.
# ---------------------------------------------------------------------------


class FakeRoundsGate:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def __call__(self, task_state, rounds):
        self.calls.append((task_state.task.id, rounds))
        return self.decisions.pop(0)


def test_review_round_cap_gate_accept_stops_loop(env):
    from spar.orchestrator import GateDecision

    task = _task(files=("work.py",))
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"work.py": "v1\n"}),
    ]
    # Reviewer would CONTINUE forever; only 1 round is budgeted.
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] never satisfied"])),
        Step(vblock("CONTINUE", remarks=["[MUST] still never satisfied"])),
    ]
    gate = FakeRoundsGate([GateDecision(action="accept")])
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps, review_steps, max_rounds=1, rounds_gate=gate
    )
    # gate consulted after the 1st non-terminating reviewer verdict; accept
    # ends the loop with NO implementer turn for that round
    assert gate.calls == [("t1", 1)]
    assert len(review.calls) == 1
    assert len(impl.calls) == 0


def test_review_round_cap_gate_extend_allows_more_rounds(env):
    from spar.orchestrator import GateDecision

    task = _task(files=("work.py",))
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"work.py": "v1\n"}),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] fix it"])),  # round 1 -> gate
        Step(vblock("DONE")),  # round 2 (granted by extend)
    ]
    gate = FakeRoundsGate([GateDecision(action="extend", extra_rounds=1)])
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps, review_steps, max_rounds=1, rounds_gate=gate
    )
    assert gate.calls == [("t1", 1)]
    assert len(review.calls) == 2
    assert len(impl.calls) == 1
    assert "work.py" in _branch_files(env)


def test_review_round_cap_without_gate_aborts(env):
    task = _task(files=("work.py",))
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"work.py": "v1\n"}),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] never satisfied"])),
    ]
    with pytest.raises(ReviewAbort) as excinfo:
        _run(env, task, impl_steps, review_steps, max_rounds=1)
    assert "round" in str(excinfo.value)


def test_review_round_cap_zero_means_unlimited(env):
    # max_rounds=0 (the default) keeps today's behavior: no cap.
    task = _task(files=("work.py",))
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 accepted"]), edits={"work.py": "v1\n"}),
        Step(vblock("CONTINUE", resolved=["#2 accepted"]), edits={"work.py": "v2\n"}),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] first"])),
        Step(vblock("CONTINUE", remarks=["[MUST] second"])),
        Step(vblock("DONE")),
    ]
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps, review_steps, max_rounds=0
    )
    assert len(review.calls) == 3


# ---------------------------------------------------------------------------
# Review dispute: a justified rejection loop (reviewer keeps re-raising a
# [MUST], implementer keeps REJECTING it with a reason and changing no files) is
# a legitimate disagreement — it must escalate to the SAME user gate as
# review-round exhaustion, NOT die as a no-change ReviewAbort.
# ---------------------------------------------------------------------------


def test_dispute_rejection_loop_escalates_to_gate_accept(env):
    from spar.orchestrator import GateDecision

    task = _task(files=("work.py",))
    # Each impl turn rejects the standing remark with a reason and edits nothing.
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 rejected: plan Decision 4 says otherwise"])),
        Step(vblock("CONTINUE", resolved=["#2 rejected: plan Decision 4 says otherwise"])),
    ]
    # Reviewer defends the task text, re-raising the same concern each round.
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] the task text requires X"])),
        Step(vblock("CONTINUE", remarks=["[MUST] the task text still requires X"])),
    ]
    gate = FakeRoundsGate([GateDecision(action="accept")])
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps, review_steps, rounds_gate=gate
    )
    # The dispute reached the gate (not a ReviewAbort) after two no-change
    # rejection turns; accept returned normally.
    assert len(gate.calls) == 1
    assert gate.calls[0][0] == "t1"
    assert len(impl.calls) == 2
    assert len(review.calls) == 2
    # Both rejected remarks were recorded as resolved (rejected), none pending.
    assert len(task_state.resolved_remarks) == 2
    assert all(rr.resolution == "rejected" for rr in task_state.resolved_remarks)
    assert task_state.pending_remarks == []


def test_dispute_rejection_loop_gate_extend_continues(env):
    from spar.orchestrator import GateDecision

    task = _task(files=("work.py",))
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 rejected: disagree"])),
        Step(vblock("CONTINUE", resolved=["#2 rejected: disagree"])),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] concern"])),  # r1 -> reject
        Step(vblock("CONTINUE", remarks=["[MUST] same concern"])),  # r2 -> reject -> gate
        Step(vblock("DONE")),  # r3 (granted by extend): no open blocking -> done
    ]
    gate = FakeRoundsGate([GateDecision(action="extend", extra_rounds=1)])
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps, review_steps, rounds_gate=gate
    )
    # Gate consulted once; extend reset the streak and the reviewer got another
    # turn, which DONEd (both remarks already resolved-as-rejected -> no block).
    assert len(gate.calls) == 1
    assert len(review.calls) == 3
    assert len(impl.calls) == 2
    assert len(task_state.resolved_remarks) == 2


def test_dispute_rejection_loop_without_gate_aborts(env):
    task = _task(files=("work.py",))
    impl_steps = [
        Step(vblock("CONTINUE", resolved=["#1 rejected: disagree"])),
        Step(vblock("CONTINUE", resolved=["#2 rejected: disagree"])),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] concern"])),
        Step(vblock("CONTINUE", remarks=["[MUST] same concern"])),
    ]
    # No rounds_gate: a dispute with nowhere to escalate keeps today's abort.
    with pytest.raises(ReviewAbort) as excinfo:
        _run(env, task, impl_steps, review_steps)
    assert "no changes" in str(excinfo.value)


def test_true_spin_with_gate_still_aborts(env):
    from spar.orchestrator import GateDecision

    task = _task(files=("work.py",))
    # The implementer does NOTHING: no resolutions, no edits. There is no
    # dispute to arbitrate, so even WITH a gate this stays a hard ReviewAbort.
    impl_steps = [Step(vblock("CONTINUE")), Step(vblock("CONTINUE"))]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] implement it"])),
        Step(vblock("CONTINUE", remarks=["[MUST] still nothing"])),
    ]
    gate = FakeRoundsGate([GateDecision(action="accept")])
    with pytest.raises(ReviewAbort) as excinfo:
        _run(env, task, impl_steps, review_steps, rounds_gate=gate)
    assert "no changes" in str(excinfo.value)
    # The gate was never consulted — nothing to arbitrate.
    assert gate.calls == []


# ---------------------------------------------------------------------------
# Agent self-commit: an implementer that runs ``git commit`` itself inside the
# worktree must still (a) count as having made changes (no false anti-spin
# abort) and (b) have its committed paths scope-checked and rolled back on a
# violation.
# ---------------------------------------------------------------------------


def test_self_committed_in_scope_change_counts_as_progress(env):
    task = _task(files=("work.py",))
    impl_steps = [
        Step(
            vblock("CONTINUE", resolved=["#1 accepted"]),
            edits={"work.py": "print('hi')\n"},
            commit=True,  # the agent commits its own work
        ),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] implement it"])),
        Step(vblock("DONE")),
    ]
    impl, review, task_state, exec_state, logs = _run(env, task, impl_steps, review_steps)

    # exactly ONE implementer call: no accept-without-edit retry, no anti-spin
    assert len(impl.calls) == 1
    assert len(review.calls) == 2
    assert task_state.resolved_remarks[0].resolution == "accepted"
    assert "work.py" in _branch_files(env)


def test_self_committed_out_of_scope_change_rolled_back_and_aborts(env):
    task = _task(files=("allowed.py",))
    impl_steps = [
        Step(vblock("CONTINUE"), edits={"forbidden.py": "nope\n"}, commit=True),
        Step(vblock("CONTINUE"), edits={"forbidden.py": "again\n"}, commit=True),
    ]
    review_steps = [Step(vblock("CONTINUE", remarks=["[MUST] implement it"]))]

    with pytest.raises(ReviewAbort):
        _run(env, task, impl_steps, review_steps)

    # the self-committed out-of-scope change must NOT survive on the branch
    assert _branch_files(env) == []
    assert not (env["worktree"] / "forbidden.py").exists()


def test_impl_own_remarks_not_added_to_ledger(env):
    task = _task(files=("work.py",))
    impl_steps = [
        Step(
            vblock(
                "CONTINUE",
                resolved=["#1 accepted"],
                remarks=["[MUST] impl's own remark", "[NICE] impl's own nice"],
            ),
            edits={"work.py": "x\n"},
        ),
    ]
    review_steps = [
        Step(vblock("CONTINUE", remarks=["[MUST] reviewer remark"])),  # raises #1
        Step(vblock("DONE")),  # #1 resolved -> terminate
    ]
    impl, review, task_state, exec_state, logs = _run(env, task, impl_steps, review_steps)

    # only the reviewer's single remark was ever ledgered
    assert task_state.next_remark_id == 2  # started at 1, +1 for the reviewer only
    assert len(task_state.resolved_remarks) == 1
    assert task_state.pending_remarks == []
    # none of the implementer's own remark texts leaked into either ledger
    all_texts = [r.text for r in task_state.pending_remarks] + [
        rr.remark.text for rr in task_state.resolved_remarks
    ]
    assert not any("impl's own" in t for t in all_texts)


def test_foreign_and_merged_files_reach_the_review_prompt(env):
    task = _task(files=("work.py",))
    impl_steps = []
    review_steps = [Step(vblock("DONE"))]
    impl, review, task_state, exec_state, logs = _run(
        env, task, impl_steps, review_steps,
        foreign_files=(("t9", ("src/*.cpp",)),),
        merged_files=("lib/util.py",),
    )
    assert "t9: src/*.cpp" in review.calls[0]["prompt"]
    assert "lib/util.py" in review.calls[0]["prompt"]
