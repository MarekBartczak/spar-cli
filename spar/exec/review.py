"""Asymmetric cross-review loop for the execution phase.

One task is implemented by a single *implementing* side and judged by the
*reviewing* side. Unlike the v1 debate (symmetric, both sides edit a shared
artifact), here only the implementer edits — inside a git worktree checked out
on the task branch — and the reviewer only reads the diff and returns a verdict.

The loop runs until the reviewer emits ``DONE`` with no open blocking
(``MUST``/``USER``) remark:

1. **Reviewer turn** — build a review prompt from
   ``gitops.diff(repo, integration_base, task_state.branch)`` plus the open
   remarks, run the reviewing adapter, parse its verdict, and append any new
   remarks to the task's ledger with fresh ids. If it is ``DONE`` and no
   blocking remark is open, return. Otherwise continue.
2. **Implementer turn** — build an impl prompt with the open remarks, run the
   implementing adapter inside the worktree, then *scope-guard* the result: the
   only paths changed (staged, unstaged, or untracked) must match the task's
   ``files`` globs. An out-of-scope change rolls the worktree back to the
   pre-turn commit and retries once with a warning; a second violation raises
   :class:`ReviewAbort`. On a clean turn, apply only the implementer's
   ``resolutions`` (its own new remarks are ignored — only the reviewer raises
   remarks and decides DONE) and commit the worktree changes onto the branch.

Every adapter turn is bracketed with ``exec_state.turn_in_progress`` and the
state is persisted through ``store`` after each turn, mirroring v1's
``Orchestrator._invoke``. A lost session is retried once with a fresh session;
an unusable verdict is retried once (verdict-only prompt) before aborting.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from spar.adapters.base import Adapter, SessionLost
from spar.exec import gitops
from spar.exec.prompts import build_impl_prompt, build_review_prompt
from spar.exec.state import ExecState, ExecStateStore, TaskState
from spar.state import ResolvedRemark, StateRemark
from spar.verdict import Severity, Verdict, VerdictError, parse_verdict

__all__ = ["ReviewAbort", "run_cross_review"]

_REVIEWER_AUTHOR = "reviewer"

# Anti-spin: if the implementer produces NO file change for this many CONSECUTIVE
# turns while the review loop still has blocking work, it is not converging —
# abort loudly rather than spin forever. A legitimately converging task (each
# turn edits) resets the streak every turn and never trips this.
_NO_CHANGE_ABORT_TURNS = 2

_VERDICT_RETRY_PROMPT = """\
Your previous reply did not contain a usable <verdict> block (it was missing or
malformed). Reply with ONLY a single, syntactically valid <verdict> block and
nothing else. Do NOT edit any files during this reply.

<verdict>
status: CONTINUE
</verdict>
"""


class ReviewAbort(Exception):
    """Raised when a verdict is unusable twice, or the scope guard fails twice."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# git worktree helpers (thin wrappers; raise on failure)
# ---------------------------------------------------------------------------


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReviewAbort(
            f"git {' '.join(args)} failed in {worktree}: "
            f"{result.stderr.strip() or result.returncode}"
        )
    return result


def _head_oid(worktree: Path) -> str:
    return _git(worktree, "rev-parse", "HEAD").stdout.strip()


def _worktree_changes(worktree: Path) -> list[str]:
    """Return the paths changed in the worktree (staged, unstaged, untracked).

    ``--untracked-files=all`` is required so that an untracked file nested in a
    new directory is reported by its full path (``src/sub/deep.py``) rather than
    collapsed to the directory (``src/sub/``); the scope guard matches globs
    against full paths, so collapsing would defeat segment-aware matching.
    """
    out = _git(
        worktree, "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all"
    ).stdout
    paths: list[str] = []
    for line in out.split("\n"):
        if not line.strip():
            continue
        rest = line[3:]  # strip the two-char XY status and the separating space
        if " -> " in rest:  # rename/copy: "old -> new"
            old, new = rest.split(" -> ", 1)
            paths.extend([old, new])
        else:
            paths.append(rest)
    return paths


@lru_cache(maxsize=None)
def _glob_to_regex(glob: str) -> re.Pattern[str]:
    """Translate a path glob to an anchored, ``/``-aware regex.

    A single ``*`` / ``?`` must NOT cross a path separator, so they map to
    ``[^/]*`` / ``[^/]``. A ``**`` (globstar) *may* cross ``/`` and maps to
    ``.*``. Everything else is matched literally. Matching is full-string
    against POSIX-style paths.
    """
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        c = glob[i]
        if c == "*":
            if i + 1 < n and glob[i + 1] == "*":  # globstar: crosses '/'
                out.append(".*")
                i += 2
            else:  # single star: does not cross '/'
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _matches_scope(path: str, files: tuple[str, ...]) -> bool:
    posix = path.replace("\\", "/")
    return any(_glob_to_regex(g).match(posix) is not None for g in files)


