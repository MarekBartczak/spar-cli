"""Pure (Qt-free) tests for build_gate_context — NO importorskip (review #12).

These must RUN, not skip, under a plain ``python3`` interpreter: the helper
lives above the ``if _HAS_QT:`` guard in spar/gui/orchestrator.py.
"""
from __future__ import annotations

from spar.gui.orchestrator import build_gate_context


class TestGateContext:
    def test_empty_when_no_gate(self):
        assert build_gate_context(None) == ""

    def test_includes_type_task_and_summary(self):
        gate = {"name": "review_rounds", "options": ["accept", "abort"],
                "context": {"task_id": "t3", "summary": "FAILED: 2 tests"}}
        out = build_gate_context(gate)
        assert "review_rounds" in out and "t3" in out and "FAILED: 2 tests" in out
        assert "NIE podejmuj decyzji" in out

    def test_includes_open_remarks_failing_output(self):
        # Review #10: the failing per-task-test output lives in open_remarks,
        # NOT in a top-level summary. build_gate_context must render it.
        gate = {"name": "review_rounds", "context": {
            "task_id": "t1", "rounds": 3, "reason": "test_escalation",
            "command": "pytest -q",
            "open_remarks": [
                {"id": 0, "severity": "USER", "author": "per-task-test",
                 "text": "per-task test FAILING. Last captured output:\nE assert 1 == 2"},
            ],
        }}
        out = build_gate_context(gate)
        assert "test_escalation" in out
        assert "pytest -q" in out
        assert "E assert 1 == 2" in out
        assert "per-task-test" in out

    def test_includes_nice_backlog_remarks(self):
        gate = {"name": "consensus", "context": {
            "artifact": "docs/plan.md",
            "nice_backlog": [
                {"id": 1, "severity": "NICE", "author": "review", "text": "tidy names"},
            ],
        }}
        out = build_gate_context(gate)
        assert "docs/plan.md" in out and "tidy names" in out

    def test_truncates_long_output(self):
        gate = {"name": "g", "context": {"summary": "x" * 5000}}
        # summary truncated to 2000 chars -> whole block well under 2500.
        assert len(build_gate_context(gate)) < 2500

    def test_truncates_long_remark_text(self):
        gate = {"name": "g", "context": {
            "open_remarks": [{"severity": "USER", "author": "a", "text": "y" * 5000}],
        }}
        assert build_gate_context(gate).count("y") <= 2000
