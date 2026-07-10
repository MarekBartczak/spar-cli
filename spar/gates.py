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

    action: str  # "accept" | "abort" | "extend" | "remarks" | "fix"
    extra_rounds: int = 0  # > 0 iff action == "extend"
    remarks: tuple[str, ...] = ()  # non-empty iff action == "remarks"
    command: str = ""  # non-empty iff action == "fix" (the replacement test cmd)


def parse_gate_value(value: str) -> GateChoice:
    """Parse ``accept`` / ``abort`` / ``extend:<n>`` / ``remarks:<file>`` /
    ``fix:<command>``.

    ``remarks:<file>`` reads the file (UTF-8); each non-empty line is one
    remark. ``fix:<command>`` carries a replacement per-task test command —
    the value is split on the FIRST colon only, so the command itself may
    contain spaces and colons (e.g. ``fix:python3 -m py_compile a.py``).
    Raises :class:`GateParseError` on bad syntax, n < 1, an unreadable/empty
    remarks file, or an empty ``fix:`` command.
    """
    if value == "accept":
        return GateChoice(action="accept")
    if value == "abort":
        return GateChoice(action="abort")
    if value.startswith("fix:"):
        command = value[len("fix:"):].strip()
        if not command:
            raise GateParseError("fix needs a non-empty command, e.g. fix:python3 -m py_compile a.py")
        return GateChoice(action="fix", command=command)
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
        f"unknown --gate value {value!r} (expected accept, abort, extend:<n>, "
        "remarks:<file> or fix:<command>)"
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
