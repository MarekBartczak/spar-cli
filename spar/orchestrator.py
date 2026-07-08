"""Orchestrator: the adversarial-debate loop at the heart of spar-cli.

Two AI CLIs (the *sides*) take turns editing a single shared artifact file.
After every turn a side must end its reply with a ``<verdict>`` block. The
orchestrator parses that verdict, keeps a running ledger of open/resolved
remarks in ``DebateState``, and drives the debate until *per-hash consensus*
(both sides ``AGREE`` on the same artifact hash with no blocking remarks) or
the round budget is exhausted — at which point the human arbiter (a
``UserGate``) decides what happens next.

This module only *composes* the already-built pieces (adapters, verdict
parser, state store, config); it contains the control flow, the prompt
contract, and the remark bookkeeping, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from spar.adapters.base import Adapter, AdapterError, SessionLost, TurnResult
from spar.config import DebateConfig, SideConfig
from spar.exec.tasklist import TaskListError, parse_task_list
from spar.state import (
    DebateState,
    LockHeld,
    ResolvedRemark,
    SideState,
    StateError,
    StateRemark,
    StateStore,
    TurnInProgress,
    check_recovery,
    hash_artifact,
)
from spar.verdict import Severity, Verdict, VerdictError, parse_verdict

__all__ = [
    "GateDecision",
    "UserGate",
    "ConsoleGate",
    "GuardContext",
    "GuardViolation",
    "GuardHook",
    "Orchestrator",
    "build_turn_prompt",
    "is_consensus",
    "PROTOCOL_BLOCK",
]


# ---------------------------------------------------------------------------
# User gate (human arbiter)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """A decision returned by a :class:`UserGate`.

    ``action`` is one of ``"accept"``, ``"remarks"``, ``"extend"`` or
    ``"abort"``. ``remarks`` is non-empty iff ``action == "remarks"`` and
    ``extra_rounds`` is ``> 0`` iff ``action == "extend"``.
    """

    action: str
    remarks: tuple[str, ...] = ()
    extra_rounds: int = 0


class UserGate(Protocol):
    """The human-in-the-loop decision points."""

    def consensus_gate(
        self, artifact_path: Path, nice_backlog: list[StateRemark]
    ) -> GateDecision: ...

    def rounds_exhausted_gate(
        self, artifact_path: Path, pending: list[StateRemark]
    ) -> GateDecision: ...

    def recovery_gate(self, artifact_path: Path, expected_hash: str) -> str:
        """Return ``"keep"`` or ``"repeat"``."""
        ...


class ConsoleGate:
    """Default :class:`UserGate` that talks to a human over stdin/stdout.

    ``input_fn`` and ``print_fn`` are injectable so the gate can be driven
    deterministically from tests without a real terminal.
    """

    def __init__(self, input_fn=input, print_fn=print) -> None:
        self._input = input_fn
        self._print = print_fn

    def consensus_gate(
        self, artifact_path: Path, nice_backlog: list[StateRemark]
    ) -> GateDecision:
        self._print(f"\nConsensus reached on {artifact_path}.")
        if nice_backlog:
            self._print("Outstanding NICE-to-have remarks (non-blocking):")
            for r in nice_backlog:
                self._print(f"  #{r.remark_id} ({r.author}): {r.text}")
        while True:
            choice = self._input(
                "[a]ccept / add [r]emarks / [x] abort? "
            ).strip().lower()
            if choice in ("a", "accept"):
                return GateDecision(action="accept")
            if choice in ("x", "abort"):
                return GateDecision(action="abort")
            if choice in ("r", "remarks"):
                remarks = self._collect_remarks()
                if remarks:
                    return GateDecision(action="remarks", remarks=remarks)
                self._print("No remarks entered.")
                continue
            self._print("Please answer with 'a', 'r' or 'x'.")

    def rounds_exhausted_gate(
        self, artifact_path: Path, pending: list[StateRemark]
    ) -> GateDecision:
        self._print(f"\nRound budget exhausted for {artifact_path} without consensus.")
        if pending:
            self._print("Still-open remarks:")
            for r in pending:
                self._print(f"  #{r.remark_id} [{r.severity.name}] ({r.author}): {r.text}")
        while True:
            choice = self._input(
                "[a]ccept as-is / [e]xtend / [x] abort? "
            ).strip().lower()
            if choice in ("a", "accept"):
                return GateDecision(action="accept")
            if choice in ("x", "abort"):
                return GateDecision(action="abort")
            if choice in ("e", "extend"):
                extra = self._read_int("How many more rounds? ")
                if extra > 0:
                    return GateDecision(action="extend", extra_rounds=extra)
                self._print("Please enter a positive number.")
                continue
            self._print("Please answer with 'a', 'e' or 'x'.")

    def recovery_gate(self, artifact_path: Path, expected_hash: str) -> str:
        self._print(
            f"\nThe artifact {artifact_path} changed during an interrupted turn.\n"
            f"Expected pre-turn hash: {expected_hash}"
        )
        while True:
            choice = self._input(
                "[k]eep current file / [r]epeat the turn? "
            ).strip().lower()
            if choice in ("k", "keep"):
                return "keep"
            if choice in ("r", "repeat"):
                return "repeat"
            self._print("Please answer with 'k' or 'r'.")

    def _collect_remarks(self) -> tuple[str, ...]:
        self._print("Enter one remark per line; blank line to finish:")
        out: list[str] = []
        while True:
            line = self._input("> ").strip()
            if not line:
                break
            out.append(line)
        return tuple(out)

    def _read_int(self, prompt: str) -> int:
        raw = self._input(prompt).strip()
        try:
            return int(raw)
        except ValueError:
            return 0


# ---------------------------------------------------------------------------
# Guard hook (real implementation lands in guard.py, task 8)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardContext:
    """Everything a guard needs to judge a completed turn."""

    artifact_path: Path
    hash_before: str
    hash_after: str
    reply_text: str


class GuardViolation(Exception):
    """Raised by a guard hook when a turn breaks the rules of engagement."""


GuardHook = Callable[[GuardContext], None]


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------


PROTOCOL_BLOCK = """\
End your reply with EXACTLY ONE verdict block, using this syntax verbatim:

