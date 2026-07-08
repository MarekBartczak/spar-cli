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
