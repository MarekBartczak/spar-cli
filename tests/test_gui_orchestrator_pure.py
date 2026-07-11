"""Pure (Qt-free) tests for build_gate_context — NO importorskip (review #12).

These must RUN, not skip, under a plain ``python3`` interpreter: the helper
lives above the ``if _HAS_QT:`` guard in spar/gui/orchestrator.py.
"""
from __future__ import annotations

from spar.gui.orchestrator import OPENING_PROMPT, build_gate_context, parse_task_draft


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


class TestOpeningPromptConversational:
    """Live smoke defect 2: the prompt must ask for a NORMAL conversation —
    no self-introduction, no volunteered lettered menus on every reply."""

    def test_instructs_natural_conversation(self):
        assert "Prowadź NORMALNĄ rozmowę" in OPENING_PROMPT
        assert "NIE przedstawiaj się" in OPENING_PROMPT
        assert "NIE proponuj menu opcji z własnej inicjatywy" in OPENING_PROMPT
        # Greetings get a one-line greeting back, nothing more.
        assert "jednym zdaniem" in OPENING_PROMPT

    def test_options_reserved_for_genuine_choices_only(self):
        assert "rezerwuj WYŁĄCZNIE" in OPENING_PROMPT
        assert "wybrał między konkretnymi alternatywami" in OPENING_PROMPT
        # The lettered format itself survives (the GUI renders buttons off it).
        assert "LITERAMI" in OPENING_PROMPT and "A., B., C." in OPENING_PROMPT

    def test_no_always_offer_options_instruction(self):
        # The old wording made the model answer even "cześć" with an A/B/C
        # menu. It must be gone.
        assert "proponujesz opcje, oznaczaj je" not in OPENING_PROMPT

    def test_read_only_contract_untouched(self):
        assert "TYLKO-DO-ODCZYTU" in OPENING_PROMPT
        assert "NIGDY nie podejmujesz decyzji" in OPENING_PROMPT


class TestParseTaskDraft:
    def test_none_when_absent(self):
        assert parse_task_draft("zwykła odpowiedź") is None

    def test_opening_prompt_format_example_parses(self):
        # Review #31: prompt/parser contract — the multiline format example
        # embedded verbatim in OPENING_PROMPT must itself parse with
        # parse_task_draft, so the prompt can never teach the model a
        # draft format the parser rejects.
        assert parse_task_draft(OPENING_PROMPT) == "<treść szkicu zadania>"

    def test_extracts_fenced_block(self):
        reply = "Oto szkic:\n\n```zadanie\nZbuduj X\n\n## Tasks\n- a\n```\ndaj znać"
        assert parse_task_draft(reply) == "Zbuduj X\n\n## Tasks\n- a"

    def test_last_block_wins(self):
        reply = "```zadanie\nstary\n```\n...\n```zadanie\nnowy\n```"
        assert parse_task_draft(reply) == "nowy"