<verdict>
status: CONTINUE
resolved:
- #7 accepted
- #9 rejected: <one-line reason you disagree>
remarks:
- [MUST] <a blocking concern that must be fixed before you can agree>
- [NICE] <an optional, non-blocking suggestion>
</verdict>

Protocol:
- status is one of CONTINUE, AGREE, or DONE:
  - CONTINUE — you still want changes (yours or others' open concerns).
  - AGREE — you accept the artifact as it stands; you MAY still edit it this
    turn and MAY add [NICE] suggestions.
  - DONE — you are finished: you agree AND made NO edits to the artifact this
    turn and have NO further concerns. This is the signal to end the debate.
- The debate ends only when BOTH sides say DONE. If you edit the artifact on a
  DONE turn it is downgraded to AGREE (editing means you are not done), so use
  DONE only when you truly have nothing left to change.
- Use AGREE or DONE only if you have NO MUST-level concerns remaining (yours or
  anyone else's open MUST/USER remarks).
- In `resolved:` you MUST address EVERY open remark id listed above, each as
  either `#<id> accepted` or `#<id> rejected: <why>`.
- In `remarks:` raise your own new concerns, tagged `[MUST]` (blocking) or
  `[NICE]` (optional). Omit the section if you have none.
"""


def _format_remarks(open_remarks: list[StateRemark]) -> str:
    if not open_remarks:
        return "No open remarks."
    lines = ["Open remarks:"]
    for r in open_remarks:
        lines.append(f"#{r.remark_id} [{r.severity.name}] ({r.author}): {r.text}")
    return "\n".join(lines)


def _format_tasks_contract(catalogs: dict[str, tuple[str, ...]] | None) -> str:
    """The opt-in `## Tasks` planning contract appended to a turn prompt.

    Instructs the sides that the final agreed plan MUST end with a machine-
    parsable ``## Tasks`` section (the §4.1 grammar), lists each side's model
    catalog so assignments are valid, and states that consensus is gated on
    the section parsing.
    """
    catalogs = catalogs or {}
    lines = [
        "TASK-LIST REQUIREMENT (this debate feeds the execution engine):",
        "The final agreed plan MUST end with a `## Tasks` section listing one "
        "task per line in EXACTLY this grammar:",
        "",
        "## Tasks",
        "- [t<n>] <desc> | side=<side> | model=<impl-model> | "
        "review=<review-model> | deps=<id,id|-> | files=<glob,glob>",
        "",
        "Rules:",
        "- side is one of the configured sides: "
        + ", ".join(catalogs.keys())
        + ".",
        "- model is one of THAT side's models (its catalog, below).",
        "- review is one of the OTHER side's models (the reviewer is the "
        "non-implementing side).",
        "- deps is a comma list of earlier task ids this task depends on, or "
        "`-` for none.",
        "- files is a comma list of globs naming the task's file scope.",
        "",
        "Model catalogs (assign only models a side actually has):",
    ]
    for side, models in catalogs.items():
        lines.append(f"- {side}: {', '.join(models)}")
    lines.append("")
    lines.append(
        "Consensus is NOT accepted until this `## Tasks` section parses; a "
        "missing or malformed section will be sent back as a blocking remark."
    )
    return "\n".join(lines)


def build_turn_prompt(
    *,
    side_name: str,
    artifact_path: Path,
    artifact_hash: str | None,
    open_remarks: list[StateRemark],
    task_prompt: str,
    kind: str = "turn",
    catalogs: dict[str, tuple[str, ...]] | None = None,
    require_tasks: bool = False,
) -> str:
    """Pure, unit-testable builder for the prompt handed to a side.

    ``kind`` selects the variant:

    - ``"turn"`` — the full contract, in order: (1) role, (2) artifact path +
      current sha256 + edit-in-place instruction, (3) open remarks (or "No
      open remarks."), (4) the user's original task, (5) the protocol block.
    - ``"creation"`` — same, minus the open-remarks section, and telling the
      creator the file does not exist yet and must be created.
    - ``"verdict_retry"`` — a minimal prompt that quotes nothing, demands only
      a syntactically valid verdict block, and forbids editing the artifact.
    """
    role = (
        f'You are side "{side_name}" in an adversarial design debate. '
        f"The artifact is the single deliverable of this debate."
    )

    if kind == "verdict_retry":
        return (
            "Your previous reply did not contain a usable verdict block "
            "(it was missing/malformed, or its status contradicted the open "
            "remarks).\n"
            "Reply with ONLY a single, syntactically valid <verdict> block and "
            "nothing else.\n"
            "Do NOT edit the artifact file during this reply.\n\n"
            + PROTOCOL_BLOCK
        )

    parts: list[str] = [role, ""]

    if kind == "creation":
        parts += [
            f"Artifact path: {artifact_path}",
            "The artifact file does not exist yet — create it at that path this "
            "turn. You own this file.",
            "",
        ]
    elif kind == "turn":
        parts += [
            f"Artifact path: {artifact_path}",
            f"Current artifact sha256: {artifact_hash}",
            "Read the artifact, then improve it in place. You own this file for "
            "this turn.",
            "",
            _format_remarks(open_remarks),
            "",
        ]
    else:  # pragma: no cover - defensive
        raise ValueError(f"unknown prompt kind: {kind!r}")

    parts += [
        "Original task (for context):",
        task_prompt,
        "",
    ]
    if require_tasks:
        parts += [_format_tasks_contract(catalogs), ""]
    parts.append(PROTOCOL_BLOCK)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Consensus (pure predicate, unit-testable)
# ---------------------------------------------------------------------------


def is_consensus(state: DebateState, order: list[str]) -> bool:
    """True iff every side's most recent verdict is ``DONE`` and no MUST/USER
    remark is still pending.

    ``DONE`` is an explicit terminal handshake: a side may only emit it on a
    turn where it made no edits (the orchestrator downgrades an editing DONE
    turn to AGREE). Because DONE turns never change the artifact, two most-
    recent DONE verdicts — one per side — necessarily describe the same, final
    content, so no separate same-hash check is needed.

    NICE remarks may remain pending at consensus (they become the gate's
    backlog); MUST and USER remarks are blocking.
    """
    for name in order:
        side = state.sides.get(name)
        if side is None or side.last_verdict_status != "DONE":
            return False
    if any(r.severity in (Severity.MUST, Severity.USER) for r in state.pending_remarks):
        return False
    return True


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class _Abort(Exception):
    """Internal control-flow signal carrying the process exit code."""

    def __init__(self, code: int) -> None:
        super().__init__(f"abort with exit code {code}")
        self.code = code


_TASK_FILENAME = "task.md"


class Orchestrator:
    """Drives a debate to consensus (or an arbiter decision)."""

    def __init__(
        self,
        sides: dict[str, Adapter],
        order: list[str],
        store: StateStore,
        artifact_path: Path,
        debate: DebateConfig,
        gate: UserGate,
        guard: GuardHook | None = None,
        log=print,
        side_configs: dict[str, SideConfig] | None = None,
        require_tasks: bool = False,
    ) -> None:
        if len(order) < 2:
            raise ValueError("a debate needs at least two sides")
        missing = [name for name in order if name not in sides]
        if missing:
            raise ValueError(f"order references unknown sides: {missing}")
        self.sides = sides
        self.order = list(order)
        self.store = store
        self.artifact_path = Path(artifact_path)
        self.debate = debate
        self.gate = gate
        self.guard = guard
        self.log = log
        self.side_configs = side_configs
        self.require_tasks = require_tasks

    # -- public entry points -------------------------------------------

    def run_new(self, task_prompt: str) -> int:
        try:
            with self.store.locked():
                return self._run_new(task_prompt)
        except LockHeld as exc:
            self.log(f"spar: another instance holds the lock ({exc}).")
            return 3

    def run_continue(self) -> int:
        try:
            with self.store.locked():
                return self._run_continue()
        except LockHeld as exc:
            self.log(f"spar: another instance holds the lock ({exc}).")
            return 3

    # -- run_new -------------------------------------------------------

    def _run_new(self, task_prompt: str) -> int:
        self.store.spar_dir.mkdir(parents=True, exist_ok=True)
        (self.store.spar_dir / "transcript").mkdir(parents=True, exist_ok=True)
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self._save_task(task_prompt)

        state = DebateState(sides={name: SideState() for name in self.order})
        self.store.save(state)

        try:
            creator = self.order[0]
            self._take_turn(state, creator, "creation", task_prompt, is_round_end=False)
            return self._debate_loop(
                state, task_prompt, next_idx=1, budget=self.debate.max_rounds
            )
        except _Abort as exc:
            return exc.code
        except AdapterError as exc:
            self.log(f"spar: adapter failed, aborting: {exc}")
            return 4

    # -- run_continue --------------------------------------------------

    def _run_continue(self) -> int:
        try:
            state = self.store.load()
        except StateError as exc:
            self.log(f"spar: cannot load debate state: {exc}")
            return 3

        mismatch = self._check_sides_match(state)
        if mismatch is not None:
            self.log(mismatch)
            return 3

        task_prompt = self._load_task()
        if task_prompt is None:
            self.log(
                "spar: cannot resume — the original task prompt "
                f"({self.store.spar_dir / _TASK_FILENAME}) is missing."
            )
            return 3

        try:
            status = check_recovery(state, self.artifact_path)
        except StateError as exc:
            self.log(f"spar: cannot inspect the artifact for recovery: {exc}")
            return 3

        next_idx = self._recover(state, status)
        if next_idx is None:
            return 3

        try:
            return self._debate_loop(
                state, task_prompt, next_idx=next_idx, budget=self.debate.max_rounds
            )
        except _Abort as exc:
            return exc.code
        except AdapterError as exc:
            self.log(f"spar: adapter failed, aborting: {exc}")
            return 4

    def _recover(self, state: DebateState, status: str) -> int | None:
        """Resolve a recovery ``status`` into the index of the next actor.

        Returns ``None`` (and logs) when the debate cannot be resumed.
        """
        if status == "clean":
            if state.last_actor is None:
                self.log("spar: nothing to resume — no completed turns recorded.")
                return None
            return (self.order.index(state.last_actor) + 1) % len(self.order)

        if status == "repeat_turn":
            interrupted = state.turn_in_progress.side
            state.turn_in_progress = None
            self.store.save(state)
            self.log(f"spar: resuming — re-running {interrupted}'s interrupted turn.")
            return self.order.index(interrupted)

        if status == "artifact_changed":
            interrupted = state.turn_in_progress.side
            expected = state.turn_in_progress.artifact_hash_before
            choice = self.gate.recovery_gate(self.artifact_path, expected)
            current = hash_artifact(self.artifact_path)
            state.turn_in_progress = None
            state.artifact_hash = current
            self.store.save(state)
            if choice == "keep":
                # We keep no backup copies of the artifact, so "keep" simply
                # adopts the on-disk file as the new baseline and advances past
                # the interrupted (never-completed) turn. That turn's verdict
                # was never recorded, so the interrupted side cannot count
                # toward consensus until it speaks again on this new hash.
                return (self.order.index(interrupted) + 1) % len(self.order)
            if choice == "repeat":
                # "repeat" cannot restore the pre-turn artifact (no copies are
                # kept), so we adopt the current file and re-run the
                # interrupted side against it.
                return self.order.index(interrupted)
            self.log(f"spar: invalid recovery choice {choice!r}.")
            return None

        self.log(f"spar: unknown recovery status {status!r}.")  # pragma: no cover
        return None

    def _check_sides_match(self, state: DebateState) -> str | None:
        """Verify the persisted debate's sides match this orchestrator's.

        Returns an error message (and does not raise) if ``state.sides``
        or ``state.last_actor`` reference side names outside ``self.order``,
        so the caller can abort cleanly instead of hitting an uncaught
        ``ValueError``/``KeyError`` deeper in the debate loop.
        """
        persisted = set(state.sides.keys())
        configured = set(self.order)
        if persisted != configured:
            return (
                "spar: persisted debate sides do not match the configured "
                f"sides: persisted={sorted(persisted)}, "
                f"configured={sorted(configured)}."
            )
        if state.last_actor is not None and state.last_actor not in self.order:
            return (
                "spar: persisted last_actor "
                f"{state.last_actor!r} is not among the configured sides "
                f"{sorted(self.order)}."
            )
        return None

    # -- the debate loop -----------------------------------------------

    def _debate_loop(
        self, state: DebateState, task_prompt: str, next_idx: int, budget: int
    ) -> int:
        while True:
            try:
                hash_artifact(self.artifact_path)  # verify the artifact still exists
            except StateError:
                self.log(
                    "spar: cannot check consensus — artifact file missing: "
                    f"{self.artifact_path}"
                )
                raise _Abort(4)

            if is_consensus(state, self.order):
                if self.require_tasks and not self._tasks_section_valid(state):
                    continue  # tasks invalid; remark injected, keep debating
                code = self._handle_consensus(state)
                if code is not None:
                    return code
                continue  # user injected remarks; keep debating

            if state.round >= budget:
                code, extra = self._handle_rounds_exhausted(state)
                if code is not None:
                    return code
                budget += extra
                continue

            actor = self.order[next_idx]
            is_round_end = next_idx == len(self.order) - 1
            self._take_turn(state, actor, "turn", task_prompt, is_round_end)
            next_idx = 0 if is_round_end else next_idx + 1

    def _tasks_section_valid(self, state: DebateState) -> bool:
        """Validate the artifact's ``## Tasks`` section at consensus.

        On success returns True (the debate may finalize). On a
        :class:`TaskListError` the debate is un-consensus'd: a blocking
        ``MUST`` remark describing the failure is injected, every side's
        ``last_verdict_status`` is reset to ``None`` (so the sides must speak
        again after fixing the section), the state is persisted, and False is
        returned so the loop keeps debating.
        """
        artifact_text = self.artifact_path.read_text(encoding="utf-8")
        try:
            parse_task_list(
                artifact_text, sides=self.side_configs or {}, order=self.order
            )
            return True
        except TaskListError as exc:
            text = (
                "The plan's `## Tasks` section is missing or invalid: "
                f"{exc}. Fix its format per the grammar."
            )
            state.pending_remarks.append(
                StateRemark(
                    remark_id=state.next_remark_id,
                    severity=Severity.MUST,
                    author="spar",
                    text=text,
                )
            )
            state.next_remark_id += 1
            for name in self.order:
                state.sides[name] = replace(
                    state.sides[name], last_verdict_status=None
                )
            self.store.save(state)
            self.log(f"spar: consensus rejected — {text}")
            return False

    def _handle_consensus(self, state: DebateState) -> int | None:
        nice = [r for r in state.pending_remarks if r.severity == Severity.NICE]
        decision = self.gate.consensus_gate(self.artifact_path, nice)

        if decision.action == "accept":
            self._print_summary(state, "consensus accepted")
            return 0
        if decision.action == "abort":
            self.store.save(state)
            self.log("spar: aborted by user at the consensus gate.")
            return 5
        if decision.action == "remarks":
            for text in decision.remarks:
                state.pending_remarks.append(
                    StateRemark(
                        remark_id=state.next_remark_id,
                        severity=Severity.USER,
                        author="user",
                        text=text,
                    )
                )
                state.next_remark_id += 1
            for name in self.order:
                state.sides[name] = replace(state.sides[name], last_verdict_status=None)
            self.store.save(state)
            self.log(
                f"spar: injected {len(decision.remarks)} user remark(s); "
                "resuming the debate."
            )
            return None
        raise ValueError(f"unexpected consensus-gate action: {decision.action!r}")

    def _handle_rounds_exhausted(self, state: DebateState) -> tuple[int | None, int]:
        decision = self.gate.rounds_exhausted_gate(
            self.artifact_path, list(state.pending_remarks)
        )
        if decision.action == "accept":
            self._print_summary(state, "accepted without consensus")
            return 0, 0
        if decision.action == "abort":
            self.store.save(state)
            self.log("spar: aborted by user at the rounds-exhausted gate.")
            return 5, 0
        if decision.action == "extend":
            if decision.extra_rounds <= 0:
                raise ValueError("extend gate decision must set extra_rounds > 0")
            self.log(f"spar: extending the debate by {decision.extra_rounds} round(s).")
            return None, decision.extra_rounds
        raise ValueError(f"unexpected rounds-exhausted-gate action: {decision.action!r}")

    # -- a single side turn --------------------------------------------

    def _take_turn(
        self,
        state: DebateState,
        side: str,
        kind: str,
        task_prompt: str,
        is_round_end: bool,
    ) -> None:
        """Run one full turn for ``side``: adapter call, verdict parse (with a
        one-shot verdict retry), guard check (with a one-shot whole-turn
        retry), then bookkeeping. Raises :class:`_Abort` on a fatal violation.
        """
        guard_warning: str | None = None

        for attempt in range(2):  # attempt 0, plus at most one guard retry
            pre_turn = getattr(self.guard, "pre_turn", None)
            if pre_turn is not None:
                pre_turn()

            hash_before = (
                None if kind == "creation" else hash_artifact(self.artifact_path)
            )
            prompt = self._compose_prompt(state, side, kind, task_prompt, guard_warning)
            result = self._invoke(state, side, prompt, hash_before or "")

            if kind == "creation" and not self.artifact_path.exists():
                result = self._creation_retry(state, side, task_prompt)

            verdict, result = self._parse_verdict_with_retry(state, side, result)
            hash_after = hash_artifact(self.artifact_path)

            if self.guard is not None:
                ctx = GuardContext(
                    artifact_path=self.artifact_path,
                    hash_before=hash_before or "",
                    hash_after=hash_after,
                    reply_text=result.reply_text,
                )
                try:
                    self.guard(ctx)
                except GuardViolation as exc:
                    if attempt == 0:
                        self.log(f"[{side}] guard violation: {exc}; retrying the turn.")
                        guard_warning = (
                            f"WARNING (guard): your previous turn was rejected: {exc}. "
                            "Redo the turn and do not repeat the violation."
                        )
                        continue
                    self.log(f"[{side}] second guard violation: {exc}; aborting.")
                    raise _Abort(4)

            # DONE is a terminal, no-edit handshake. If the side changed the
            # artifact this turn, its "done" is contradicted by the edit — the
            # opponent has not seen the new content — so downgrade to AGREE.
            edited = kind == "creation" or (
                hash_before is not None and hash_after != hash_before
            )
            if verdict.status == "DONE" and edited:
                self.log(
                    f"[{side}] DONE but edited the artifact this turn; "
                    "recording as AGREE (an edit means not done)."
                )
                verdict = replace(verdict, status="AGREE")

            self._apply_verdict(state, side, verdict, hash_after, is_round_end)
            self.log(
                f"[{side}] turn complete: status={verdict.status}, "
                f"pending={len(state.pending_remarks)}, round={state.round}."
            )
            return

    def _creation_retry(
        self, state: DebateState, side: str, task_prompt: str
    ) -> TurnResult:
        self.log(f"[{side}] did not create the artifact; retrying once.")
        warning = (
            "WARNING: you did not create the artifact file last turn. "
            "You MUST create it at the given path this turn."
        )
        prompt = self._compose_prompt(state, side, "creation", task_prompt, warning)
        result = self._invoke(state, side, prompt, "")
        if not self.artifact_path.exists():
            self.log(f"[{side}] still did not create the artifact; aborting.")
            raise _Abort(4)
        return result

    def _parse_verdict_with_retry(
        self, state: DebateState, side: str, result: TurnResult
    ) -> tuple[Verdict, TurnResult]:
        """Parse the reply's verdict; on failure, demand exactly one corrected
        verdict in the same session. Abort (exit 4) if the retry still fails or
        the side edits the artifact during the retry.
        """
        try:
            verdict = parse_verdict(result.reply_text)
            self._check_agree(state, verdict)
            return verdict, result
        except VerdictError as exc:
            self.log(f"[{side}] unusable verdict: {exc}; demanding a corrected one.")

        hash_at_retry = (
            hash_artifact(self.artifact_path)
            if self.artifact_path.exists()
            else None
        )
        retry_prompt = build_turn_prompt(
            side_name=side,
            artifact_path=self.artifact_path,
            artifact_hash=None,
            open_remarks=[],
            task_prompt="",
            kind="verdict_retry",
        )
        retry_result = self._invoke(state, side, retry_prompt, hash_at_retry or "")

        hash_now = (
            hash_artifact(self.artifact_path)
            if self.artifact_path.exists()
            else None
        )
        if hash_now != hash_at_retry:
            self.log(f"[{side}] edited the artifact during a verdict retry; aborting.")
            raise _Abort(4)

        try:
            verdict = parse_verdict(retry_result.reply_text)
            self._check_agree(state, verdict)
        except VerdictError as exc:
            self.log(f"[{side}] verdict still unusable on retry: {exc}; aborting.")
            raise _Abort(4)
        return verdict, retry_result

    def _check_agree(self, state: DebateState, verdict: Verdict) -> None:
        """Reject an AGREE/DONE that leaves a MUST/USER remark unresolved.

        Treated exactly like a malformed verdict: it triggers the same
        one-shot verdict retry.
        """
        if verdict.status not in ("AGREE", "DONE"):
            return
        resolved_ids = {r.remark_id for r in verdict.resolutions}
        for r in state.pending_remarks:
            if r.remark_id in resolved_ids:
                continue
            if r.severity in (Severity.MUST, Severity.USER):
                raise VerdictError(
                    f"{verdict.status} is not allowed while remark #{r.remark_id} "
                    f"[{r.severity.name}] is unresolved"
                )

    def _apply_verdict(
        self,
        state: DebateState,
        side: str,
        verdict: Verdict,
        hash_after: str,
        is_round_end: bool,
    ) -> None:
        # Resolutions first (they reference existing pending ids).
        for res in verdict.resolutions:
            match = next(
                (r for r in state.pending_remarks if r.remark_id == res.remark_id),
                None,
            )
            if match is None:
                self.log(
                    f"[{side}] resolution for unknown remark "
                    f"#{res.remark_id}; ignoring."
                )
                continue
            state.pending_remarks.remove(match)
            state.resolved_remarks.append(
                ResolvedRemark(
                    remark=match,
                    resolution="accepted" if res.accepted else "rejected",
                    justification=res.justification,
                )
            )

        # New remarks authored by this side.
        for rem in verdict.remarks:
            state.pending_remarks.append(
                StateRemark(
                    remark_id=state.next_remark_id,
                    severity=rem.severity,
                    author=side,
                    text=rem.text,
                )
            )
            state.next_remark_id += 1

        state.sides[side] = replace(
            state.sides[side],
            last_verdict_status=verdict.status,
            last_verdict_artifact_hash=hash_after,
        )
        state.artifact_hash = hash_after
        state.last_actor = side
        if is_round_end:
            state.round += 1
        self.store.save(state)

    # -- adapter invocation --------------------------------------------

    def _invoke(
        self, state: DebateState, side: str, prompt: str, hash_before: str
    ) -> TurnResult:
        """Run one adapter turn, bracketing it with the ``turn_in_progress``
        marker and handling a lost session with a single fresh retry.
        """
        state.turn_in_progress = TurnInProgress(
            side=side, artifact_hash_before=hash_before
        )
        self.store.save(state)

        adapter = self.sides[side]
        session_id = state.sides[side].session_id
        timeout = self.debate.turn_timeout_sec
        try:
            result = adapter.run_turn(prompt, session_id, timeout)
        except SessionLost:
            self.log(f"[{side}] session lost; retrying with a fresh session.")
            result = adapter.run_turn(prompt, None, timeout)

        state.sides[side] = replace(state.sides[side], session_id=result.session_id)
        state.turn_in_progress = None
        self.store.save(state)
        return result

    # -- helpers -------------------------------------------------------

    def _compose_prompt(
        self,
        state: DebateState,
        side: str,
        kind: str,
        task_prompt: str,
        warning: str | None,
    ) -> str:
        catalogs = self._catalogs() if self.require_tasks else None
        if kind == "creation":
            base = build_turn_prompt(
                side_name=side,
                artifact_path=self.artifact_path,
                artifact_hash=None,
                open_remarks=[],
                task_prompt=task_prompt,
                kind="creation",
                catalogs=catalogs,
                require_tasks=self.require_tasks,
            )
        else:
            base = build_turn_prompt(
                side_name=side,
                artifact_path=self.artifact_path,
                artifact_hash=state.artifact_hash,
                open_remarks=list(state.pending_remarks),
                task_prompt=task_prompt,
                kind="turn",
                catalogs=catalogs,
                require_tasks=self.require_tasks,
            )
        if warning:
            return f"{warning}\n\n{base}"
        return base

    def _catalogs(self) -> dict[str, tuple[str, ...]]:
        """Per-side model catalogs (in debate order) for the tasks contract."""
        cfgs = self.side_configs or {}
        return {
            name: cfgs[name].models
            for name in self.order
            if name in cfgs
        }

    def _save_task(self, task_prompt: str) -> None:
        (self.store.spar_dir / _TASK_FILENAME).write_text(task_prompt, encoding="utf-8")

    def _load_task(self) -> str | None:
        path = self.store.spar_dir / _TASK_FILENAME
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def _print_summary(self, state: DebateState, headline: str) -> None:
        self.log(f"\nspar: {headline}.")
        self.log(f"  artifact: {self.artifact_path}")
        self.log(f"  rounds:   {state.round}")
        self.log(f"  resolved remarks: {len(state.resolved_remarks)}")
        pending_nice = [r for r in state.pending_remarks if r.severity == Severity.NICE]
        if pending_nice:
            self.log(f"  open NICE remarks: {len(pending_nice)}")
        for name in self.order:
            side = state.sides[name]
            self.log(f"  {name}: {side.last_verdict_status}")
