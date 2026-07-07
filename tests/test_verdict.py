"""Tests for the spar verdict block parser."""

import pytest

from spar.verdict import (
    Severity,
    Remark,
    Resolution,
    Verdict,
    VerdictError,
    parse_verdict,
)


class TestDataclasses:
    """Test frozen dataclasses creation and basic behavior."""

    def test_remark_creation(self):
        remark = Remark(severity=Severity.MUST, text="No rollback strategy")
        assert remark.severity == Severity.MUST
        assert remark.text == "No rollback strategy"

    def test_remark_frozen(self):
        remark = Remark(severity=Severity.NICE, text="consider X")
        with pytest.raises(Exception):
            remark.text = "changed"

    def test_resolution_defaults(self):
        resolution = Resolution(remark_id=7, accepted=True)
        assert resolution.remark_id == 7
        assert resolution.accepted is True
        assert resolution.justification == ""

    def test_resolution_frozen(self):
        resolution = Resolution(remark_id=7, accepted=True)
        with pytest.raises(Exception):
            resolution.accepted = False

    def test_verdict_frozen(self):
        verdict = Verdict(status="AGREE", resolutions=(), remarks=())
        with pytest.raises(Exception):
            verdict.status = "CONTINUE"

    def test_verdict_has_new_must_false_when_no_remarks(self):
        verdict = Verdict(status="AGREE", resolutions=(), remarks=())
        assert verdict.has_new_must is False

    def test_verdict_has_new_must_true_for_must(self):
        verdict = Verdict(
            status="CONTINUE",
            resolutions=(),
            remarks=(Remark(severity=Severity.MUST, text="x"),),
        )
        assert verdict.has_new_must is True

    def test_verdict_has_new_must_true_for_user(self):
        verdict = Verdict(
            status="CONTINUE",
            resolutions=(),
            remarks=(Remark(severity=Severity.USER, text="x"),),
        )
        assert verdict.has_new_must is True

    def test_verdict_has_new_must_false_for_nice_only(self):
        verdict = Verdict(
            status="AGREE",
            resolutions=(),
            remarks=(Remark(severity=Severity.NICE, text="x"),),
        )
        assert verdict.has_new_must is False


class TestVerdictErrorException:
    def test_verdict_error_is_exception(self):
        error = VerdictError("boom")
        assert isinstance(error, Exception)
        assert str(error) == "boom"


class TestHappyPath:
    def test_full_block_parsed_exactly(self):
        text = """\
Here is my analysis of the artifact.

<verdict>
status: CONTINUE
resolved:
- #7 accepted
- #9 rejected: big-bang deliberate, feature flag adds complexity
remarks:
- [MUST] No rollback strategy in step 3
- [NICE] Consider a feature flag instead of big-bang
</verdict>
"""
        verdict = parse_verdict(text)
        assert verdict.status == "CONTINUE"
        assert verdict.resolutions == (
            Resolution(remark_id=7, accepted=True),
            Resolution(
                remark_id=9,
                accepted=False,
                justification="big-bang deliberate, feature flag adds complexity",
            ),
        )
        assert verdict.remarks == (
            Remark(severity=Severity.MUST, text="No rollback strategy in step 3"),
            Remark(severity=Severity.NICE, text="Consider a feature flag instead of big-bang"),
        )

    def test_agree_with_empty_remarks(self):
        text = """
<verdict>
status: AGREE
resolved:
- #1 accepted
remarks:
</verdict>
"""
        verdict = parse_verdict(text)
        assert verdict.status == "AGREE"
        assert verdict.resolutions == (Resolution(remark_id=1, accepted=True),)
        assert verdict.remarks == ()

    def test_continue_with_only_musts(self):
        text = """
<verdict>
status: CONTINUE
remarks:
- [MUST] fix this
- [MUST] fix that too
</verdict>
"""
        verdict = parse_verdict(text)
        assert verdict.status == "CONTINUE"
        assert len(verdict.remarks) == 2
        assert all(r.severity == Severity.MUST for r in verdict.remarks)
        assert verdict.has_new_must is True

    def test_mixed_must_nice_user(self):
        text = """
<verdict>
status: CONTINUE
remarks:
- [MUST] a
- [NICE] b
- [USER] c
</verdict>
"""
        verdict = parse_verdict(text)
        severities = [r.severity for r in verdict.remarks]
        assert severities == [Severity.MUST, Severity.NICE, Severity.USER]
        assert verdict.has_new_must is True

    def test_status_only_no_sections(self):
        text = "<verdict>\nstatus: AGREE\n</verdict>"
        verdict = parse_verdict(text)
        assert verdict.status == "AGREE"
        assert verdict.resolutions == ()
        assert verdict.remarks == ()

    def test_block_embedded_in_long_prose(self):
        text = (
            "Lots of discussion here about the artifact and various concerns "
            "that span multiple paragraphs and lines of reasoning before the "
            "actual structured verdict block shows up down below.\n\n"
            "More prose about tradeoffs, rollout risk, and testing strategy.\n\n"
            "<verdict>\n"
            "status: AGREE\n"
            "remarks:\n"
            "- [NICE] minor polish\n"
            "</verdict>\n\n"
            "Trailing commentary after the block, should be ignored entirely."
        )
        verdict = parse_verdict(text)
        assert verdict.status == "AGREE"
        assert verdict.remarks == (Remark(severity=Severity.NICE, text="minor polish"),)

    def test_two_blocks_last_wins(self):
        text = """
<verdict>
status: CONTINUE
remarks:
- [MUST] old remark that should be ignored
</verdict>

Some more back and forth happens here.

<verdict>
status: AGREE
remarks:
- [NICE] final remark
</verdict>
"""
        verdict = parse_verdict(text)
        assert verdict.status == "AGREE"
        assert verdict.remarks == (Remark(severity=Severity.NICE, text="final remark"),)

    def test_stray_verdict_tag_in_prose_before_complete_block_still_errors(self):
        """A literal, unclosed '<verdict>' mention in prose *before* the real
        block gets swallowed into the same non-greedy regex match (it pairs
        with the real block's closing tag), which corrupts the captured
        content and raises. This is the chosen/documented behavior for this
        edge case: a stray earlier opening tag does not trigger the
        "truncated trailing block" error (it's not after the last complete
        pair), but it does still make the block content unparsable.
        """
        text = (
            "Some prose mentioning <verdict> casually without closing it here.\n\n"
            "<verdict>\n"
            "status: AGREE\n"
            "remarks:\n"
            "- [NICE] ok\n"
            "</verdict>\n"
        )
        with pytest.raises(VerdictError):
            parse_verdict(text)


