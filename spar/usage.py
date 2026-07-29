"""Per-side token accounting, read back off the transcript files.

Both CLIs report their usage in the terminal event of every turn they run --
claude in ``result`` (``usage`` + ``total_cost_usd``), codex in
``turn.completed`` (``usage``) -- and ``run_cli`` already persists every raw
event line under ``.spar/transcript/<side>-<stamp>-<pid>.json``. So the totals
are derivable from files that already exist: no engine plumbing, no state
schema change, and it works retroactively on runs that finished before this
module existed (including a run currently in flight, whose transcript grows
under us).

Token semantics differ between the two CLIs and are normalized here:

* claude's ``input_tokens`` counts only the UNCACHED prompt, with
  ``cache_creation_input_tokens``/``cache_read_input_tokens`` alongside, so the
  prompt total is the sum of all three.
* codex's ``input_tokens`` is already the full prompt, with
  ``cached_input_tokens`` as a subset of it (adding it would double-count).
* codex bills reasoning separately in ``reasoning_output_tokens``; claude's
  ``output_tokens`` already includes its thinking.

:class:`UsageTracker` caches per file and only re-reads files whose
(size, mtime) changed, so a 2s GUI poll over a directory of multi-hundred-KB
transcripts stays cheap.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["SideUsage", "UsageTracker", "format_usage", "usage_from_events"]

_TRANSCRIPT_SUFFIXES = (".json", ".jsonl")


@dataclass
class SideUsage:
    """Normalized totals for one side."""

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    def __add__(self, other: "SideUsage") -> "SideUsage":
        return SideUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            turns=self.turns + other.turns,
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _int(obj: object, key: str) -> int:
    value = obj.get(key) if isinstance(obj, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _usage_from_claude_result(obj: dict) -> SideUsage | None:
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    cache_read = _int(usage, "cache_read_input_tokens")
    cost = obj.get("total_cost_usd")
    return SideUsage(
        input_tokens=(
            _int(usage, "input_tokens")
            + _int(usage, "cache_creation_input_tokens")
            + cache_read
        ),
        output_tokens=_int(usage, "output_tokens"),
        cached_input_tokens=cache_read,
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0,
        turns=1,
    )


def _usage_from_codex_turn(obj: dict) -> SideUsage | None:
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    return SideUsage(
        input_tokens=_int(usage, "input_tokens"),
        output_tokens=_int(usage, "output_tokens") + _int(usage, "reasoning_output_tokens"),
        cached_input_tokens=_int(usage, "cached_input_tokens"),
        turns=1,
    )


def usage_from_events(lines: "list[str] | str") -> SideUsage:
    """Sum the usage of every terminal turn event in a transcript's lines.

    Accepts the file's text or its already-split lines. Unparseable lines and
    events without usage are skipped: a transcript being appended to while we
    read it ends in a half-written line, which must not raise.
    """
    if isinstance(lines, str):
        lines = lines.splitlines()
    total = SideUsage()
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "result":
            found = _usage_from_claude_result(obj)
        elif kind == "turn.completed":
            found = _usage_from_codex_turn(obj)
        else:
            continue
        if found is not None:
            total = total + found
    return total


def side_of_transcript(name: str) -> str:
    """The side a transcript filename belongs to (``claude-<stamp>-<pid>``).

    Non-side helper files the engine writes next to the transcripts (e.g.
    ``orchestrator-...``, ``grill-...``) yield their own bucket name; the
    caller decides which buckets to show.
    """
    return name.split("-", 1)[0]


@dataclass
class UsageTracker:
    """Incremental per-side totals over a ``.spar/transcript`` directory."""

    transcript_dir: Path
    _cache: dict[str, tuple[tuple[int, int], SideUsage]] = field(default_factory=dict)

    def refresh(self) -> dict[str, SideUsage]:
        """Re-scan changed/new transcripts; return totals keyed by side."""
        directory = Path(self.transcript_dir)
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            return {}

        seen: set[str] = set()
        for path in entries:
            if path.suffix not in _TRANSCRIPT_SUFFIXES:
                continue
            key = path.name
            seen.add(key)
            try:
                stat = path.stat()
            except OSError:
                continue
            stamp = (stat.st_size, stat.st_mtime_ns)
            cached = self._cache.get(key)
            if cached is not None and cached[0] == stamp:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self._cache[key] = (stamp, usage_from_events(text))

        for stale in set(self._cache) - seen:
            del self._cache[stale]

        totals: dict[str, SideUsage] = {}
        for key, (_stamp, usage) in self._cache.items():
            if not usage.turns:
                continue
            side = side_of_transcript(key)
            totals[side] = totals.get(side, SideUsage()) + usage
        return totals


def _compact(count: int) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return f"{count}"


def format_usage(totals: dict[str, SideUsage], order: "list[str] | None" = None) -> str:
    """One-line summary for a status bar; empty string when nothing is known.

    ``order`` fixes the side order (the configured debate order); any extra
    bucket found on disk is appended after it, alphabetically.
    """
    if not totals:
        return ""
    order = order or []
    names = [s for s in order if s in totals] + sorted(set(totals) - set(order))
    parts = []
    for side in names:
        usage = totals[side]
        piece = f"{side} {_compact(usage.input_tokens)}↓ {_compact(usage.output_tokens)}↑"
        if usage.cost_usd:
            piece += f" ${usage.cost_usd:.2f}"
        parts.append(piece)
    return "tokeny: " + "  ·  ".join(parts)
