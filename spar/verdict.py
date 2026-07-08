"""Verdict module for parsing and analyzing debate outcomes.

Agents end each turn's reply with a structured ``<verdict>...</verdict>``
block. This module parses that block into a frozen ``Verdict`` dataclass;
the orchestrator acts only on what ``parse_verdict`` returns — prose outside
the block (and outside the *last* block, if several are present) is ignored.

Grammar::

    <verdict>
    status: AGREE | CONTINUE
    resolved:
    - #7 accepted
    - #9 rejected: big-bang deliberate, feature flag adds complexity
    remarks:
    - [MUST] No rollback strategy in step 3
    - [NICE] Consider a feature flag instead of big-bang
    </verdict>
"""

import enum
import re
from dataclasses import dataclass


class VerdictError(Exception):
    """Exception raised when a reply's verdict block is missing or malformed."""

    pass


class Severity(enum.Enum):
    """Severity tag for a remark."""

    MUST = "MUST"
    NICE = "NICE"
    USER = "USER"


@dataclass(frozen=True)
class Remark:
    """A single remark raised in a verdict block."""

    severity: Severity
    text: str


@dataclass(frozen=True)
class Resolution:
    """Resolution of a previously-open remark, by id."""

    remark_id: int
    accepted: bool
    justification: str = ""  # non-empty iff rejected


@dataclass(frozen=True)
class Verdict:
    """Fully parsed verdict block."""

    status: str  # "AGREE" | "CONTINUE" | "DONE"
    resolutions: tuple[Resolution, ...]
    remarks: tuple[Remark, ...]

    @property
    def has_new_must(self) -> bool:
        """True if any remark in this verdict is MUST or USER severity."""
        return any(r.severity in (Severity.MUST, Severity.USER) for r in self.remarks)


_VERDICT_BLOCK_RE = re.compile(r"<verdict>(.*?)</verdict>", re.DOTALL)

_STATUS_RE = re.compile(r"^status:\s*(.*)$")
# A trailing empty-list marker (`resolved: []` / `remarks: []`) is how models
# often spell "this section is empty"; accept it as an empty section header.
_RESOLVED_HEADER_RE = re.compile(r"^resolved:\s*(\[\s*\])?\s*$")
_REMARKS_HEADER_RE = re.compile(r"^remarks:\s*(\[\s*\])?\s*$")
# Tolerate an optional trailing note after ``accepted`` (models naturally
# explain why they accepted, e.g. ``#2 accepted: switched to per-target flags``).
# The note is ignored — acceptance carries no justification in the ledger.
_RESOLVED_ACCEPTED_RE = re.compile(r"^#(\d+)\s+accepted\b.*$")
_RESOLVED_REJECTED_RE = re.compile(r"^#(\d+)\s+rejected:\s*(.*)$")
_REMARK_RE = re.compile(r"^\[(MUST|NICE|USER)\]\s*(.*)$")

_VALID_STATUSES = {"AGREE", "CONTINUE", "DONE"}


def _extract_block(reply_text: str) -> str:
    """Return the contents of the last <verdict>...</verdict> block, or raise."""
    matches = list(_VERDICT_BLOCK_RE.finditer(reply_text))
    if matches:
        last = matches[-1]
        # If another <verdict> opening tag appears after the end of the last
        # complete pair, that later block was truncated (never closed) — the
        # reply is malformed and we must not silently fall back to the
        # earlier, possibly-stale complete block.
        if "<verdict>" in reply_text[last.end():]:
            raise VerdictError(
                "unclosed <verdict> block after the last complete block: found a "
                "<verdict> opening tag with no matching </verdict>"
            )
        return last.group(1)
    if "<verdict>" in reply_text:
        raise VerdictError("unclosed <verdict> block: found <verdict> with no matching </verdict>")
    raise VerdictError("no <verdict>...</verdict> block found in reply")


def parse_verdict(reply_text: str) -> Verdict:
    """
    Parse the last <verdict>...</verdict> block found in ``reply_text``.

    Raises:
        VerdictError: on a missing/unclosed block (including a truncated
            trailing block after the last complete one), missing/invalid/
            duplicate status, a malformed resolved/remark entry, or a
            duplicate resolution id.
    """
    block = _extract_block(reply_text)

    # Normalize line endings (CRLF / lone CR) so whitespace-tolerant parsing
    # doesn't have to special-case trailing \r characters.
    normalized = block.replace("\r\n", "\n").replace("\r", "\n")

    status: str | None = None
    resolutions: list[Resolution] = []
    remarks: list[Remark] = []
    seen_ids: set[int] = set()
    section: str | None = None

    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue

        status_match = _STATUS_RE.match(line)
        if status_match:
            if status is not None:
                raise VerdictError("duplicate 'status:' line in verdict block")
            value = status_match.group(1).strip()
            if value not in _VALID_STATUSES:
                raise VerdictError(
                    f"invalid status value: {value!r} "
                    "(expected AGREE, CONTINUE or DONE)"
                )
            status = value
            section = None
            continue

        if _RESOLVED_HEADER_RE.match(line):
            section = "resolved"
            continue

        if _REMARKS_HEADER_RE.match(line):
            section = "remarks"
            continue

        if line.startswith("-"):
            entry = line[1:].strip()

            if section == "resolved":
                resolutions.append(_parse_resolved_entry(entry, seen_ids))
                continue

            if section == "remarks":
                remarks.append(_parse_remark_entry(entry))
                continue

            raise VerdictError(f"verdict entry outside of a resolved/remarks section: {line!r}")

        raise VerdictError(f"unrecognized line in verdict block: {line!r}")

    if status is None:
        raise VerdictError("verdict block is missing required 'status:' line")

    return Verdict(status=status, resolutions=tuple(resolutions), remarks=tuple(remarks))


def _parse_resolved_entry(entry: str, seen_ids: set[int]) -> Resolution:
    accepted_match = _RESOLVED_ACCEPTED_RE.match(entry)
    if accepted_match:
        remark_id = int(accepted_match.group(1))
        _check_duplicate(remark_id, seen_ids)
        return Resolution(remark_id=remark_id, accepted=True)

    rejected_match = _RESOLVED_REJECTED_RE.match(entry)
    if rejected_match:
        remark_id = int(rejected_match.group(1))
        justification = rejected_match.group(2).strip()
        if not justification:
            raise VerdictError(
                f"rejected resolution for #{remark_id} is missing a justification"
            )
        _check_duplicate(remark_id, seen_ids)
        return Resolution(remark_id=remark_id, accepted=False, justification=justification)

    raise VerdictError(f"malformed resolved entry: '- {entry}'")


def _check_duplicate(remark_id: int, seen_ids: set[int]) -> None:
    if remark_id in seen_ids:
        raise VerdictError(f"duplicate resolution for #{remark_id}")
    seen_ids.add(remark_id)


def _parse_remark_entry(entry: str) -> Remark:
    match = _REMARK_RE.match(entry)
    if not match:
        raise VerdictError(f"malformed remark entry: '- {entry}'")

    severity_str, text = match.groups()
    text = text.strip()
    if not text:
        raise VerdictError(f"remark [{severity_str}] has empty text")

    return Remark(severity=Severity[severity_str], text=text)
