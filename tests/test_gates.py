"""Tests for spar.gates: GatePending, --gate value parsing, and validation."""

import pytest

from spar.gates import (
    GateChoice,
    GateParseError,
    GatePending,
    parse_gate_value,
    validate_choice,
)


class TestParseGateValue:
    def test_accept(self):
        assert parse_gate_value("accept") == GateChoice(action="accept")

    def test_abort(self):
        assert parse_gate_value("abort") == GateChoice(action="abort")

    def test_extend(self):
        choice = parse_gate_value("extend:3")
        assert choice.action == "extend"
        assert choice.extra_rounds == 3

    def test_remarks_from_file(self, tmp_path):
        remarks_file = tmp_path / "remarks.txt"
        remarks_file.write_text("first remark\nsecond remark\n\n", encoding="utf-8")
        choice = parse_gate_value(f"remarks:{remarks_file}")
        assert choice.action == "remarks"
        assert choice.remarks == ("first remark", "second remark")

    def test_extend_non_integer_raises(self):
        with pytest.raises(GateParseError):
            parse_gate_value("extend:x")

    def test_extend_zero_raises(self):
        with pytest.raises(GateParseError):
            parse_gate_value("extend:0")

    def test_extend_negative_raises(self):
        with pytest.raises(GateParseError):
            parse_gate_value("extend:-1")

    def test_remarks_nonexistent_file_raises(self):
        with pytest.raises(GateParseError):
            parse_gate_value("remarks:/nonexistent/path/remarks.txt")

    def test_remarks_empty_file_raises(self, tmp_path):
        remarks_file = tmp_path / "empty.txt"
        remarks_file.write_text("\n\n   \n", encoding="utf-8")
        with pytest.raises(GateParseError):
            parse_gate_value(f"remarks:{remarks_file}")

    def test_junk_value_raises(self):
        with pytest.raises(GateParseError):
            parse_gate_value("not-a-real-value")


class TestValidateChoice:
    def test_happy_path(self):
        choice = GateChoice(action="accept")
        pending = {"name": "review-gate", "options": ["accept", "abort"], "context": {}}
        validate_choice(choice, pending)  # should not raise

    def test_no_pending_raises(self):
        choice = GateChoice(action="accept")
        with pytest.raises(GateParseError):
            validate_choice(choice, None)

    def test_action_not_in_options_raises(self):
        choice = GateChoice(action="abort")
        pending = {"name": "review-gate", "options": ["accept", "extend"], "context": {}}
        with pytest.raises(GateParseError):
            validate_choice(choice, pending)


class TestGatePending:
    def test_to_state(self):
        exc = GatePending("review-gate", ["accept", "abort"], {"round": 3})
        assert exc.to_state() == {
            "name": "review-gate",
            "options": ["accept", "abort"],
            "context": {"round": 3},
        }

    def test_defaults(self):
        exc = GatePending("review-gate", ["accept"])
        assert exc.context == {}
        assert exc.options == ["accept"]
