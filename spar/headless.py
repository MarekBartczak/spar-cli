"""Headless (agent-mode) debate gate.

In interactive mode :class:`spar.orchestrator.ConsoleGate` blocks on stdin at
each user decision point. In headless mode there is no human to prompt, so
the orchestrator must instead *exit* at a gate and *resume* once the operator
supplies a decision via ``--gate``. :class:`HeadlessGate` implements that
contract, mirroring :class:`spar.exec.headless.HeadlessExecGate` for the
debate side's :class:`~spar.orchestrator.UserGate` protocol:

- With no preloaded decision, ``consensus_gate``/``rounds_exhausted_gate``
  raise :class:`spar.gates.GatePending` — the control signal the Orchestrator
  catches to persist the pending gate and exit 10.
- With a preloaded decision matching the gate being reached, the method
  *consumes* it: it clears the preload, invokes ``on_consume`` (the
  Orchestrator's callback that clears ``state.pending_gate`` and saves —
  clearing happens AT consumption, never at preload) and returns the
  :class:`~spar.orchestrator.GateDecision`.
- ``recovery_gate`` always returns ``"repeat"`` — a safe default that never
  pends: there is no interesting decision to defer here, since we keep no
  backup copies of the artifact.
"""

from __future__ import annotations

from pathlib import Path

from spar.gates import GatePending
from spar.orchestrator import GateDecision
from spar.state import StateRemark

__all__ = ["HeadlessGate"]


class HeadlessGate:
    """A :class:`~spar.orchestrator.UserGate` that pends (exit 10) or consumes
    a single preloaded decision.

    ``preloaded`` is ``(gate_name, GateDecision)`` or ``None``. ``on_consume``
    is an optional zero-arg callback run the moment the preloaded decision is
    consumed (the Orchestrator wires it to clear ``state.pending_gate`` and
    save).
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

    def consensus_gate(
        self, artifact_path: Path, nice_backlog: list[StateRemark]
    ) -> GateDecision:
        context = {
            "artifact": str(artifact_path),
            "nice_backlog": [
                {
                    "id": r.remark_id,
                    "severity": r.severity.name,
                    "author": r.author,
                    "text": r.text,
                }
                for r in nice_backlog
            ],
        }
        return self._consume_or_pend(
            "consensus", ["accept", "remarks", "abort"], context
        )

    def rounds_exhausted_gate(
        self, artifact_path: Path, pending: list[StateRemark]
    ) -> GateDecision:
        context = {
            "artifact": str(artifact_path),
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
        return self._consume_or_pend(
            "rounds_exhausted", ["accept", "extend", "abort"], context
        )

    def recovery_gate(self, artifact_path: Path, expected_hash: str) -> str:
        # Safe default: never pends. The interrupted turn is simply repeated
        # on top of whatever is currently on disk (no backup copies are kept).
        return "repeat"
