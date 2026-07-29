"""Tests for spar/usage.py: per-side token accounting off the transcripts."""

import json

from spar.usage import SideUsage, UsageTracker, format_usage, usage_from_events


def _claude_result(inp, cache_create, cache_read, out, cost=None):
    return json.dumps({
        "type": "result",
        "total_cost_usd": cost,
        "usage": {
            "input_tokens": inp,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
            "output_tokens": out,
        },
    })


def _codex_turn(inp, cached, out, reasoning):
    return json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": inp,
            "cached_input_tokens": cached,
            "output_tokens": out,
            "reasoning_output_tokens": reasoning,
        },
    })


def test_claude_prompt_total_sums_uncached_and_cache_tokens():
    # claude's input_tokens counts ONLY the uncached prompt.
    usage = usage_from_events([_claude_result(14, 4700, 443182, 1405, 0.18)])
    assert usage.input_tokens == 14 + 4700 + 443182
    assert usage.output_tokens == 1405
    assert usage.cached_input_tokens == 443182
    assert usage.cost_usd == 0.18
    assert usage.turns == 1


def test_codex_input_tokens_are_not_double_counted():
    # codex's input_tokens ALREADY includes cached_input_tokens.
    usage = usage_from_events([_codex_turn(460674, 403712, 6566, 4372)])
    assert usage.input_tokens == 460674
    assert usage.cached_input_tokens == 403712
    # reasoning is billed as output on codex
    assert usage.output_tokens == 6566 + 4372


def test_multiple_turns_accumulate():
    usage = usage_from_events([
        _codex_turn(100, 0, 10, 0),
        _codex_turn(200, 0, 20, 5),
    ])
    assert usage.input_tokens == 300
    assert usage.output_tokens == 35
    assert usage.turns == 2


def test_half_written_and_irrelevant_lines_are_skipped():
    # A transcript being appended to while we read it ends mid-line.
    usage = usage_from_events([
        '{"type":"stream_event","event":{}}',
        _codex_turn(50, 0, 5, 0),
        '{"type":"turn.compl',
        "",
    ])
    assert usage.turns == 1
    assert usage.input_tokens == 50


def test_events_without_usage_do_not_count_as_turns():
    usage = usage_from_events(['{"type":"result"}', '{"type":"turn.completed"}'])
    assert usage == SideUsage()


def test_tracker_buckets_by_side_and_skips_non_transcripts(tmp_path):
    d = tmp_path / "transcript"
    d.mkdir()
    (d / "claude-1-99.json").write_text(_claude_result(1, 0, 9, 100, 0.5), encoding="utf-8")
    (d / "codex-1-99.jsonl").write_text(_codex_turn(500, 100, 50, 0), encoding="utf-8")
    (d / "codex-last-1-99.md").write_text("not a transcript", encoding="utf-8")

    totals = UsageTracker(d).refresh()

    assert set(totals) == {"claude", "codex"}
    assert totals["claude"].input_tokens == 10
    assert totals["claude"].cost_usd == 0.5
    assert totals["codex"].input_tokens == 500


def test_tracker_rereads_only_changed_files(tmp_path):
    d = tmp_path / "transcript"
    d.mkdir()
    path = d / "codex-1-99.jsonl"
    path.write_text(_codex_turn(100, 0, 10, 0), encoding="utf-8")

    tracker = UsageTracker(d)
    assert tracker.refresh()["codex"].input_tokens == 100

    reads = []
    original = type(path).read_text

    def counting_read_text(self, *args, **kwargs):
        reads.append(self.name)
        return original(self, *args, **kwargs)

    type(path).read_text = counting_read_text
    try:
        tracker.refresh()  # nothing changed -> no re-read
        assert reads == []
        path.write_text(
            _codex_turn(100, 0, 10, 0) + "\n" + _codex_turn(7, 0, 3, 0), encoding="utf-8"
        )
        totals = tracker.refresh()
        assert reads == ["codex-1-99.jsonl"]
    finally:
        type(path).read_text = original

    assert totals["codex"].input_tokens == 107
    assert totals["codex"].turns == 2


def test_tracker_forgets_deleted_transcripts(tmp_path):
    d = tmp_path / "transcript"
    d.mkdir()
    path = d / "claude-1-99.json"
    path.write_text(_claude_result(1, 0, 0, 5), encoding="utf-8")

    tracker = UsageTracker(d)
    assert "claude" in tracker.refresh()
    path.unlink()
    assert tracker.refresh() == {}


def test_tracker_on_missing_directory_is_empty(tmp_path):
    assert UsageTracker(tmp_path / "nope").refresh() == {}


def test_format_usage_orders_configured_sides_first():
    totals = {
        "grill": SideUsage(input_tokens=1000, output_tokens=10),
        "codex": SideUsage(input_tokens=2_000_000, output_tokens=2000),
        "claude": SideUsage(input_tokens=3000, output_tokens=300, cost_usd=1.5),
    }
    text = format_usage(totals, ["claude", "codex"])
    assert text.startswith("tokeny: claude 3k↓ 300↑ $1.50")
    assert text.index("claude") < text.index("codex") < text.index("grill")
    assert "2.0M↓" in text


def test_format_usage_empty_when_nothing_known():
    assert format_usage({}) == ""