def _scope_violations(paths: list[str], files: tuple[str, ...]) -> list[str]:
    return [p for p in paths if not _matches_scope(p, files)]


def _rollback(worktree: Path, oid: str) -> None:
    _git(worktree, "reset", "--hard", oid)
    _git(worktree, "clean", "-fd")


def _committed_paths(worktree: Path, base_oid: str, head_oid: str) -> list[str]:
    """Paths changed by commits the agent made ITSELF during its turn."""
    out = _git(
        worktree, "-c", "core.quotePath=false", "diff", "--name-only",
        f"{base_oid}..{head_oid}",
    ).stdout
    return [line for line in out.splitlines() if line]


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run_cross_review(
    *,
    task_state: TaskState,
    impl_adapter: Adapter,
    review_adapter: Adapter,
    repo: Path,
    worktree: Path,
    integration_base: str,
    plan_path: Path,
    timeout_sec: int,
    store: ExecStateStore,
    exec_state: ExecState,
    log=print,
    max_rounds: int = 0,
    rounds_gate=None,
) -> None:
    """Drive the asymmetric cross-review loop until the reviewer emits DONE.

    Mutates ``task_state`` (ledger + session ids) and ``exec_state``
    (turn_in_progress), persisting through ``store`` after each turn.

    ``max_rounds`` (0 = unlimited) bounds the number of non-terminating
    reviewer verdicts: an implementer that keeps making REAL edits while the
    reviewer never DONEs (e.g. an unsatisfiable MUST) would otherwise churn
    forever — the anti-spin guard below only catches no-change turns. On
    exhaustion, ``rounds_gate(task_state, rounds_used)`` decides: ``accept``
    stops reviewing (the task proceeds as-is to its per-Task test), ``extend``
    grants ``extra_rounds`` more; anything else — or no gate — aborts loudly.
    """
    repo = Path(repo)
    worktree = Path(worktree)
    task = task_state.task

    no_change_streak = 0  # consecutive implementer turns that changed no files
    rounds_used = 0  # non-terminating reviewer verdicts so far

    while True:
        # -- reviewer turn ------------------------------------------------
        diff_text = gitops.diff(repo, integration_base, task_state.branch or integration_base)
        prompt = build_review_prompt(task, diff_text, list(task_state.pending_remarks))
        result = _invoke(
            role="review",
            adapter=review_adapter,
            prompt=prompt,
            hash_before=gitops.rev_parse(repo, task_state.branch or integration_base),
            task_state=task_state,
            exec_state=exec_state,
            store=store,
            log=log,
            timeout_sec=timeout_sec,
        )
        verdict = _parse_verdict_with_retry(
            role="review",
            adapter=review_adapter,
            result=result,
            task_state=task_state,
            exec_state=exec_state,
            store=store,
            log=log,
            timeout_sec=timeout_sec,
        )

        # Append the reviewer's new remarks to the ledger with fresh ids.
        for rem in verdict.remarks:
            task_state.pending_remarks.append(
                StateRemark(
                    remark_id=task_state.next_remark_id,
                    severity=rem.severity,
                    author=_REVIEWER_AUTHOR,
                    text=rem.text,
                )
            )
            task_state.next_remark_id += 1
        store.save(exec_state)

        blocking_open = [
            r for r in task_state.pending_remarks if r.severity in (Severity.MUST, Severity.USER)
        ]
        if verdict.status == "DONE" and not blocking_open:
            log(f"[t={task.id}] reviewer DONE; cross-review complete.")
            return
        if verdict.status == "DONE" and blocking_open:
            log(
                f"[t={task.id}] reviewer emitted DONE with {len(blocking_open)} open "
                "blocking remark(s); continuing to an implementer turn."
            )

        # -- review-round budget (churn guard) -----------------------------
        rounds_used += 1
        if max_rounds > 0 and rounds_used >= max_rounds:
            if rounds_gate is None:
                raise ReviewAbort(
                    f"task {task.id}: review did not converge within "
                    f"{max_rounds} round(s)"
                )
            decision = rounds_gate(task_state, rounds_used)
            if decision.action == "accept":
                log(
                    f"[t={task.id}] review-round budget exhausted; user accepted "
                    "the task as-is."
                )
                return
            if decision.action == "extend" and decision.extra_rounds > 0:
                max_rounds += decision.extra_rounds
                log(
                    f"[t={task.id}] review extended by {decision.extra_rounds} "
                    "round(s)."
                )
            else:
                raise ReviewAbort(
                    f"task {task.id}: unexpected review-rounds gate action "
                    f"{decision.action!r}"
                )

        # -- implementer turn --------------------------------------------
        made_changes = _implementer_turn(
            task_state=task_state,
            impl_adapter=impl_adapter,
            worktree=worktree,
            plan_path=plan_path,
            exec_state=exec_state,
            store=store,
            log=log,
            timeout_sec=timeout_sec,
        )

        # Anti-spin guard: the loop only reaches an implementer turn while there
        # is still blocking work (a non-terminating reviewer verdict). An
        # implementer that changes NO files for several consecutive turns is not
        # converging — abort loudly rather than loop forever writing nothing.
        if made_changes:
            no_change_streak = 0
        else:
            no_change_streak += 1
            if no_change_streak >= _NO_CHANGE_ABORT_TURNS:
                raise ReviewAbort(
                    f"task {task.id}: implementer produced no changes across "
                    f"{no_change_streak} turns"
                )


