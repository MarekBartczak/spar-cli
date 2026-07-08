from pathlib import Path
from spar.exec.prompts import build_impl_prompt, build_review_prompt
from spar.exec.tasklist import Task
from spar.state import StateRemark
from spar.verdict import Severity

T = Task("t1", "do it", "claude", "opus", "gpt-5.4", (), ("spar/a.py", "tests/test_a.py"))


def test_impl_prompt():
    p = build_impl_prompt(
        T, Path(".spar/artifact.md"), [StateRemark(3, Severity.MUST, "codex", "fix X")]
    )
    assert "t1" in p and "spar/a.py" in p and ".spar/artifact.md" in p
    assert "#3" in p and "<verdict>" in p
    assert "do not" in p.lower() and "DONE" in p  # impl must not emit DONE


def test_review_prompt():
    p = build_review_prompt(T, "diff --git a/spar/a.py ...", [])
    assert "diff --git" in p and "<verdict>" in p
    assert "read-only" in p.lower() or "do not edit" in p.lower()


def test_impl_prompt_does_not_invite_self_judgment():
    p = build_impl_prompt(
        T, Path(".spar/artifact.md"), [StateRemark(3, Severity.MUST, "codex", "fix X")]
    )
    assert "raise your own" not in p.lower()
    assert "remarks:" not in p
    assert "resolved:" in p


def test_impl_prompt_with_no_open_remarks_has_no_dangling_reference():
    p = build_impl_prompt(T, Path(".spar/artifact.md"), [])
    assert "remarks below" not in p.lower()
    assert "implement the task" in p.lower()


def test_impl_prompt_warning_included_when_provided():
    p = build_impl_prompt(
        T, Path(".spar/artifact.md"), [], warning="careful with X"
    )
    assert "careful with X" in p


def test_impl_prompt_warning_absent_when_not_provided():
    p = build_impl_prompt(T, Path(".spar/artifact.md"), [])
    assert "Warning:" not in p


def test_impl_prompt_first_turn_forces_real_file_writes_on_disk():
    # No open remarks -> the initial code-creating turn. It must force real edits
    # on disk with the model's tools, not a prose description, and still forbid
    # emitting DONE.
    p = build_impl_prompt(T, Path(".spar/artifact.md"), [])
    low = p.lower()
    assert "on disk" in low
    assert "file-editing tools" in low
    assert "do not merely describe" in low
    assert "DONE" in p and "do not emit done" in low


def test_impl_prompt_review_response_requires_edit_for_accepted():
    # Open remarks -> a review-response turn. Accepting a remark must require a
    # real code change on disk, not a prose acknowledgment; DONE still forbidden.
    p = build_impl_prompt(
        T, Path(".spar/artifact.md"), [StateRemark(3, Severity.MUST, "codex", "fix X")]
    )
    low = p.lower()
    assert "accepted" in low and "on disk" in low
    assert "real code change" in low
    assert "reject" in low
    # the code change is primary but the verdict still records it
    assert "resolved:" in p
    assert "DONE" in p and "do not emit done" in low
