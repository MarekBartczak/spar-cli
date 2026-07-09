"""Shared headless-gate plumbing: the control exception and --gate parsing."""

from __future__ import annotations

from dataclasses import dataclass


class GateParseError(Exception):
    """Raised for an unparsable or mismatched --gate value."""


class GatePending(Exception):
    """Control-flow signal: a user gate was reached in headless mode.

    Carries everything the runner must persist so ``spar status --json``
    can describe the gate and a resume can validate the decision.
    """

    def __init__(self, name: str, options: list[str], context: dict | None = None) -> None:
        super().__init__(f"gate pending: {name}")
        self.name = name
        self.options = list(options)
        self.context = dict(context or {})

    def to_state(self) -> dict:
        return {"name": self.name, "options": self.options, "context": self.context}


@dataclass(frozen=True)
class GateChoice:
    """A parsed --gate value, not yet validated against a pending gate."""

    action: str  # "accept" | "abort" | "extend" | "remarks"
    extra_rounds: int = 0  # > 0 iff action == "extend"
    remarks: tuple[str, ...] = ()  # non-empty iff action == "remarks"


def parse_gate_value(value: str) -> GateChoice:
    """Parse ``accept`` / ``abort`` / ``extend:<n>`` / ``remarks:<file>``.

    ``remarks:<file>`` reads the file (UTF-8); each non-empty line is one
    remark. Raises :class:`GateParseError` on bad syntax, n < 1, an
    unreadable/empty remarks file.
    """
    if value == "accept":
        return GateChoice(action="accept")
    if value == "abort":
        return GateChoice(action="abort")
    if value.startswith("extend:"):
        raw = value[len("extend:"):]
        try:
            n = int(raw)
        except ValueError:
            raise GateParseError(f"extend needs an integer, got {raw!r}")
        if n < 1:
            raise GateParseError(f"extend needs a positive integer, got {n}")
        return GateChoice(action="extend", extra_rounds=n)
    if value.startswith("remarks:"):
        path = value[len("remarks:"):]
        try:
            text = open(path, encoding="utf-8").read()
        except OSError as exc:
            raise GateParseError(f"cannot read remarks file {path!r}: {exc}")
        remarks = tuple(ln.strip() for ln in text.splitlines() if ln.strip())
        if not remarks:
            raise GateParseError(f"remarks file {path!r} contains no remarks")
        return GateChoice(action="remarks", remarks=remarks)
    raise GateParseError(
        f"unknown --gate value {value!r} (expected accept, abort, extend:<n> or remarks:<file>)"
    )


def validate_choice(choice: GateChoice, pending: dict | None) -> None:
    """Check ``choice`` against the persisted pending-gate record.

    Raises GateParseError when there is no pending gate or the action is
    not among the gate's options.
    """
    if pending is None:
        raise GateParseError("no gate is pending; --gate is not applicable")
    if choice.action not in pending.get("options", []):
        raise GateParseError(
            f"gate {pending.get('name')!r} accepts {pending.get('options')}, "
            f"got {choice.action!r}"
        )