def _implementer_turn(
    *,
    task_state: TaskState,
    impl_adapter: Adapter,
    worktree: Path,
    plan_path: Path,
    exec_state: ExecState,
    store: ExecStateStore,
    log,
    timeout_sec: int,
    warning: str | None = None,
) -> bool:
    """Run one implementer turn; return ``True`` iff it committed file changes.

    ``warning`` seeds the first attempt's prompt with out-of-loop failure
    context (e.g. the per-Task test's captured failing output on a
    testing→implementing re-entry, spec §6). Two in-turn guards may overwrite it
    with their own message and retry:

    - **scope guard** — a change outside the task's file scope is rolled back and
      retried once; a second violation raises :class:`ReviewAbort`.
    - **anti-spin guard** — a verdict that marks ≥1 remark ``accepted`` while
      changing NO files on disk is a protocol contradiction (accepted a fix,
      didn't apply it). It is retried once with a stern warning; the accepted
      remarks are held OPEN across that retry so the re-prompt still lists them.
    """
    task = task_state.task

    scope_retried = False
    nochange_retried = False

    while True:  # bounded: each guard retries at most once (its flag latches)
        pre_oid = _head_oid(worktree)
        prompt = build_impl_prompt(
            task, plan_path, list(task_state.pending_remarks), warning=warning
        )
        result = _invoke(
            role="impl",
            adapter=impl_adapter,
            prompt=prompt,
            hash_before=pre_oid,
            task_state=task_state,
            exec_state=exec_state,
            store=store,
            log=log,
            timeout_sec=timeout_sec,
        )

        # Parse the verdict FIRST. ``_parse_verdict_with_retry`` may run the
        # implementer adapter a second time (a verdict-only retry), and that
        # retry can also touch the worktree. The scope guard must therefore run
        # on the FINAL worktree state — after any retry — so no path that
        # reaches the commit below can escape it.
        verdict = _parse_verdict_with_retry(
            role="impl",
            adapter=impl_adapter,
            result=result,
            task_state=task_state,
            exec_state=exec_state,
            store=store,
            log=log,
            timeout_sec=timeout_sec,
        )

        # The implementer never ends the review — only the reviewer's DONE does.
        # A model that treats this turn as a review (e.g. emitting AGREE, or
        # even DONE) must not derail or terminate the loop: coerce to CONTINUE
        # and keep going.
        if verdict.status != "CONTINUE":
            log(
                f"[t={task.id}] implementer emitted status={verdict.status!r}; "
                "coercing to CONTINUE (only the reviewer emits DONE)."
            )
            verdict = Verdict(
                status="CONTINUE", resolutions=verdict.resolutions, remarks=verdict.remarks
            )

        # An agent may run ``git commit`` itself inside the worktree: such a
        # turn leaves the worktree clean but moves HEAD. Its committed paths
        # must be scope-checked like uncommitted ones (rollback resets to
        # ``pre_oid``, which undoes agent commits too), and the turn must count
        # as real progress for the no-change guards below.
        post_oid = _head_oid(worktree)
        self_committed = (
            _committed_paths(worktree, pre_oid, post_oid) if post_oid != pre_oid else []
        )
        changes = _worktree_changes(worktree) + self_committed
        violations = _scope_violations(changes, task.files)
        if violations:
            _rollback(worktree, pre_oid)
            if not scope_retried:
                scope_retried = True
                log(
                    f"[t={task.id}] scope violation: {sorted(violations)} outside "
                    f"{list(task.files)}; rolled back, retrying the turn."
                )
                warning = (
                    "Your previous turn changed files outside your allowed scope "
                    f"({sorted(violations)}) and was rolled back. Edit ONLY the files "
                    f"listed in the file scope: {list(task.files)}."
                )
                continue
            raise ReviewAbort(
                f"task {task.id}: second scope violation {sorted(violations)} "
                f"outside {list(task.files)}"
            )

        # Anti-spin: accepting a remark while writing nothing is a contradiction.
        # Retry once with a stern warning, holding the accepted remarks OPEN (do
        # NOT apply resolutions yet) so the re-prompt still lists them to fix.
        accepted = [r for r in verdict.resolutions if r.accepted]
        if accepted and not changes and not nochange_retried:
            nochange_retried = True
            log(
                f"[t={task.id}] implementer marked {len(accepted)} remark(s) accepted "
                "but changed no files; retrying the turn with a warning."
            )
            warning = (
                "You marked remark(s) as accepted but changed NO files on disk. A remark "
                "you accept REQUIRES a real code change to the file(s) this turn — apply "
                "the changes on disk now with your file-editing tools, or reject the "
                "remark with a reason instead."
            )
            continue

        # scope OK (and no unaddressed accept-without-edit contradiction to
        # retry): apply only the implementer's resolutions.
        _apply_resolutions(task_state, verdict, log)

        # Commit whatever the implementer changed onto the task branch so the
        # next reviewer turn's diff reflects it. A no-op turn is a normal
        # continue (e.g. leaving an open MUST unaddressed).
        if _worktree_changes(worktree):
            short = task.description.splitlines()[0][:60] if task.description else task.id
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"{task.id}: {short}")
            log(f"[t={task.id}] committed implementer changes.")
            store.save(exec_state)
            return True
        if post_oid != pre_oid:
            log(f"[t={task.id}] implementer committed its changes itself.")
            store.save(exec_state)
            return True
        log(f"[t={task.id}] implementer made no change this turn.")
        store.save(exec_state)
        return False


