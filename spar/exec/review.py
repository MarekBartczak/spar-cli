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

import fnmatch
import subprocess
from dataclasses import replace
from pathlib import Path

from spar.adapters.base import Adapter, SessionLost
from spar.exec import gitops
from spar.exec.prompts import build_impl_prompt, build_review_prompt
from spar.exec.state import ExecState, ExecStateStore, TaskState
from spar.state import ResolvedRemark, StateRemark
from spar.verdict import Severity, Verdict, VerdictError, parse_verdict

__all__ = ["ReviewAbort", "run_cross_review"]

_REVIEWER_AUTHOR = "reviewer"

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
    """Return the paths changed in the worktree (staged, unstaged, untracked)."""
    out = _git(worktree, "-c", "core.quotePath=false", "status", "--porcelain").stdout
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


def _scope_violations(paths: list[str], files: tuple[str, ...]) -> list[str]:
    return [p for p in paths if not any(fnmatch.fnmatch(p, g) for g in files)]


def _rollback(worktree: Path, oid: str) -> None:
    _git(worktree, "reset", "--hard", oid)
    _git(worktree, "clean", "-fd")


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
) -> None:
    """Drive the asymmetric cross-review loop until the reviewer emits DONE.

    Mutates ``task_state`` (ledger + session ids) and ``exec_state``
    (turn_in_progress), persisting through ``store`` after each turn.
    """
    repo = Path(repo)
    worktree = Path(worktree)
    task = task_state.task

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

        # -- implementer turn --------------------------------------------
        _implementer_turn(
            task_state=task_state,
            impl_adapter=impl_adapter,
            worktree=worktree,
            plan_path=plan_path,
            exec_state=exec_state,
            store=store,
            log=log,
            timeout_sec=timeout_sec,
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
) -> None:
    task = task_state.task
    warning: str | None = None

    for attempt in range(2):  # attempt 0 plus at most one scope-guard retry
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

        changes = _worktree_changes(worktree)
        violations = _scope_violations(changes, task.files)
        if violations:
            _rollback(worktree, pre_oid)
            if attempt == 0:
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

        # scope OK: parse verdict, apply only the implementer's resolutions.
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
        _apply_resolutions(task_state, verdict, log)

        # Commit whatever the implementer changed onto the task branch so the
        # next reviewer turn's diff reflects it. A no-op turn is a normal
        # continue (e.g. leaving an open MUST unaddressed).
        if _worktree_changes(worktree):
            short = task.description.splitlines()[0][:60] if task.description else task.id
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"{task.id}: {short}")
            log(f"[t={task.id}] committed implementer changes.")
        else:
            log(f"[t={task.id}] implementer made no change this turn.")
        store.save(exec_state)
        return


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