class TestTruncatedTrailingBlock:
    def test_complete_block_then_truncated_second_block_raises(self):
        """A complete block followed later by a second '<verdict>' that never
        closes must not silently fall back to the earlier (possibly stale)
        complete block -- the reply was truncated and that's an error.
        """
        text = """
<verdict>
status: CONTINUE
remarks:
- [MUST] real remark that must not be silently used
</verdict>

More back and forth happens here, then the response gets cut off mid-stream.

<verdict>
status: AGREE
remarks:
- [NICE] this second block never closes
"""
        with pytest.raises(VerdictError):
            parse_verdict(text)


class TestResolvedEntries:
    def test_accepted_entry(self):
        text = "<verdict>\nstatus: AGREE\nresolved:\n- #3 accepted\n</verdict>"
        verdict = parse_verdict(text)
        assert verdict.resolutions == (Resolution(remark_id=3, accepted=True),)

    def test_rejected_with_justification(self):
        text = (
            "<verdict>\nstatus: CONTINUE\nresolved:\n"
            "- #5 rejected: not worth the complexity\n</verdict>"
        )
        verdict = parse_verdict(text)
        assert verdict.resolutions == (
            Resolution(remark_id=5, accepted=False, justification="not worth the complexity"),
        )

    def test_rejected_without_justification_raises(self):
        text = "<verdict>\nstatus: CONTINUE\nresolved:\n- #5 rejected:\n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_rejected_without_justification_whitespace_only_raises(self):
        text = "<verdict>\nstatus: CONTINUE\nresolved:\n- #5 rejected:    \n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_malformed_resolved_entry_no_id_raises(self):
        text = "<verdict>\nstatus: CONTINUE\nresolved:\n- accepted\n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)


class TestErrors:
    def test_no_block_raises(self):
        with pytest.raises(VerdictError):
            parse_verdict("Just a plain reply with no verdict block at all.")

    def test_unclosed_block_raises(self):
        text = "<verdict>\nstatus: AGREE\n"
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_invalid_status_value_raises(self):
        text = "<verdict>\nstatus: MAYBE\n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_missing_status_raises(self):
        text = "<verdict>\nremarks:\n- [NICE] something\n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_unknown_remark_severity_raises(self):
        text = "<verdict>\nstatus: CONTINUE\nremarks:\n- [WTF] x\n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_remark_empty_text_raises(self):
        text = "<verdict>\nstatus: CONTINUE\nremarks:\n- [MUST]\n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_remark_whitespace_only_text_raises(self):
        text = "<verdict>\nstatus: CONTINUE\nremarks:\n- [MUST]    \n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_duplicate_resolution_raises(self):
        text = (
            "<verdict>\nstatus: CONTINUE\nresolved:\n"
            "- #7 accepted\n- #7 rejected: changed my mind\n</verdict>"
        )
        with pytest.raises(VerdictError):
            parse_verdict(text)

    def test_duplicate_status_line_raises(self):
        text = "<verdict>\nstatus: AGREE\nstatus: CONTINUE\n</verdict>"
        with pytest.raises(VerdictError):
            parse_verdict(text)


class TestWhitespaceTolerance:
    def test_indented_entries(self):
        text = (
            "<verdict>\n"
            "  status: AGREE\n"
            "  resolved:\n"
            "    - #1 accepted\n"
            "  remarks:\n"
            "    - [NICE] indented remark\n"
            "</verdict>"
        )
        verdict = parse_verdict(text)
        assert verdict.status == "AGREE"
        assert verdict.resolutions == (Resolution(remark_id=1, accepted=True),)
        assert verdict.remarks == (Remark(severity=Severity.NICE, text="indented remark"),)

    def test_trailing_spaces(self):
        text = (
            "<verdict>   \n"
            "status: AGREE   \n"
            "remarks:   \n"
            "- [MUST] trailing spaces here   \n"
            "</verdict>   "
        )
        verdict = parse_verdict(text)
        assert verdict.status == "AGREE"
        assert verdict.remarks == (
            Remark(severity=Severity.MUST, text="trailing spaces here"),
        )

    def test_crlf_input(self):
        text = (
            "<verdict>\r\n"
            "status: CONTINUE\r\n"
            "resolved:\r\n"
            "- #2 accepted\r\n"
            "remarks:\r\n"
            "- [MUST] crlf remark\r\n"
            "</verdict>\r\n"
        )
        verdict = parse_verdict(text)
        assert verdict.status == "CONTINUE"
        assert verdict.resolutions == (Resolution(remark_id=2, accepted=True),)
        assert verdict.remarks == (Remark(severity=Severity.MUST, text="crlf remark"),)
