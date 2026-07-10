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

Exit codes mirror v1: 0 ok, 2 preflight/validation refusal (a fresh start
whose per-task test command names a tool missing on this machine), 3
lock/state guard, 4 protocol/adapter abort, 5 user abort at the
final-merge gate.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Callable, Protocol

from spar.adapters.base import Adapter, AdapterError
from spar.config import ExecutionConfig, SideConfig
from spar.exec import gitops
from spar.exec.gitops import GitError
from spar.exec.preflight import preflight_test_commands
from spar.exec.review import ReviewAbort, _implementer_turn, run_cross_review
from spar.exec.state import ExecState, ExecStateStore, TaskState
from spar.exec.tasklist import Task
from spar.gates import GateChoice, GateParseError, GatePending, validate_choice
from spar.orchestrator import GateDecision
from spar.state import LockHeld, StateError, StateRemark
from spar.stream import StreamSink
from spar.verdict import Severity

__all__ = ["Executor", "ExecGate", "ConsoleExecGate"]

# Anti-spin for the per-task TEST loop: after this many CONSECUTIVE failing
# test iterations that change NOTHING on the task branch (neither the
# re-implement turn nor the ensuing review), the loop is not converging —
# escalate to the user rounds-gate instead of re-implementing forever.
_TEST_FAIL_STALL_ITERS = 2


def _task_id_order(tid: str) -> tuple:
    """Numeric sort key for a task id: ``t2`` before ``t10`` (not lexicographic).

    Numeric ``t<N>`` ids sort first, ordered by ``N``; any other id sorts
    after, ordered by its raw string. Shared by every place that must present
    tasks in a stable, human-sensible order (foreign-file listings in the
    review prompt, the final-merge NICE-remarks summary).
    """
    m = re.match(r"t(\d+)$", tid)
    return (0, int(m.group(1)), "") if m else (1, 0, tid)


# ---------------------------------------------------------------------------
# Final-merge gate (mirrors v1's ConsoleGate; reuses GateDecision)
# ---------------------------------------------------------------------------


