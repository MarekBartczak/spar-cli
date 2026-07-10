"""Headless (agent-mode) execution gate.

In interactive mode :class:`spar.exec.loop.ConsoleExecGate` blocks on stdin at
each user decision point. In headless mode there is no human to prompt, so the
runner must instead *exit* at a gate and *resume* once the operator supplies a
decision via ``--gate``. :class:`HeadlessExecGate` implements that contract:

- With no preloaded decision, every gate method raises
  :class:`spar.gates.GatePending` — the control signal the Executor catches to
  persist the pending gate and exit 10.
- With a preloaded decision matching the gate being reached, the method
  *consumes* it: it clears the preload, invokes ``on_consume`` (the Executor's
  callback that clears ``state.pending_gate`` and saves — clearing happens AT
  consumption, never at preload) and returns the :class:`GateDecision`.

Single-owner rule: only the ``final_merge`` gate is consumed through this
object's preload. The ``review_rounds`` gate is resumed by the Executor itself
(via ``self._resume_decision``), so this object always *pends* it — which is
exactly what a fresh (non-preloaded) gate method does.
"""

from __future__ import annotations

from spar.gates import GatePending
from spar.orchestrator import GateDecision

__all__ = ["HeadlessExecGate"]


class HeadlessExecGate:
    """An :class:`~spar.exec.loop.ExecGate` that pends (exit 10) or consumes a
    single preloaded decision.

    ``preloaded`` is ``(gate_name, GateDecision)`` or ``None``. ``on_consume``
    is an optional zero-arg callback run the moment the preloaded decision is
    consumed (the Executor wires it to clear ``pending_gate`` + save).
    """

    def __init__(
        self,
        preloaded: tuple[str, GateDecision] | None = None,
        on_consume=None,
    ) -> None:
        self.preloaded = preloaded
        self.on_consume = on_consume

    def _consume_or_pend(
        self, name: str, options: list[str], context: dict
    ) -> GateDecision:
        if self.preloaded is not None and self.preloaded[0] == name:
            decision = self.preloaded[1]
            self.preloaded = None
            if self.on_consume is not None:
                self.on_consume()
            return decision
        raise GatePending(name, options, context)

    def final_merge_gate(self, summary: str) -> GateDecision:
        return self._consume_or_pend(
            "final_merge", ["accept", "abort"], {"summary": summary}
        )

    def review_rounds_exhausted_gate(
        self,
        task_id: str,
        rounds: int,
        pending: list,
        *,
        allow_fix: bool = False,
        command: str | None = None,
    ) -> GateDecision:
        context = {
            "task_id": task_id,
            "rounds": rounds,
            "open_remarks": [
                {
                    "id": r.remark_id,
                    "severity": r.severity.name,
                    "author": r.author,
                    "text": r.text,
                }
                for r in pending
            ],
        }
        # ``fix`` is offered only for a test escalation (a broken/failing
        # per-task test command); the current command rides into the context so
        # the operator (and the GUI's prefill) can see what they are replacing.
        options = ["accept", "extend", "abort"]
        if allow_fix:
            options = ["accept", "extend", "fix", "abort"]
            if command is not None:
                context["command"] = command
        return self._consume_or_pend("review_rounds", options, context)
