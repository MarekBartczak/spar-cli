"""Auto-mode gate policy: how a pending gate answers itself.

A headless run stops at a gate and exits 10; someone then resumes it with
``--gate <value>``. Auto mode is "do not ask me, keep going": every gate this
module knows gets an answer, and the run never parks waiting for a click.

The answers:

* ``rounds_exhausted`` (debate ran out of rounds, no consensus) -> ``extend:2``
* ``review_rounds`` with reason ``review_dispute`` (implementer and reviewer
  still disagree) -> ``extend:2``
* ``review_rounds`` with reason ``test_escalation`` -> ``accept``. The task's
  own ``test=`` command is broken or failing, and MORE review rounds cannot fix
  a command: the next round re-runs the same command and re-escalates. So
  rounds would only burn turns; the run moves on instead. The global test
  command and the final merge still gate the whole body of work.
* ``consensus`` (both sides AGREE, only NICE remarks left) -> ``accept``
* ``final_merge`` -> ``accept``

Extension is capped at :data:`AUTO_EXTEND_LIMIT` per gate. Hitting the cap does
NOT stop and ask -- it falls through to ``accept``, because a debate that will
not converge must still end somewhere. ``abort`` is never chosen: auto mode
makes progress or it accepts, it never throws work away.

The only thing still left to a human is a gate name this module has never seen
(no way to guess what its options mean) -- and that is reported, not silent.
"""

from __future__ import annotations

__all__ = [
    "AUTO_EXTEND_LIMIT",
    "AUTO_EXTEND_ROUNDS",
    "AutoDecision",
    "auto_gate_decision",
    "gate_extend_key",
]

# Rounds added per automatic extension.
AUTO_EXTEND_ROUNDS = 2

# How many times auto mode may extend the SAME gate before deferring to a human.
AUTO_EXTEND_LIMIT = 3

_EXTENDABLE = ("rounds_exhausted", "review_rounds")


class AutoDecision:
    """What auto mode wants to do about a pending gate.

    ``value`` is a ``--gate`` value (``"accept"``, ``"extend:2"``, ...) or
    ``None`` when the gate must be left to the user. ``reason`` always
    explains the outcome -- it is surfaced in the live log so an automatic
    answer is never silent, and so a deferral says WHY it is waiting.
    """

    __slots__ = ("value", "reason", "auto_exec")

    def __init__(self, value: str | None, reason: str, auto_exec: bool = False) -> None:
        self.value = value
        self.reason = reason
        self.auto_exec = auto_exec

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AutoDecision):
            return NotImplemented
        return (
            self.value == other.value
            and self.reason == other.reason
            and self.auto_exec == other.auto_exec
        )

    def __repr__(self) -> str:  # pragma: no cover (debug aid)
        return f"AutoDecision({self.value!r}, {self.reason!r}, auto_exec={self.auto_exec})"


def gate_extend_key(gate: dict | None) -> str:
    """Key counting extensions of "the same gate".

    Deliberately excludes ``rounds``: every extension raises the round count,
    so keying on it would reset the cap on each pass and never stop extending.
    """
    if not gate:
        return ""
    name = f"{gate.get('name')}"
    task_id = f"{(gate.get('context') or {}).get('task_id') or ''}"
    return f"{name}:{task_id}"


def auto_gate_decision(
    gate: dict | None,
    extends_used: int = 0,
    *,
    start_exec_on_consensus: bool = True,
) -> AutoDecision:
    """Decide how auto mode answers ``gate`` (a persisted pending-gate record).

    ``extends_used`` is how many times auto mode already extended this gate
    (see :func:`gate_extend_key`). ``start_exec_on_consensus`` mirrors the
    GUI's "Accept -> start exec" button: an accepted plan chains straight into
    execution instead of stopping with an agreed plan and nothing running.
    """
    if not gate:
        return AutoDecision(None, "no gate pending")

    name = gate.get("name")
    options = list(gate.get("options") or [])
    context = gate.get("context") or {}

    if name == "consensus":
        if "accept" not in options:
            return AutoDecision(None, f"gate 'consensus' has no accept option: {options}")
        return AutoDecision(
            "accept",
            "consensus reached — accepting automatically",
            auto_exec=start_exec_on_consensus,
        )

    if name in _EXTENDABLE:
        reason = context.get("reason")
        if name == "review_rounds" and reason == "test_escalation":
            # More rounds cannot fix a broken/failing test COMMAND -- the next
            # round runs the same command and escalates again. Accept and move
            # on; the global test command still gates the whole run.
            command = context.get("command")
            detail = f" ({command})" if command else ""
            return _accept_or_defer(
                options,
                name,
                f"task's own test= is broken or failing{detail} — rounds cannot fix "
                "a command, accepting and moving on",
            )
        if extends_used >= AUTO_EXTEND_LIMIT:
            return _accept_or_defer(
                options,
                name,
                f"extended {extends_used}x already (limit {AUTO_EXTEND_LIMIT}) — "
                "not converging, accepting to keep going",
            )
        if "extend" not in options:
            return _accept_or_defer(
                options, name, f"gate has no extend option ({options}) — accepting"
            )
        return AutoDecision(
            f"extend:{AUTO_EXTEND_ROUNDS}",
            f"no resolution yet — adding {AUTO_EXTEND_ROUNDS} rounds "
            f"({extends_used + 1}/{AUTO_EXTEND_LIMIT})",
        )

    if name == "final_merge":
        return _accept_or_defer(options, name, "final merge — accepting")

    return AutoDecision(None, f"gate {name!r} is unknown to auto mode — decide yourself")


def _accept_or_defer(options: list, name: str, reason: str) -> AutoDecision:
    """``accept`` when the gate offers it, otherwise defer (never ``abort``)."""
    if "accept" not in options:
        return AutoDecision(None, f"gate {name!r} offers no accept: {options}")
    return AutoDecision("accept", reason)