class ExecGate(Protocol):
    """The user decision points of Execution: the final merge (§9) and a
    review that exhausted its round budget without converging."""

    def final_merge_gate(self, summary: str) -> GateDecision: ...

    def review_rounds_exhausted_gate(
        self,
        task_id: str,
        rounds: int,
        pending: list,
        *,
        allow_fix: bool = False,
        command: str | None = None,
    ) -> GateDecision: ...


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

    def review_rounds_exhausted_gate(
        self,
        task_id: str,
        rounds: int,
        pending: list,
        *,
        allow_fix: bool = False,
        command: str | None = None,
    ) -> GateDecision:
        self._print(
            f"\nTask {task_id}: review-round budget exhausted after {rounds} "
            "round(s) without reviewer DONE."
        )
        if pending:
            self._print("Still-open remarks:")
            for r in pending:
                self._print(f"  #{r.remark_id} [{r.severity.name}] ({r.author}): {r.text}")
        # ``fix`` is offered only for a test escalation (``allow_fix``): the
        # user can replace a broken/wrong per-task test command outright.
        prompt = (
            "[a]ccept task as-is / [e]xtend / [f]ix test command / [x] abort? "
            if allow_fix
            else "[a]ccept task as-is / [e]xtend / [x] abort? "
        )
        hint = "'a', 'e', 'f' or 'x'" if allow_fix else "'a', 'e' or 'x'"
        while True:
            choice = self._input(prompt).strip().lower()
            if choice in ("a", "accept"):
                return GateDecision(action="accept")
            if choice in ("x", "abort"):
                return GateDecision(action="abort")
            if choice in ("e", "extend"):
                raw = self._input("How many more rounds? ").strip()
                try:
                    extra = int(raw)
                except ValueError:
                    extra = 0
                if extra > 0:
                    return GateDecision(action="extend", extra_rounds=extra)
                self._print("Please enter a positive number.")
                continue
            if allow_fix and choice in ("f", "fix"):
                if command:
                    self._print(f"Current test command: {command}")
                new_cmd = self._input("New test command: ").strip()
                if new_cmd:
                    return GateDecision(action="fix", command=new_cmd)
                self._print("Please enter a non-empty command.")
                continue
            self._print(f"Please answer with {hint}.")


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
        # ``(side, worktree, model, readonly=False) -> Adapter``: builds the
        # Adapter for one turn. ``model`` is the negotiated per-Task model to
        # run (the implementer's ``task.model`` or the reviewer's
        # ``task.review_model``) so the Assignment negotiated for the Task
        # actually drives which model executes the turn, rather than whatever
        # default the factory would otherwise pick. ``readonly=True`` is passed
        # for the reviewer role: the built adapter must not be able to write.
        make_adapter: Callable[..., Adapter],
        sides: dict[str, SideConfig],
        order: list[str],
        plan_path: Path,
        tasks: tuple[Task, ...],
        execution: ExecutionConfig,
        gate: ExecGate,
        store: ExecStateStore,
        log=print,
        auto_integration_merge: bool = False,
        sink: StreamSink | None = None,
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
        # Resolve to an absolute path: implementer prompts embed this path, and
        # the implementer's cwd is its task WORKTREE (not the repo), where a
        # relative ".spar/artifact.md" points nowhere.
        self.plan_path = Path(plan_path).resolve()
        self.tasks = tuple(tasks)
        self.execution = execution
        self.gate = gate
        self.store = store
        self.sink = sink
        # ``sink`` present -> route spar's own log lines through it (stdout +
        # .spar/live.log); otherwise keep the caller's ``log`` (tests pass
        # ``log=`` directly with no sink).
        self.log = sink.log if sink is not None else log
        self.auto_integration_merge = auto_integration_merge
        # The live state object under mutation, so ``_guarded`` can persist a
        # pending gate onto it before returning 10. Set at the top of a run.
        self._state: ExecState | None = None
        # ``--gate`` decision threaded into a resume, and the review-rounds
        # decision the Executor owns at its consumption point (single-owner).
        self._gate_choice: GateChoice | None = None
        self._resume_decision: GateChoice | None = None

    # -- public entry points -------------------------------------------

    def run(self) -> int:
        """Start a fresh Execution. Holds the single-instance lock throughout."""
        try:
            with self.store.locked():
                try:
                    return self._guarded(self._run_fresh)
                finally:
                    self._restore_target_checkout()
        except LockHeld as exc:
            self.log(f"spar exec: another instance holds the lock ({exc}).")
            return 3
        except KeyboardInterrupt:
            # State was persisted by the in-flight save points (every turn is
            # bracketed by store.save); the lock was released by locked().
            self.log(
                "spar exec: interrupted — state saved; resume with "
                "'spar exec --continue'."
            )
            return 130

    def run_continue(self, gate_choice: GateChoice | None = None) -> int:
        """Resume from ``exec.json`` + git reconciliation (§11.1).

        ``gate_choice`` (the parsed ``--gate`` value, headless mode) is applied
        to the persisted pending gate. It is validated PURELY against the
        pending-gate record before any side effect: a mismatch returns 2 with
        state untouched (see ``_run_continue``).
        """
        self._gate_choice = gate_choice
        self._resume_decision = None
        try:
            with self.store.locked():
                try:
                    return self._guarded(self._run_continue)
                finally:
                    self._restore_target_checkout()
        except LockHeld as exc:
            self.log(f"spar exec: another instance holds the lock ({exc}).")
            return 3
        except KeyboardInterrupt:
            self.log(
                "spar exec: interrupted — state saved; resume with "
                "'spar exec --continue'."
            )
            return 130

    def _restore_target_checkout(self) -> None:
        """Best-effort: leave the user's repo on the target branch.

        The final Test phase (and each task merge) checks out the integration
        branch in the user's repo; an abort or error can otherwise strand the
        checkout there. Only acts when it is unambiguously safe: state loads,
        the current branch IS the integration branch, no merge is in progress,
        and the tree is clean. Never raises — the real exit code always wins.
        """
        try:
            state = self.store.load()
        except StateError:
            # No/unreadable state is the NORMAL case on every pre-state exit
            # path (e.g. a refused fresh run): nothing to restore, stay silent.
            return
        try:
            if (
                gitops.current_branch(self.repo) == state.integration_branch
                and not gitops.merge_in_progress(self.repo)
                and gitops.is_clean(self.repo)
                and self._branch_exists(state.target_branch)
            ):
                gitops.checkout(self.repo, state.target_branch)
                self.log(
                    f"spar exec: restored checkout to '{state.target_branch}'."
                )
        except Exception as exc:
            self.log(f"spar exec: could not restore target checkout: {exc}")

    def _guarded(self, fn: Callable[[], int]) -> int:
        try:
            return fn()
        except GatePending as exc:
            # A user gate was reached in headless mode: persist the pending gate
            # onto the live state (cleared only at consumption, never here) and
            # exit 10 so the operator can resume with ``--gate``. Re-raising an
            # already-pending gate (resume without ``--gate``) is idempotent:
            # ``to_state()`` reconstructs the same record.
            self._state.pending_gate = exc.to_state()
            self.store.save(self._state)
            self.log(
                f"spar exec: gate '{exc.name}' pending (options: "
                f"{', '.join(exc.options)}); resume with 'spar exec --continue "
                "--gate <choice>'. (exit 10)"
            )
            return 10
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

    # Markers delimiting the block spar manages inside ``.git/info/exclude``.
    # Anything outside the markers (pre-existing user content) is preserved
    # verbatim; the block itself is replaced (not duplicated) on every call.
    _SCOPE_IGNORE_BEGIN = "# >>> spar scope_ignore (managed)"
    _SCOPE_IGNORE_END = "# <<< spar scope_ignore"

    def _apply_scope_ignore(self) -> None:
        """Write ``execution.scope_ignore`` patterns into the repo's LOCAL git
        exclude file (``<git-common-dir>/info/exclude``).

        Once excluded, ``git status --porcelain`` stops reporting matching
        paths, so both the scope guard (which reads worktree status) and the
        turn-commit (``git add -A``) naturally skip them — no scope-guard
        logic changes needed. No-op when ``scope_ignore`` is empty. Idempotent:
        a previously written managed block is replaced, not duplicated, and any
        other content in the file is preserved.
        """
        patterns = self.execution.scope_ignore
        if not patterns:
            return

        git_dir = gitops.git_common_dir(self.repo)
        exclude_path = git_dir / "info" / "exclude"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)

        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        lines = existing.splitlines()

        kept: list[str] = []
        in_block = False
        for line in lines:
            stripped = line.strip()
            if stripped == self._SCOPE_IGNORE_BEGIN:
                in_block = True
                continue
            if stripped == self._SCOPE_IGNORE_END:
                in_block = False
                continue
            if in_block:
                continue
            kept.append(line)

        while kept and kept[-1] == "":
            kept.pop()

        block = [self._SCOPE_IGNORE_BEGIN, *patterns, self._SCOPE_IGNORE_END]
        new_lines = (kept + [""] + block) if kept else block
        exclude_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    def _run_fresh(self) -> int:
        # Preflight (fresh start only): refuse BEFORE touching any git state
        # when a task's test command names a tool missing on this machine
        # (live incident: a plan wrote ``python`` on a python3-only host and
        # the 127 only surfaced deep into the run). Resume is exempt — a
        # command corrected mid-run via ``fix:`` is already persisted, and the
        # 126/127 gate covers anything still broken.
        problems = preflight_test_commands(self.tasks)
        if problems:
            self.log(
                "spar exec: preflight failed — per-task test command(s) name "
                "tools missing on this machine:"
            )
            for problem in problems:
                self.log(f"  {problem}")
            self.log(
                "Fix the plan's test commands (or install the tools) and "
                "rerun. Nothing was started."
            )
            return 2

        self._apply_scope_ignore()
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
        self._state = state
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
        self._state = state
        self._apply_scope_ignore()

        if state.phase == "done":
            self.log("spar exec: nothing to resume — execution already done.")
            return 0

        gate_choice = self._gate_choice
        # (2) Validate the --gate decision PURELY against the persisted pending
        # gate, BEFORE any side effect (reconcile, git, preload). A mismatch —
        # or a --gate with no gate pending — returns 2 with state untouched.
        if gate_choice is not None:
            try:
                validate_choice(gate_choice, state.pending_gate)
            except GateParseError as exc:
                self.log(f"spar exec: --gate rejected: {exc}")
                return 2

        # (3) Reconcile against git with the pending gate STILL SET (a review
        # task named by a pending review_rounds gate is preserved, not restarted).
        self._reconcile(state)
        self.store.save(state)

        # (4) Stash the decision for consumption. ``final_merge`` is consumed by
        # the gate object (preload + on_consume clears pending_gate at that
        # point). ``review_rounds`` is owned by the Executor: it is consumed in
        # ``_resume_review_task`` via ``self._resume_decision`` (single-owner).
        # ``pending_gate`` is NOT cleared here.
        if gate_choice is not None:
            name = (state.pending_gate or {}).get("name")
            if name == "final_merge":
                self.gate.preloaded = (
                    "final_merge",
                    GateDecision(action=gate_choice.action),
                )
                self.gate.on_consume = self._make_gate_on_consume(state)
            elif name == "review_rounds":
                self._resume_decision = gate_choice

        return self._drive(state)

    def _make_gate_on_consume(self, state: ExecState):
        """Callback the headless gate runs the moment it consumes a preloaded
        decision: clear the pending gate and persist (clearing at consumption)."""

        def _on_consume() -> None:
            state.pending_gate = None
            self.store.save(state)

        return _on_consume

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
            # A task left in ``review`` by a pending ``review_rounds`` gate is
            # NOT restarted: its branch/worktree and open ledger must survive so
            # the resume can apply the operator's decision. (Crash-safety: the
            # gate record survives until consumption, so a crash anywhere before
            # the decision is applied reconciles identically — the operator
            # re-issues the same ``--gate``.)
            pg = state.pending_gate
            if (
                ts.status == "review"
                and pg is not None
                and pg.get("name") == "review_rounds"
                and (pg.get("context") or {}).get("task_id") == ts.task.id
            ):
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
            # A task left in ``review`` is only reachable via the pending-gate
            # resume path (a normal reconcile restarts an interrupted review).
            # Consume the operator's review-rounds decision before scheduling.
            pending_review = next(
                (
                    ts
                    for ts in sorted(
                        state.tasks.values(),
                        key=lambda t: _task_id_order(t.task.id),
                    )
                    if ts.status == "review"
                ),
                None,
            )
            if pending_review is not None:
                self._resume_review_task(state, pending_review)
                continue
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

    def _event_callbacks(self, task: Task):
        """Build the per-task (impl-prefixed, review-prefixed) ``on_event``
        callbacks the sink fans adapter turn lines out through, or
        ``(None, None)`` when no sink was configured."""
        if self.sink is None:
            return None, None
        side = task.side
        # The reviewer is always the OTHER side — the prefix must name who is
        # actually speaking, not the task's owner (live-smoke finding: opus
        # review turns were mislabeled with the implementer's side).
        reviewer = self._other_side(side)
        on_event_impl = lambda ln: self.sink.event(f"{side} {task.id} impl", ln)
        on_event_review = lambda ln: self.sink.event(f"{reviewer} {task.id} review", ln)
        return on_event_impl, on_event_review

    def _run_task(self, state: ExecState, ts: TaskState) -> None:
        task = ts.task
        branch = self._task_branch(task)
        worktree = self._worktree_for(task.side)
        reviewer = self._other_side(task.side)
        on_event_impl, on_event_review = self._event_callbacks(task)

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

        try:
            # Initial code-creating implement turn BEFORE the first reviewer
            # turn (the reviewer must have a non-empty diff to read).
            ts.status = "implementing"
            self.store.save(state)
            impl_adapter = self.make_adapter(task.side, worktree, task.model)
            # The reviewer only reads the diff embedded in its prompt; it must
            # never be able to write to the main repo checkout.
            review_adapter = self.make_adapter(
                reviewer, self.repo, task.review_model, readonly=True
            )
            _implementer_turn(
                task_state=ts,
                impl_adapter=impl_adapter,
                worktree=worktree,
                plan_path=self.plan_path,
                exec_state=state,
                store=self.store,
                log=self.log,
                timeout_sec=self.execution.turn_timeout_sec,
                warning=None,
                on_event=on_event_impl,
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
                    timeout_sec=self.execution.turn_timeout_sec,
                    warning=(
                        "Your previous turn created NO files on disk. You MUST "
                        "create/edit the file(s) in your scope now, on disk, with real "
                        "content per the plan, using your file-editing tools. Do not "
                        "merely describe the change."
                    ),
                    on_event=on_event_impl,
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
                timeout_sec=self.execution.turn_timeout_sec,
                store=self.store,
                exec_state=state,
                log=self.log,
                max_rounds=self.execution.max_review_rounds,
                rounds_gate=self._review_rounds_gate(state),
                # Review context (A2). Foreign files: file scopes of other,
                # not-yet-merged tasks — legitimately absent on the task branch.
                # Merged files: ACTUAL paths already merged into integration —
                # present on the branch though invisible in the reviewer's diff.
                foreign_files=self._foreign_files(state, task),
                merged_files=gitops.present_files(
                    self.repo, state.target_base_oid, state.integration_branch
                ),
                on_event_impl=on_event_impl,
                on_event_review=on_event_review,
            )

            # Per-task test → merge, shared with the review-resume path.
            self._test_and_merge_task(state, ts, branch, worktree)
        except BaseException:
            self.store.save(state)
            raise

    def _foreign_files(
        self, state: ExecState, task: Task
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """File scopes of other, not-yet-merged tasks (A2 review context).

        Sequential execution: statuses cannot change while a task runs, so this
        is computed fresh at each review entry rather than cached.
        """
        return tuple(
            (other.task.id, other.task.files)
            for other in sorted(
                state.tasks.values(), key=lambda ts_: _task_id_order(ts_.task.id)
            )
            if other.task.id != task.id and other.status != "merged"
        )

    def _test_and_merge_task(
        self, state: ExecState, ts: TaskState, branch: str, worktree: Path
    ) -> None:
        """Per-task test loop → merge into integration → cleanup.

        On a per-task test failure the captured output is threaded back into a
        fresh implement+review cycle (§6) until the test passes, then the branch
        is merged no-ff into integration and its artifacts are swept. Shared by
        the normal task path (:meth:`_run_task`) and the review-resume path
        (:meth:`_resume_review_task`) so the two cannot drift.
        """
        task = ts.task
        reviewer = self._other_side(task.side)
        on_event_impl, on_event_review = self._event_callbacks(task)
        # Anti-spin for the per-task TEST loop (mirrors run_cross_review's
        # no-change guard). A failing test re-triggers implement→review, but if
        # neither the re-implement turn NOR the ensuing review changes anything
        # on the task branch, the loop makes no progress and would re-implement
        # forever (live bug: a test command naming a missing interpreter fails
        # 127 while the implementer keeps replying "Unchanged"). After
        # ``_TEST_FAIL_STALL_ITERS`` consecutive no-change failing iterations,
        # escalate to the SAME user rounds-gate that review disputes use.
        no_change_iters = 0
        stall_budget = _TEST_FAIL_STALL_ITERS
        while True:
            ts.status = "testing"
            self.store.save(state)
            cmd = self._task_test_cmd(task)
            rc, output = self._run_test_rc(cmd, worktree)
            if rc == 0:
                break

            # Broken test COMMAND (126 not executable / 127 command not found):
            # the command itself is wrong, not the implementation — no amount of
            # re-implementing can fix it (live incident: a plan naming ``python``
            # on a ``python3``-only host looped 127 forever). Do NOT enter the
            # re-implement loop; escalate IMMEDIATELY to the SAME user gate as a
            # review/test dispute so the operator can supply a corrected command
            # (``fix:``), override (``accept``), grant re-implement rounds anyway
            # (``extend``), or abort.
            if rc in (126, 127):
                reason = (
                    "command not found" if rc == 127 else "command not executable"
                )
                message = (
                    f"test command failed with exit {rc} ({reason}): {cmd}\n\n"
                    f"{output}"
                )
                self.log(
                    f"spar exec: [{task.id}] test command failed with exit {rc} "
                    f"({reason}): {cmd} — escalating to the user gate without "
                    "burning an implementer turn."
                )
                # Mark ``review`` so a headless pend reconciles/resumes through
                # the review-resume machinery (the task WAS already reviewed in
                # ``_run_task`` before this test).
                ts.status = "review"
                self.store.save(state)
                decision = self._review_rounds_gate(state, test_output=message)(ts, 0)
                if decision.action == "fix":
                    task = self._apply_fix_command(state, ts, decision.command)
                    no_change_iters = 0
                    stall_budget = _TEST_FAIL_STALL_ITERS
                    continue
                if decision.action == "accept":
                    self.log(
                        f"spar exec: [{task.id}] user ACCEPTED the task with a "
                        "FAILING per-task test (broken command); merging on the "
                        "user's override."
                    )
                    break
                # extend: the user chose to spend implementer rounds anyway
                # (e.g. the code is expected to create the missing command).
                stall_budget = decision.extra_rounds
                no_change_iters = 0
                self.log(
                    f"spar exec: [{task.id}] granting {decision.extra_rounds} "
                    "re-implement round(s) on a broken test command per the user."
                )

            self.log(
                f"spar exec: [{task.id}] per-task test failed; re-implementing."
            )
            warning = (
                "The per-task test command "
                f"(`{self._task_test_cmd(task)}`) failed. You MUST change the "
                "implementation so the tests pass. Captured failing output:\n"
                f"{output}"
            )
            ts.status = "implementing"
            self.store.save(state)
            impl_adapter = self.make_adapter(task.side, worktree, task.model)
            review_adapter = self.make_adapter(
                reviewer, self.repo, task.review_model, readonly=True
            )
            branch_before = gitops.rev_parse(self.repo, branch)
            made_changes, _ = _implementer_turn(
                task_state=ts,
                impl_adapter=impl_adapter,
                worktree=worktree,
                plan_path=self.plan_path,
                exec_state=state,
                store=self.store,
                log=self.log,
                timeout_sec=self.execution.turn_timeout_sec,
                warning=warning,
                on_event=on_event_impl,
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
                timeout_sec=self.execution.turn_timeout_sec,
                store=self.store,
                exec_state=state,
                log=self.log,
                max_rounds=self.execution.max_review_rounds,
                rounds_gate=self._review_rounds_gate(state),
                foreign_files=self._foreign_files(state, task),
                merged_files=gitops.present_files(
                    self.repo, state.target_base_oid, state.integration_branch
                ),
                on_event_impl=on_event_impl,
                on_event_review=on_event_review,
            )

            # Progress check: an iteration that changed the task branch (either
            # via the re-implement turn or a review-loop edit) is progress —
            # reset the stall counter. ``_TEST_FAIL_STALL_ITERS`` consecutive
            # iterations that change NOTHING while the test still fails is a
            # stall: escalate to the user gate rather than loop forever.
            if made_changes or gitops.rev_parse(self.repo, branch) != branch_before:
                no_change_iters = 0
                continue
            no_change_iters += 1
            if no_change_iters < stall_budget:
                continue
            # Stalled. Escalate to the SAME rounds-gate wrapper as review
            # disputes, carrying the failing output so the user sees WHY.
            # ``abort`` is raised as _Abort(5) inside the wrapper; headless
            # pends (GatePending → exit 10) from ``review_rounds_exhausted_gate``.
            decision = self._review_rounds_gate(state, test_output=output)(
                ts, no_change_iters
            )
            if decision.action == "fix":
                # The user realized the test COMMAND itself is wrong: replace it
                # and re-run from the top with a fresh stall budget.
                task = self._apply_fix_command(state, ts, decision.command)
                no_change_iters = 0
                stall_budget = _TEST_FAIL_STALL_ITERS
                continue
            if decision.action == "accept":
                self.log(
                    f"spar exec: [{task.id}] user ACCEPTED the task with a "
                    "FAILING per-task test; merging on the user's override."
                )
                break
            # extend: grant a FRESH budget of N more no-change iterations.
            stall_budget = decision.extra_rounds
            no_change_iters = 0
            self.log(
                f"spar exec: [{task.id}] per-task-test stall extended by "
                f"{decision.extra_rounds} iteration(s) on the user's request."
            )

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

    def _resume_review_task(self, state: ExecState, ts: TaskState) -> None:
        """Executor-owned consumption point for a ``review_rounds`` decision.

        Applies ``self._resume_decision`` to a task left in ``review`` by a
        pending gate: ``accept`` skips review and proceeds to the per-task
        test/merge tail; ``extend`` re-enters the review loop with a FRESH
        budget starting at an implementer turn (``start_with="implementer"``);
        ``abort`` aborts (exit 5). Resumed WITHOUT ``--gate`` (no decision)
        re-pends the same gate (exit 10, idempotent). ``pending_gate`` is
        cleared here — at consumption — never before.
        """
        task = ts.task
        branch = ts.branch or self._task_branch(task)
        worktree = self._worktree_for(task.side)
        reviewer = self._other_side(task.side)
        on_event_impl, on_event_review = self._event_callbacks(task)

        decision = self._resume_decision
        if decision is None:
            # Resumed without --gate while a review gate pends: re-pend it
            # unchanged (branch/worktree/status all preserved). ``_guarded``
            # re-persists the identical record and exits 10.
            record = state.pending_gate or {}
            raise GatePending(
                record.get("name", "review_rounds"),
                record.get("options", ["accept", "extend", "abort"]),
                record.get("context", {}),
            )

        # Re-create the worktree from the surviving branch if it is gone.
        if not worktree.exists():
            gitops.add_worktree(self.repo, worktree, branch)

        if decision.action == "abort":
            state.pending_gate = None
            self.store.save(state)
            self.log(
                f"spar exec: aborted by user at the review-rounds gate "
                f"(task {task.id})."
            )
            raise _Abort(5)

        # accept | extend | fix: clear the gate AT consumption, then proceed.
        state.pending_gate = None
        self.store.save(state)
        self._resume_decision = None

        if decision.action == "fix":
            # The pending gate was a per-task-test escalation and the operator
            # supplied a corrected command: swap it in, then fall through to the
            # per-task test/merge tail, which re-runs the (now fixed) command.
            self._apply_fix_command(state, ts, decision.command)
            self._test_and_merge_task(state, ts, branch, worktree)
            return

        if decision.action == "extend":
            n = decision.extra_rounds
            self.log(
                f"spar exec: [{task.id}] review-rounds extended by {n} round(s) "
                "on resume."
            )
            impl_adapter = self.make_adapter(task.side, worktree, task.model)
            review_adapter = self.make_adapter(
                reviewer, self.repo, task.review_model, readonly=True
            )
            run_cross_review(
                task_state=ts,
                impl_adapter=impl_adapter,
                review_adapter=review_adapter,
                repo=self.repo,
                worktree=worktree,
                integration_base=state.integration_branch,
                plan_path=self.plan_path,
                timeout_sec=self.execution.turn_timeout_sec,
                store=self.store,
                exec_state=state,
                log=self.log,
                max_rounds=n,
                start_with="implementer",
                rounds_gate=self._review_rounds_gate(state),
                foreign_files=self._foreign_files(state, task),
                merged_files=gitops.present_files(
                    self.repo, state.target_base_oid, state.integration_branch
                ),
                on_event_impl=on_event_impl,
                on_event_review=on_event_review,
            )
        else:
            self.log(
                f"spar exec: [{task.id}] review accepted as-is on resume; "
                "proceeding to the per-task test."
            )

        self._test_and_merge_task(state, ts, branch, worktree)

    def _review_rounds_gate(self, state: ExecState, test_output: str | None = None):
        """Adapter between ``run_cross_review``'s ``rounds_gate`` callback and
        the user-facing :class:`ExecGate`. An ``abort`` decision raises the
        user-abort exit (5) directly — the review loop only ever sees
        accept/extend.

        ``test_output`` (set only by the stalled per-task-test escalation) is
        surfaced to the gate as a synthetic USER remark so the console prompt
        and the headless ``review_rounds`` pending-gate context both carry the
        failing output — the user must see WHY the test fails to decide."""

        def gate(task_state: TaskState, rounds: int) -> GateDecision:
            # A DISPUTE escalation arrives with the contested remarks already
            # moved to resolved (rejected) — surface them so the user is not
            # arbitrating blind: fall back to the most recent rejections when
            # nothing is pending.
            remarks = list(task_state.pending_remarks)
            if not remarks:
                remarks = [
                    rr.remark
                    for rr in task_state.resolved_remarks[-4:]
                    if rr.resolution == "rejected"
                ]
            if test_output is not None:
                # Per-task-test escalation: the user is arbitrating a test that
                # will not pass, not a review dispute. Prepend a synthetic
                # remark carrying the (truncated) failing output (which, for a
                # broken command, names the offending command).
                synthetic = StateRemark(
                    remark_id=0,
                    severity=Severity.USER,
                    author="per-task-test",
                    text=(
                        "per-task test FAILING. Last captured output "
                        "(truncated):\n" + (test_output or "")[:2000]
                    ),
                )
                remarks = [synthetic, *remarks]
            decision = self.gate.review_rounds_exhausted_gate(
                task_state.task.id,
                rounds,
                remarks,
                # A test escalation lets the user correct a wrong test command
                # (``fix:``); a pure review dispute does not.
                allow_fix=test_output is not None,
                command=(
                    self._task_test_cmd(task_state.task)
                    if test_output is not None
                    else None
                ),
            )
            if decision.action == "abort":
                self.store.save(state)
                self.log(
                    f"spar exec: aborted by user at the review-rounds gate "
                    f"(task {task_state.task.id})."
                )
                raise _Abort(5)
            return decision

        return gate

    # -- final Test phase + integration-fix Task + final merge ---------

    def _test_and_merge(self, state: ExecState) -> int | None:
        """Run the final Test phase; open a fix Task on failure (return None to
        loop), else run the user-gated final merge (return its exit code)."""
        state.phase = "test"
        self.store.save(state)
        gitops.checkout(self.repo, state.integration_branch)
        passed, output = self._run_test_capture(self.execution.test_command, self.repo)
        if not passed:
            # Fix-task budget (churn guard): a final test that keeps failing
            # would otherwise open fix tasks forever. 0 = unlimited.
            cap = self.execution.max_fix_tasks
            if cap > 0 and state.fix_tasks_opened >= cap:
                self.store.save(state)
                self.log(
                    f"spar exec: final test still failing after {state.fix_tasks_opened} "
                    f"integration-fix task(s) — fix-task budget ({cap}) exhausted; "
                    "aborting. Fix manually and resume with 'spar exec --continue'."
                )
                return 4
            fix = self._generate_fix_task(state, output)
            state.tasks[fix.id] = TaskState(task=fix, status="pending")
            state.fix_tasks_opened += 1
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
            self._delete_integration_branch(integration)
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
        self._delete_integration_branch(integration)
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
        summary = (
            "spar exec: final Test passed. Ready to merge integration into "
            f"'{state.target_branch}'.\n"
            f"  tasks merged: {', '.join(sorted(merged))}\n"
            f"  diff --stat {state.target_branch}..integration:\n{diffstat}"
        )
        # Open NICE remarks are non-blocking by design, but they should not
        # silently die with the run — surface them once, at the final gate.
        nice_lines = [
            f"    [{tid}] #{r.remark_id} ({r.author}): {r.text}"
            for tid, ts in sorted(state.tasks.items(), key=lambda kv: _task_id_order(kv[0]))
            for r in ts.pending_remarks
            if r.severity == Severity.NICE
        ]
        if nice_lines:
            summary += "\n  open NICE remarks (non-blocking):\n" + "\n".join(nice_lines)
        return summary

    # -- test-command execution ----------------------------------------

    def _task_test_cmd(self, task: Task) -> str:
        return task.test if task.test else self.execution.test_command

    def _apply_fix_command(self, state: ExecState, ts: TaskState, command: str) -> Task:
        """Replace the task's per-task ``test`` command (``fix:`` gate decision).

        ``Task`` is frozen, so a new one is built via ``dataclasses.replace``
        and swapped onto the ``TaskState``; the change is persisted to
        ``exec.json`` immediately so a crash before the re-run keeps the fix.
        Returns the new ``Task`` so callers can refresh their local binding.
        """
        old = self._task_test_cmd(ts.task)
        ts.task = replace(ts.task, test=command)
        self.store.save(state)
        self.log(
            f"spar exec: [{ts.task.id}] per-task test command replaced on the "
            f"user's request: {old!r} → {command!r}; re-running the test."
        )
        return ts.task

    def _run_test_capture(self, cmd: str, cwd: Path) -> tuple[bool, str]:
        """Run ``cmd`` (shell) in ``cwd``; empty command is a pass."""
        rc, output = self._run_test_rc(cmd, cwd)
        return rc == 0, output

    def _run_test_rc(self, cmd: str, cwd: Path) -> tuple[int, str]:
        """Run ``cmd`` (shell) in ``cwd``, returning ``(returncode, output)``.

        An empty command is a pass (rc 0). Exposes the raw exit code so the
        per-task loop can distinguish a broken test COMMAND (126 not
        executable / 127 command not found) — which no amount of
        re-implementing can fix — from ordinary test failures.
        """
        if not cmd:
            return 0, ""
        result = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode, output

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
            if name == "spar/integration":
                # A fully-merged integration branch is debris from a prior
                # finished run, not an orphan mid-execution artifact: sweep
                # it instead of hard-refusing the fresh run.
                current = gitops.current_branch(self.repo)
                if gitops.is_ancestor(self.repo, "spar/integration", current):
                    try:
                        gitops.delete_branch(self.repo, "spar/integration")
                    except GitError:
                        pass
                    self.log(
                        "spar exec: removed fully-merged leftover "
                        "spar/integration."
                    )
                    continue
                found.append(f"branch {name}")
            elif re.match(r"spar/t\d+", name):
                found.append(f"branch {name}")

        worktrees_dir = self.spar_dir / "worktrees"
        if worktrees_dir.exists():
            for child in sorted(worktrees_dir.iterdir()):
                found.append(f"worktree {child}")
        return found

    def _delete_integration_branch(self, name: str) -> None:
        """Best-effort cleanup: the integration branch is fully merged by the
        time this is called, so leave no leftover artifact behind for the
        next fresh run to trip over."""
        try:
            gitops.delete_branch(self.repo, name)
        except GitError:
            pass

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
