"""Sequential execution Executor — the Task FSM driver (spec §5–§9, §11).

After a Debate produces a consensus Plan with a machine-parsable ``## Tasks``
list, ``spar exec`` runs this Executor to actually implement it. It drives each
Task through the FSM (spec §6):

    pending → ready → implementing → review → testing → merged

sequentially (one ready Task at a time), merges each into a single
``spar/integration`` accumulator, runs the final Test phase over the whole
Integration branch, opens an integration-fix Task on a final-suite failure
(§7), and — gated by the user (§9) — merges Integration into the target branch.

Isolation (§5): the target branch is recorded (name + base OID) and must be
clean at start. Integration is branched from that base. Each Task gets a
short-lived branch ``spar/<id>-<side>`` off the *current* Integration and a
worktree ``<spar_dir>/worktrees/<side>``; the implementing Side edits there,
the reviewing Side reads the diff (never checks out). After review DONE and a
passing per-Task test the branch merges into Integration and is deleted.

Crash recovery (§11.1) reconciles recorded FSM state against actual git via
ancestor checks rather than trusting either alone: a Task branch that is already
an ancestor of Integration was merged (mark it merged, delete the lingering
branch); a Task recorded merged whose branch survives is simply cleaned up; an
interrupted mid-turn / mid-test Task is restarted; an interrupted final merge is
detected by Integration already being an ancestor of the target.

The heavy lifting of a single implement turn (prompt → adapter → scope guard →
commit) and the asymmetric review loop are reused verbatim from
``spar.exec.review`` (``_implementer_turn`` for the initial code-creating turn,
``run_cross_review`` for the review loop).

Exit codes mirror v1: 0 ok, 3 lock/state guard, 4 protocol/adapter abort,
5 user abort at the final-merge gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Protocol

from spar.adapters.base import Adapter, AdapterError
from spar.config import ExecutionConfig, SideConfig
from spar.exec import gitops
from spar.exec.gitops import GitError
from spar.exec.review import ReviewAbort, _implementer_turn, run_cross_review
from spar.exec.state import ExecState, ExecStateStore, TaskState
from spar.exec.tasklist import Task
from spar.orchestrator import GateDecision
from spar.state import LockHeld, StateError

__all__ = ["Executor", "ExecGate", "ConsoleExecGate"]

_DEFAULT_TIMEOUT_SEC = 900


# ---------------------------------------------------------------------------
# Final-merge gate (mirrors v1's ConsoleGate; reuses GateDecision)
# ---------------------------------------------------------------------------


class ExecGate(Protocol):
    """The single user decision point of Execution (the final merge, §9)."""

    def final_merge_gate(self, summary: str) -> GateDecision: ...


class ConsoleExecGate:
    """Default :class:`ExecGate` talking to a human over stdin/stdout.

    ``input_fn``/``print_fn`` are injectable so the gate is deterministically
    drivable from tests, exactly like v1's ``ConsoleGate``.
    """

    def __init__(self, input_fn=input, print_fn=print) -> None:
        self._input = input_fn
        self._print = print_fn

    def final_merge_gate(self, summary: str) -> GateDecision:
        self._print(summary)
        while True:
            choice = self._input("[a]ccept merge / [x] abort? ").strip().lower()
            if choice in ("a", "accept"):
                return GateDecision(action="accept")
            if choice in ("x", "abort"):
                return GateDecision(action="abort")
            self._print("Please answer with 'a' or 'x'.")


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class _Abort(Exception):
    """Internal control-flow signal carrying the process exit code."""

    def __init__(self, code: int) -> None:
        super().__init__(f"abort with exit code {code}")
        self.code = code


class Executor:
    """Drives the sequential Task FSM to a user-gated final merge."""

    def __init__(
        self,
        *,
        repo: Path,
        spar_dir: Path,
        # ``(side, worktree, model) -> Adapter``: builds the Adapter for one
        # turn. ``model`` is the negotiated per-Task model to run (the
        # implementer's ``task.model`` or the reviewer's ``task.review_model``)
        # so the Assignment negotiated for the Task actually drives which
        # model executes the turn, rather than whatever default the factory
        # would otherwise pick.
        make_adapter: Callable[[str, Path, str], Adapter],
        sides: dict[str, SideConfig],
        order: list[str],
        plan_path: Path,
        tasks: tuple[Task, ...],
        execution: ExecutionConfig,
        gate: ExecGate,
        store: ExecStateStore,
        log=print,
        auto_integration_merge: bool = False,
    ) -> None:
        # ``sides`` (SideConfig catalog) extends the brief: it is required to
        # generate the model/review of an integration-fix Task from each Side's
        # ``default_model`` (spec §7).
        if len(order) != 2:
            raise ValueError("sequential execution requires exactly two sides")
        self.repo = Path(repo)
        self.spar_dir = Path(spar_dir)
        self.make_adapter = make_adapter
        self.sides = sides
        self.order = list(order)
        self.plan_path = Path(plan_path)
        self.tasks = tuple(tasks)
        self.execution = execution
        self.gate = gate
        self.store = store
        self.log = log
        self.auto_integration_merge = auto_integration_merge

    # -- public entry points -------------------------------------------

    def run(self) -> int:
        """Start a fresh Execution. Holds the single-instance lock throughout."""
        try:
            with self.store.locked():
                return self._guarded(self._run_fresh)
        except LockHeld as exc:
            self.log(f"spar exec: another instance holds the lock ({exc}).")
            return 3

    def run_continue(self) -> int:
        """Resume from ``exec.json`` + git reconciliation (§11.1)."""
        try:
            with self.store.locked():
                return self._guarded(self._run_continue)
        except LockHeld as exc:
            self.log(f"spar exec: another instance holds the lock ({exc}).")
            return 3

    def _guarded(self, fn: Callable[[], int]) -> int:
        try:
            return fn()
        except _Abort as exc:
            return exc.code
        except ReviewAbort as exc:
            self.log(f"spar exec: review aborted: {exc}")
            return 4
        except AdapterError as exc:
            self.log(f"spar exec: adapter failed, aborting: {exc}")
            return 4
        except GitError as exc:
            self.log(f"spar exec: git operation failed, aborting: {exc}")
            return 4

    # -- fresh run -----------------------------------------------------

    def _run_fresh(self) -> int:
        # §5 collision policy: a fresh run always means these artifacts are
        # orphans (a matching-state resume goes through ``run_continue``), so
        # refuse rather than clobber a leftover integration/task branch or a
        # stale worktree directory from a prior run whose state was removed.
        leftovers = self._detect_leftovers()
        if leftovers:
            self.log(
                "spar exec: refusing to start — leftover execution artifacts "
                "exist without matching .spar/ state: "
                + ", ".join(leftovers)
                + ". Clean them up (or run 'spar exec --continue' if this is a "
                "resumable run) before starting a fresh exec."
            )
            return 3

        target_branch = gitops.current_branch(self.repo)
        target_base_oid = gitops.rev_parse(self.repo, "HEAD")
        if not gitops.is_clean(self.repo):
            self.log(
                f"spar exec: target worktree '{target_branch}' is not clean; "
                "commit or stash before running exec."
            )
            return 3

        state = ExecState(
            phase="execution",
            target_branch=target_branch,
            target_base_oid=target_base_oid,
            integration_branch="spar/integration",
            tasks={t.id: TaskState(task=t, status="pending") for t in self.tasks},
        )
        gitops.create_branch(self.repo, state.integration_branch, target_base_oid)
        self.store.save(state)
        return self._drive(state)

    # -- resume --------------------------------------------------------

    def _run_continue(self) -> int:
        try:
            state = self.store.load()
        except StateError as exc:
            self.log(f"spar exec: cannot load execution state: {exc}")
            return 3

        if state.phase == "done":
            self.log("spar exec: nothing to resume — execution already done.")
            return 0

        self._reconcile(state)
        self.store.save(state)
        return self._drive(state)

    def _reconcile(self, state: ExecState) -> None:
        """Reconcile recorded FSM state against actual git (§11.1)."""
        integration = state.integration_branch
        for ts in sorted(state.tasks.values(), key=lambda t: t.task.id):
            branch = ts.branch or self._task_branch(ts.task)
            if ts.status == "merged":
                if self._branch_exists(branch):
                    self._reset_task_artifacts(branch, self._worktree_for(ts.task.side))
                    self.log(f"spar exec: recovery — deleted lingering branch {branch}.")
                continue
            if ts.status in ("implementing", "review", "testing"):
                # A merged task branch is a STRICT ancestor of integration: it
                # must be genuinely BEHIND the integration tip (integration
                # contains the task's no-ff merge commit the branch lacks).
                # ``git merge-base --is-ancestor`` also returns true when the
                # two commits are IDENTICAL — a freshly created task branch
                # points at the integration tip with zero commits — so an equal
                # tip must NOT be read as merged (it is an interrupted first
                # implementer turn whose work was never done). Require tip
                # inequality to distinguish the two.
                if (
                    self._branch_exists(branch)
                    and gitops.is_ancestor(self.repo, branch, integration)
                    and gitops.rev_parse(self.repo, branch)
                    != gitops.rev_parse(self.repo, integration)
                ):
                    # crash after merge, before state save: the merge happened.
                    self._reset_task_artifacts(branch, self._worktree_for(ts.task.side))
                    ts.status = "merged"
                    self.log(
                        f"spar exec: recovery — {ts.task.id} already merged into "
                        "integration; marking merged."
                    )
                else:
                    # mid-turn / mid-test: restart the task from a clean slate.
                    self._reset_task_artifacts(branch, self._worktree_for(ts.task.side))
                    ts.status = "pending"
                    ts.branch = None
                    self.log(
                        f"spar exec: recovery — restarting interrupted task "
                        f"{ts.task.id}."
                    )
                continue
            # ``ready``/``pending``: a crash in the branch-created window (before
            # the first ``implementing`` save) can leave a stale branch/worktree.
            # Sweep it so the upcoming (re)start never hard-fails on the leftover.
            if self._branch_exists(branch) or self._worktree_for(ts.task.side).exists():
                self._reset_task_artifacts(branch, self._worktree_for(ts.task.side))
                ts.branch = None
                self.log(
                    f"spar exec: recovery — swept stale branch/worktree for "
                    f"{ts.task.id} before restart."
                )
        state.turn_in_progress = None

    # -- the FSM driver ------------------------------------------------

    def _drive(self, state: ExecState) -> int:
        while True:
            state.mark_ready()
            self.store.save(state)
            ts = state.next_task()
            if ts is not None:
                self._run_task(state, ts)
                continue
            if state.all_merged():
                code = self._test_and_merge(state)
                if code is not None:
                    return code
                continue
            self.log("spar exec: no runnable task and not all merged; aborting.")
            return 4

    # -- a single task through implement → review → test → merge -------

    def _run_task(self, state: ExecState, ts: TaskState) -> None:
        task = ts.task
        branch = self._task_branch(task)
        worktree = self._worktree_for(task.side)
        reviewer = self._other_side(task.side)

        # Record intent BEFORE creating any git artifact (§11.1): persist the
        # branch name and ``implementing`` status first, so a crash between the
        # branch/worktree creation and the first in-loop save is recoverable
        # (recovery restarts an ``implementing`` task from a clean slate).
        ts.branch = branch
        ts.status = "implementing"
        self.store.save(state)

        # Idempotent create: sweep any stale branch/worktree left by a crashed
        # prior attempt so creating the task branch never hard-fails on resume.
        self._reset_task_artifacts(branch, worktree)
        gitops.create_branch(self.repo, branch, state.integration_branch)
        gitops.add_worktree(self.repo, worktree, branch)
        self.log(f"spar exec: [{task.id}] branch {branch} + worktree {worktree}.")

        # Failure context threaded into the NEXT implement turn on a test-fail
        # re-entry (§6): without it the re-implement prompt would be identical
        # and the testing→implementing loop could never converge.
        test_warning: str | None = None
        try:
            while True:
                ts.status = "implementing"
                self.store.save(state)
                impl_adapter = self.make_adapter(task.side, worktree, task.model)
                review_adapter = self.make_adapter(reviewer, self.repo, task.review_model)

                # Initial code-creating implement turn BEFORE the first reviewer
                # turn (the reviewer must have a non-empty diff to read).
                _implementer_turn(
                    task_state=ts,
                    impl_adapter=impl_adapter,
                    worktree=worktree,
                    plan_path=self.plan_path,
                    exec_state=state,
                    store=self.store,
                    log=self.log,
                    timeout_sec=_DEFAULT_TIMEOUT_SEC,
                    warning=test_warning,
                )

                # Empty-implementation guard (§6/§8): if the initial turn created
                # nothing on the task branch, review/test would run on an empty
                # diff — the reviewer can emit DONE on nothing and the per-Task
                # test then fails, spinning forever. Retry the turn ONCE with a
                # stern warning; if it is STILL empty, abort this task loudly at
                # the source rather than proceeding to review/test on nothing.
                if self._task_branch_empty(branch, worktree, state.integration_branch):
                    self.log(
                        f"spar exec: [{task.id}] initial implement produced no files; "
                        "retrying the turn once with a warning."
                    )
                    _implementer_turn(
                        task_state=ts,
                        impl_adapter=impl_adapter,
                        worktree=worktree,
                        plan_path=self.plan_path,
                        exec_state=state,
                        store=self.store,
                        log=self.log,
                        timeout_sec=_DEFAULT_TIMEOUT_SEC,
                        warning=(
                            "Your previous turn created NO files on disk. You MUST "
                            "create/edit the file(s) in your scope now, on disk, with real "
                            "content per the plan, using your file-editing tools. Do not "
                            "merely describe the change."
                        ),
                    )
                    if self._task_branch_empty(
                        branch, worktree, state.integration_branch
                    ):
                        raise ReviewAbort(
                            f"task {task.id}: implementer created no files"
                        )

                ts.status = "review"
                self.store.save(state)
                run_cross_review(
                    task_state=ts,
                    impl_adapter=impl_adapter,
                    review_adapter=review_adapter,
                    repo=self.repo,
                    worktree=worktree,
                    integration_base=state.integration_branch,
                    plan_path=self.plan_path,
                    timeout_sec=_DEFAULT_TIMEOUT_SEC,
                    store=self.store,
                    exec_state=state,
                    log=self.log,
                )

                ts.status = "testing"
                self.store.save(state)
                passed, output = self._run_test_capture(
                    self._task_test_cmd(task), worktree
                )
                if passed:
                    break
                test_warning = (
                    "The per-task test command "
                    f"(`{self._task_test_cmd(task)}`) failed. You MUST change the "
                    "implementation so the tests pass. Captured failing output:\n"
                    f"{output}"
                )
                self.log(f"spar exec: [{task.id}] per-task test failed; re-implementing.")
        except BaseException:
            self.store.save(state)
            raise

        # Merge the task branch into integration.
        gitops.checkout(self.repo, state.integration_branch)
        gitops.merge_no_ff(
            self.repo, branch, f"spar: merge {task.id} ({task.side}) into integration"
        )
        # Record completion (§11.1) BEFORE deleting the branch: the merge is
        # only detectable on resume while the branch survives, so ``merged``
        # must be durable first. A crash after this save leaves a lingering
        # branch that recovery cleans up idempotently.
        ts.status = "merged"
        self.store.save(state)
        # Idempotent cleanup, tolerant of an already-removed worktree/branch.
        self._reset_task_artifacts(branch, worktree)
        self.log(f"spar exec: [{task.id}] merged into integration.")

    # -- final Test phase + integration-fix Task + final merge ---------

    def _test_and_merge(self, state: ExecState) -> int | None:
        """Run the final Test phase; open a fix Task on failure (return None to
        loop), else run the user-gated final merge (return its exit code)."""
        state.phase = "test"
        self.store.save(state)
        gitops.checkout(self.repo, state.integration_branch)
        passed, output = self._run_test_capture(self.execution.test_command, self.repo)
        if not passed:
            fix = self._generate_fix_task(state, output)
            state.tasks[fix.id] = TaskState(task=fix, status="pending")
            state.phase = "execution"
            self.store.save(state)
            self.log(
                f"spar exec: final test failed; opened integration-fix task "
                f"{fix.id} (side={fix.side}, files={list(fix.files)})."
            )
            return None
        return self._final_merge(state)

    def _generate_fix_task(self, state: ExecState, failing_output: str) -> Task:
        """Build an integration-fix Task per spec §7."""
        next_num = 1 + max(
            (int(m.group(1)) for tid in state.tasks if (m := re.match(r"t(\d+)$", tid))),
            default=0,
        )
        fix_id = f"t{next_num}"

        integrated_files = gitops.changed_files(
            self.repo, state.target_base_oid, state.integration_branch
        )
        failing_files = self._failing_files(failing_output, integrated_files)
        scope = failing_files or integrated_files or ("**",)

        # Dominating task = implementer of the task owning the most failing
        # files; ties → first side in order.
        dominating = self._dominating_task(state, failing_files)
        fix_side = dominating.side if dominating is not None else self.order[0]
        other = self._other_side(fix_side)

        if dominating is not None and dominating.side == fix_side:
            model = dominating.model
            review = dominating.review_model
        else:
            model = self.sides[fix_side].default_model
            review = self.sides[other].default_model

        description = (
            f"make `{self.execution.test_command}` pass on the integrated branch.\n\n"
            f"Captured failing output:\n{failing_output}"
        )
        return Task(
            id=fix_id,
            description=description,
            side=fix_side,
            model=model,
            review_model=review,
            deps=(),
            files=tuple(scope),
            test=None,
        )

    def _failing_files(
        self, output: str, integrated_files: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Best-effort: which integrated files are named in the failing output."""
        hits = [f for f in integrated_files if f and f in output]
        return tuple(hits)

    def _dominating_task(
        self, state: ExecState, failing_files: tuple[str, ...]
    ) -> Task | None:
        """The task owning the most failing files (ties → first side in order)."""
        if not failing_files:
            return None
        best: Task | None = None
        best_count = 0
        for ts in state.tasks.values():
            count = sum(1 for f in failing_files if f in ts.task.files)
            if count > best_count or (
                count == best_count
                and count > 0
                and best is not None
                and self.order.index(ts.task.side) < self.order.index(best.side)
            ):
                best, best_count = ts.task, count
        return best

    def _final_merge(self, state: ExecState) -> int:
        integration = state.integration_branch
        target = state.target_branch

        # Interrupted-final-merge recovery: integration already in target → done.
        if self._branch_exists(target) and gitops.is_ancestor(
            self.repo, integration, target
        ):
            state.phase = "done"
            self.store.save(state)
            self.log("spar exec: integration already merged into target; done.")
            return 0

        summary = self._merge_summary(state)
        if not self.auto_integration_merge:
            decision = self.gate.final_merge_gate(summary)
            if decision.action == "abort":
                self.store.save(state)
                self.log("spar exec: aborted by user at the final-merge gate.")
                return 5
            if decision.action != "accept":
                self.store.save(state)
                self.log(
                    f"spar exec: unexpected final-merge gate action "
                    f"{decision.action!r}; aborting."
                )
                return 4
        else:
            self.log(summary)

        current_target_oid = gitops.rev_parse(self.repo, target)
        gitops.checkout(self.repo, target)
        merge_msg = f"spar: merge integration into {target}"
        if current_target_oid == state.target_base_oid:
            gitops.merge_no_ff(self.repo, integration, merge_msg)
        else:
            # Target advanced (§9): merge integration onto the new tip, re-run
            # the final test; surface a conflict at the gate.
            self.log(
                f"spar exec: target '{target}' advanced during execution; "
                "merging integration onto the new tip and re-testing."
            )
            try:
                gitops.merge_no_ff(self.repo, integration, merge_msg)
            except GitError as exc:
                return self._surface_merge_conflict(state, exc)
            passed, _ = self._run_test_capture(self.execution.test_command, self.repo)
            if not passed:
                self.log(
                    "spar exec: final test failed after merging onto the advanced "
                    "target; surfacing at the gate."
                )
                return self._surface_merge_conflict(
                    state, GitError("final test failed on the merged result")
                )

        state.phase = "done"
        self.store.save(state)
        self.log(f"spar exec: merged integration into {target}; execution done.")
        return 0

    def _surface_merge_conflict(self, state: ExecState, exc: Exception) -> int:
        """A target-moved merge produced a conflict / test failure: let the user
        accept a manual resolution (proceed) or abort."""
        summary = (
            f"spar exec: merging integration onto the advanced target failed: {exc}\n"
            "Resolve manually, then accept; or abort."
        )
        if self.auto_integration_merge:
            self.log(summary + " (--auto-integration-merge → aborting)")
            # Leave the working tree clean rather than stuck mid-merge.
            gitops.merge_abort(self.repo)
            self.store.save(state)
            return 4
        decision = self.gate.final_merge_gate(summary)
        if decision.action == "accept":
            state.phase = "done"
            self.store.save(state)
            self.log("spar exec: user accepted the manual merge resolution; done.")
            return 0
        # Abort: undo the conflicting merge so the repo is not left mid-merge.
        gitops.merge_abort(self.repo)
        self.store.save(state)
        self.log("spar exec: aborted by user after a merge conflict.")
        return 5

    def _merge_summary(self, state: ExecState) -> str:
        merged = [tid for tid, ts in state.tasks.items() if ts.status == "merged"]
        diffstat = self._diffstat(state.target_base_oid, state.integration_branch)
        return (
            "spar exec: final Test passed. Ready to merge integration into "
            f"'{state.target_branch}'.\n"
            f"  tasks merged: {', '.join(sorted(merged))}\n"
            f"  diff --stat {state.target_branch}..integration:\n{diffstat}"
        )

    # -- test-command execution ----------------------------------------

    def _task_test_cmd(self, task: Task) -> str:
        return task.test if task.test else self.execution.test_command

    def _run_test_capture(self, cmd: str, cwd: Path) -> tuple[bool, str]:
        """Run ``cmd`` (shell) in ``cwd``; empty command is a pass."""
        if not cmd:
            return True, ""
        result = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output

    # -- small git / path helpers --------------------------------------

    def _task_branch_empty(
        self, branch: str, worktree: Path, integration_base: str
    ) -> bool:
        """True iff the task branch carries no change vs its integration base.

        An empty implementation is one that committed nothing onto ``branch``
        (no diff against ``integration_base``) AND left no uncommitted work in
        the worktree — i.e. the implementer created no files at all.
        """
        committed = gitops.changed_files(self.repo, integration_base, branch)
        return not committed and gitops.is_clean(worktree)

    def _task_branch(self, task: Task) -> str:
        return f"spar/{task.id}-{task.side}"

    def _worktree_for(self, side: str) -> Path:
        return self.spar_dir / "worktrees" / side

    def _other_side(self, side: str) -> str:
        others = [s for s in self.order if s != side]
        return others[0]

    def _detect_leftovers(self) -> list[str]:
        """Names of orphaned §5 artifacts a fresh run must refuse to clobber.

        Reports a leftover ``spar/integration`` branch, any ``spar/t*`` task
        branch, and any child of ``<spar_dir>/worktrees`` — the artifacts a
        prior run creates. On a fresh run their presence always signals an
        orphan (a matching-state resume never reaches ``_run_fresh``).
        """
        found: list[str] = []
        result = subprocess.run(
            ["git", "-C", str(self.repo), "for-each-ref",
             "--format=%(refname:short)", "refs/heads/spar/"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.splitlines():
            name = line.strip()
            if not name:
                continue
            if name == "spar/integration" or re.match(r"spar/t\d+", name):
                found.append(f"branch {name}")

        worktrees_dir = self.spar_dir / "worktrees"
        if worktrees_dir.exists():
            for child in sorted(worktrees_dir.iterdir()):
                found.append(f"worktree {child}")
        return found

    def _branch_exists(self, name: str) -> bool:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet",
             f"refs/heads/{name}"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _force_remove_worktree(self, path: Path) -> None:
        """Best-effort worktree removal (used only during recovery)."""
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "remove", "--force", str(path)],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "prune"],
            capture_output=True,
            text=True,
        )

    def _reset_task_artifacts(self, branch: str, worktree: Path) -> None:
        """Idempotently remove a task's worktree + branch.

        Tolerant of an already-removed worktree or branch (best-effort), so it
        is safe both as post-merge cleanup and as a pre-create sweep of stale
        artifacts left by a crashed prior attempt. A stray worktree directory
        that git no longer tracks is removed directly so ``worktree add`` (which
        refuses a non-empty path) cannot hard-fail on resume.
        """
        self._force_remove_worktree(worktree)
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
        if self._branch_exists(branch):
            gitops.delete_branch(self.repo, branch)

    def _diffstat(self, base: str, ref: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.repo), "diff", "--stat", f"{base}..{ref}"],
            capture_output=True,
            text=True,
        )
        return result.stdout.rstrip("\n")
