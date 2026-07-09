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
    low = p.lower()
    assert "raise your own" not in low
    # the prompt explicitly forbids a `remarks:` section (the implementer never
    # raises remarks) -- the literal substring "remarks:" appears only inside
    # that prohibition, never as an invitation to emit one.
    assert "do not include a `remarks:` section" in low or "you do not raise remarks" in low
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
    # still forbid raising remarks
    assert "do not include a `remarks:` section" in low or "you do not raise remarks" in low


def test_impl_prompt_no_remarks_leads_with_imperative_not_review():
    # The primary directive must be unmistakable: write the files now, this is
    # not a review. The verdict is a trailing formality.
    p = build_impl_prompt(T, Path(".spar/artifact.md"), [])
    low = p.lower()
    assert "create/edit" in low
    assert "on disk now" in low
    for f in T.files:
        assert f in p
    assert "not a review" in low
    # with no open remarks, the prompt must tell the model to OMIT resolved:
    # entirely rather than write any placeholder line.
    assert "omit" in low and "resolved:" in low
    assert "placeholder" in low
    # forbid remarks:/DONE
    assert "do not include a `remarks:` section" in low or "you do not raise remarks" in low
    assert "do not emit done" in low


def test_review_prompt_lists_foreign_and_merged_files():
    p = build_review_prompt(
        T,
        "diff --git a/CMakeLists.txt ...",
        [],
        foreign_files=(("t3", ("src/main.cpp",)), ("t4", ("tests/*.cpp",))),
        merged_files=("src/factorial.hpp", "src/factorial.cpp"),
    )
    # the foreign section names each scope with its owning task
    assert "Files owned by other, not-yet-merged tasks" in p
    assert "t3: src/main.cpp" in p
    assert "t4: tests/*.cpp" in p
    assert "their absence is NOT a defect by itself" in p
    # ...but a hard reference breaking THIS task's isolation is flagged
    assert "HARD-reference" in p
    assert "plan-ordering defect" in p
    # the merged section lists real paths present on the branch
    assert "Files already merged from earlier tasks" in p
    assert "src/factorial.hpp" in p
    # the defect rule accounts for diff, both lists, AND pre-existing files
    assert "already present in the repository" in p


def test_review_prompt_omits_context_sections_when_empty():
    p = build_review_prompt(T, "diff --git a/x ...", [])
    assert "not-yet-merged tasks" not in p
    assert "already merged from earlier tasks" not in p
    # ...but the missing-file protocol rule is PERMANENT: a standalone first
    # task must not draw MUSTs about files that pre-date the run
    assert "already present in the repository" in p


def test_impl_prompt_with_remarks_resolves_ids_and_still_forces_edit():
    # Open remarks -> must instruct resolving each id AND still making the real
    # edit on disk; still forbid remarks:/DONE.
    p = build_impl_prompt(
        T,
        Path(".spar/artifact.md"),
        [StateRemark(7, Severity.MUST, "codex", "fix X")],
    )
    low = p.lower()
    assert "#7" in p
    assert "create/edit" in low or "on disk" in low
    assert "accepted" in low and "rejected" in low
    assert "do not include a `remarks:` section" in low or "you do not raise remarks" in low
    assert "do not emit done" in low


def test_review_protocol_forbids_no_concerns_remark():
    p = build_review_prompt(T, "diff --git a/x ...", [])
    assert "OMIT the `remarks:` section entirely" in p


def test_review_protocol_hedges_foreign_section_reference():
    p = build_review_prompt(T, "diff --git a/x ...", [])
    assert "foreign-files section (when present)" in p


def test_impl_protocol_forbids_invented_remark_ids():
    p = build_impl_prompt(T, Path("/abs/plan.md"), [])
    assert "ONLY ids listed" in p