def _apply_resolutions(task_state: TaskState, verdict: Verdict, log) -> None:
    """Move remarks resolved by the implementer from pending to resolved.

    New remarks the implementer may have raised are ignored — only the reviewer
    raises remarks and decides DONE/CONTINUE.
    """
    for res in verdict.resolutions:
        match = next(
            (r for r in task_state.pending_remarks if r.remark_id == res.remark_id), None
        )
        if match is None:
            log(f"[t={task_state.task.id}] resolution for unknown remark #{res.remark_id}; ignoring.")
            continue
        task_state.pending_remarks.remove(match)
        task_state.resolved_remarks.append(
            ResolvedRemark(
                remark=match,
                resolution="accepted" if res.accepted else "rejected",
                justification=res.justification,
            )
        )


# ---------------------------------------------------------------------------
# Adapter invocation + verdict parsing (mirrors v1 Orchestrator patterns)
# ---------------------------------------------------------------------------


def _invoke(
    *,
    role: str,
    adapter: Adapter,
    prompt: str,
    hash_before: str,
    task_state: TaskState,
    exec_state: ExecState,
    store: ExecStateStore,
    log,
    timeout_sec: int,
):
    """Run one adapter turn, bracketing it with ``turn_in_progress`` and
    handling a lost session with a single fresh retry."""
    exec_state.turn_in_progress = {
        "task_id": task_state.task.id,
        "role": role,
        "hash_before": hash_before,
    }
    store.save(exec_state)

    session_id = task_state.impl_session_id if role == "impl" else task_state.review_session_id
    try:
        result = adapter.run_turn(prompt, session_id, timeout_sec)
    except SessionLost:
        log(f"[t={task_state.task.id}] {role} session lost; retrying with a fresh session.")
        result = adapter.run_turn(prompt, None, timeout_sec)

    if role == "impl":
        task_state.impl_session_id = result.session_id
    else:
        task_state.review_session_id = result.session_id
    exec_state.turn_in_progress = None
    store.save(exec_state)
    return result


def _parse_verdict_with_retry(
    *,
    role: str,
    adapter: Adapter,
    result,
    task_state: TaskState,
    exec_state: ExecState,
    store: ExecStateStore,
    log,
    timeout_sec: int,
) -> Verdict:
    """Parse the reply's verdict; on failure, demand exactly one corrected
    verdict in the same session, then raise :class:`ReviewAbort` if still bad."""
    try:
        return parse_verdict(result.reply_text)
    except VerdictError as exc:
        log(f"[t={task_state.task.id}] {role} unusable verdict: {exc}; demanding a corrected one.")

    retry_result = _invoke(
        role=role,
        adapter=adapter,
        prompt=_VERDICT_RETRY_PROMPT,
        hash_before="",
        task_state=task_state,
        exec_state=exec_state,
        store=store,
        log=log,
        timeout_sec=timeout_sec,
    )
    try:
        return parse_verdict(retry_result.reply_text)
    except VerdictError as exc:
        raise ReviewAbort(
            f"task {task_state.task.id}: {role} verdict still unusable on retry: {exc}"
        ) from exc
