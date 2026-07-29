"""Tests for spar/automode.py: which pending gates answer themselves."""

from spar.automode import (
    AUTO_EXTEND_LIMIT,
    AUTO_EXTEND_ROUNDS,
    auto_gate_decision,
    gate_extend_key,
)


def _gate(name, options, **context):
    return {"name": name, "options": options, "context": context}


class TestExtendableGates:
    def test_rounds_exhausted_adds_two_rounds(self):
        decision = auto_gate_decision(_gate("rounds_exhausted", ["accept", "extend", "abort"]))
        assert decision.value == f"extend:{AUTO_EXTEND_ROUNDS}"
        assert "adding 2 rounds" in decision.reason

    def test_review_dispute_adds_two_rounds(self):
        gate = _gate(
            "review_rounds", ["accept", "extend", "abort"],
            task_id="t3", rounds=3, reason="review_dispute",
        )
        assert auto_gate_decision(gate).value == f"extend:{AUTO_EXTEND_ROUNDS}"

    def test_extension_is_capped_then_accepts_instead_of_asking(self):
        gate = _gate("rounds_exhausted", ["accept", "extend", "abort"])
        assert auto_gate_decision(gate, AUTO_EXTEND_LIMIT - 1).value == "extend:2"
        capped = auto_gate_decision(gate, AUTO_EXTEND_LIMIT)
        assert capped.value == "accept"  # never parks waiting for a click
        assert "not converging" in capped.reason

    def test_gate_without_extend_option_accepts(self):
        gate = _gate("rounds_exhausted", ["accept", "abort"])
        assert auto_gate_decision(gate).value == "accept"


class TestNothingIsLeftHanging:
    def test_broken_task_test_is_accepted_not_extended(self):
        # More rounds re-run the SAME failing command and re-escalate, so
        # extending would only burn turns.
        gate = _gate(
            "review_rounds", ["accept", "extend", "fix", "abort"],
            task_id="t2", rounds=2, reason="test_escalation", command="yarn test",
        )
        decision = auto_gate_decision(gate)
        assert decision.value == "accept"
        assert "yarn test" in decision.reason

    def test_final_merge_is_accepted(self):
        assert auto_gate_decision(_gate("final_merge", ["accept", "abort"])).value == "accept"

    def test_abort_is_never_chosen(self):
        for gate in (
            _gate("rounds_exhausted", ["accept", "extend", "abort"]),
            _gate("review_rounds", ["accept", "extend", "abort"], reason="review_dispute"),
            _gate("review_rounds", ["accept", "extend", "fix", "abort"], reason="test_escalation"),
            _gate("consensus", ["accept", "remarks", "abort"]),
            _gate("final_merge", ["accept", "abort"]),
        ):
            for used in (0, AUTO_EXTEND_LIMIT):
                assert auto_gate_decision(gate, used).value != "abort"

    def test_gate_offering_no_accept_is_deferred(self):
        # Nothing sane to pick: not asking would mean aborting.
        gate = _gate("final_merge", ["abort"])
        assert auto_gate_decision(gate).value is None

    def test_unknown_gate_name_is_deferred(self):
        assert auto_gate_decision(_gate("brand_new_gate", ["accept"])).value is None

    def test_no_gate_is_a_no_op(self):
        assert auto_gate_decision(None).value is None


class TestConsensus:
    def test_consensus_accepts_and_chains_exec(self):
        decision = auto_gate_decision(_gate("consensus", ["accept", "remarks", "abort"]))
        assert decision.value == "accept"
        assert decision.auto_exec is True

    def test_consensus_without_exec_chaining(self):
        decision = auto_gate_decision(
            _gate("consensus", ["accept", "remarks", "abort"]),
            start_exec_on_consensus=False,
        )
        assert decision.value == "accept"
        assert decision.auto_exec is False


class TestExtendKey:
    def test_key_ignores_rounds_so_the_cap_actually_caps(self):
        # Every extension raises `rounds`; keying on it would reset the counter.
        first = _gate("review_rounds", ["extend"], task_id="t3", rounds=3)
        later = _gate("review_rounds", ["extend"], task_id="t3", rounds=5)
        assert gate_extend_key(first) == gate_extend_key(later)

    def test_key_separates_tasks_and_gate_names(self):
        a = _gate("review_rounds", ["extend"], task_id="t3")
        b = _gate("review_rounds", ["extend"], task_id="t4")
        c = _gate("rounds_exhausted", ["extend"])
        assert len({gate_extend_key(a), gate_extend_key(b), gate_extend_key(c)}) == 3

    def test_no_gate_has_an_empty_key(self):
        assert gate_extend_key(None) == ""
